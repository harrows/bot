from __future__ import annotations

import asyncio
import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Union

from playwright.async_api import async_playwright, Page, Frame, BrowserContext

# "Слотов нет" (расширяй по мере наблюдений)
NO_SLOTS_PATTERNS = [
    r"No hay horas disponibles",
    r"Inténtelo de nuevo",
]

# Ретраи
MAX_ATTEMPTS = 3

# Бэкоффы (чтобы не усугублять антибот-защиту)
BACKOFF_NO_BUTTON = (20, 60)       # кнопки нет, но страница не пустая
BACKOFF_EMPTY_PAGE = (180, 420)    # страница пустая -> похоже на защиту/заглушку

# Таймауты
NAV_TIMEOUT_MS = 60_000
CLICK_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class CheckResult:
    checked_at: str
    has_slots: bool
    summary: str
    digest: str
    screenshot_path: Optional[str] = None


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _looks_like_no_slots(text: str) -> bool:
    t = _normalize(text)
    return any(re.search(p, t, re.IGNORECASE) for p in NO_SLOTS_PATTERNS)


def _make_digest(text: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


async def _dump_debug(page: Page, data_dir: Path, prefix: str) -> tuple[str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    png = data_dir / "screenshots" / f"{prefix}_{ts}.png"
    html = data_dir / "screenshots" / f"{prefix}_{ts}.html"
    await page.screenshot(path=str(png), full_page=True)
    html.write_text(await page.content(), encoding="utf-8")
    return str(png), str(html)


def _scopes(page: Page) -> list[Union[Page, Frame]]:
    # main page + все фреймы (iframe)
    return [page] + list(page.frames)


async def _first_existing(locator_candidates: Sequence) -> Optional:
    for loc in locator_candidates:
        try:
            if await loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


async def _find_continue_anywhere(page: Page) -> Optional:
    # Ищем кнопку во всех scope: main + iframe
    for scope in _scopes(page):
        btn = await _first_existing(
            [
                scope.locator("#idCaptchaButton"),
                scope.get_by_role("button", name=re.compile(r"continue", re.I)),
                scope.get_by_role("button", name=re.compile(r"continuar", re.I)),
                scope.get_by_text(re.compile(r"continue", re.I)),
                scope.get_by_text(re.compile(r"continuar", re.I)),
                scope.locator('input[type="submit"]'),
            ]
        )
        if btn:
            return btn
    return None


async def _safe_networkidle(page: Page, timeout_ms: int) -> None:
    # networkidle может не наступить — это ок
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        return


async def check_once(
    target_url: str,
    data_dir: Path,
    screenshot_on_slots: bool = True,
    headless: bool = True,
) -> CheckResult:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    profile_dir = data_dir / "pw_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    last_error: Optional[Exception] = None

    async with async_playwright() as p:
        # Persistent context: сохраняем cookies/localStorage между тиками
        context: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 720},
        )

        page = await context.new_page()

        # Автопринятие alert("Welcome / Bienvenido") -> OK
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # джиттер перед попыткой
                await page.wait_for_timeout(random.randint(250, 950))

                await page.goto(target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await _safe_networkidle(page, NAV_TIMEOUT_MS)
                await page.wait_for_timeout(random.randint(800, 1600))

                # Быстрый индикатор "пустая страница"
                body_text = ""
                try:
                    body_text = await page.inner_text("body")
                except Exception:
                    body_text = ""

                normalized_body = _normalize(body_text or "")
                if len(normalized_body) < 5:
                    png, html = await _dump_debug(page, data_dir, f"fail_empty_attempt{attempt}")
                    # длинный бэкофф: похоже, сайт режет выдачу
                    await page.wait_for_timeout(random.randint(*BACKOFF_EMPTY_PAGE) * 1000)
                    raise RuntimeError(f"Empty page (likely blocked). Debug saved: {png} {html}")

                # Ищем Continue/Continuar
                btn = await _find_continue_anywhere(page)
                if not btn:
                    png, html = await _dump_debug(page, data_dir, f"fail_no_continue_attempt{attempt}")
                    await page.wait_for_timeout(random.randint(*BACKOFF_NO_BUTTON) * 1000)
                    raise RuntimeError(f"Continue button not found. Debug saved: {png} {html}")

                await btn.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                await btn.click(timeout=CLICK_TIMEOUT_MS)

                # Дать странице прогрузиться после клика
                await _safe_networkidle(page, 30_000)
                await page.wait_for_timeout(random.randint(900, 1800))

                body_text2 = await page.inner_text("body")
                normalized2 = _normalize(body_text2)

                if len(normalized2) < 20:
                    png, html = await _dump_debug(page, data_dir, f"fail_empty_after_click_attempt{attempt}")
                    await page.wait_for_timeout(random.randint(*BACKOFF_EMPTY_PAGE) * 1000)
                    raise RuntimeError(f"Empty content after click. Debug saved: {png} {html}")

                has_slots = not _looks_like_no_slots(normalized2)
                summary = normalized2[:350]
                digest = _make_digest(normalized2)

                screenshot_path = None
                if has_slots and screenshot_on_slots:
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    out = data_dir / "screenshots" / f"slots_{ts}.png"
                    await page.screenshot(path=str(out), full_page=True)
                    screenshot_path = str(out)

                await context.close()

                return CheckResult(
                    checked_at=checked_at,
                    has_slots=has_slots,
                    summary=summary,
                    digest=digest,
                    screenshot_path=screenshot_path,
                )

            except Exception as e:
                last_error = e
                # если есть ещё попытки — пробуем заново (перезагрузка/ожидание уже внутри)
                if attempt < MAX_ATTEMPTS:
                    continue
                await context.close()
                raise last_error
        raise RuntimeError("Unexpected exit from check_once() without result")
