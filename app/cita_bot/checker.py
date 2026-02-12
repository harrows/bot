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
from playwright._impl._errors import TargetClosedError

# ---- Настройки под "быстро поймать слот" ----
MAX_ATTEMPTS = 3

# Если сайт отдаёт пусто (часто признак антибота) — охлаждение длиннее
BACKOFF_EMPTY_PAGE = (120, 240)  # 2–4 минуты
# Если просто не нашли кнопку — короткий бэкофф
BACKOFF_NO_BUTTON = (10, 25)

NAV_TIMEOUT_MS = 45_000
CLICK_TIMEOUT_MS = 30_000

# Весь check_once не должен жить дольше этого (чтобы не блокировать мониторинг)
TOTAL_TIMEOUT_SECONDS = 110

NO_SLOTS_PATTERNS = [
    r"No hay horas disponibles",
    r"Inténtelo de nuevo",
]


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


async def _safe_close_context(ctx: BrowserContext) -> None:
    try:
        await ctx.close()
    except TargetClosedError:
        pass
    except Exception:
        # закрытие не должно падать и мешать мониторингу
        pass


async def _safe_networkidle(page: Page, timeout_ms: int) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        return


async def _impl_check_once(
    target_url: str,
    data_dir: Path,
    screenshot_on_slots: bool,
    headless: bool,
) -> CheckResult:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    profile_dir = data_dir / "pw_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx: BrowserContext = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 720},
        )
        page = await ctx.new_page()

        # Автопринятие alert("Welcome/Bienvenido")
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # джиттер, чтобы не выглядеть как cron
                await page.wait_for_timeout(random.randint(150, 650))

                await page.goto(target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await _safe_networkidle(page, NAV_TIMEOUT_MS)
                await page.wait_for_timeout(random.randint(400, 900))

                # Проверка на "пустую выдачу"
                try:
                    body_text = await page.inner_text("body")
                except Exception:
                    body_text = ""
                normalized = _normalize(body_text or "")

                if len(normalized) < 5:
                    png, html = await _dump_debug(page, data_dir, f"fail_empty_attempt{attempt}")
                    await page.wait_for_timeout(random.randint(*BACKOFF_EMPTY_PAGE) * 1000)
                    raise RuntimeError(f"Empty page (likely protection). Debug saved: {png} {html}")

                btn = await _find_continue_anywhere(page)
                if not btn:
                    png, html = await _dump_debug(page, data_dir, f"fail_no_continue_attempt{attempt}")
                    await page.wait_for_timeout(random.randint(*BACKOFF_NO_BUTTON) * 1000)
                    raise RuntimeError(f"Continue button not found. Debug saved: {png} {html}")

                await btn.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                await btn.click(timeout=CLICK_TIMEOUT_MS)

                await _safe_networkidle(page, 20_000)
                await page.wait_for_timeout(random.randint(500, 1200))

                body_text2 = await page.inner_text("body")
                normalized2 = _normalize(body_text2 or "")

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

                await _safe_close_context(ctx)

                return CheckResult(
                    checked_at=checked_at,
                    has_slots=has_slots,
                    summary=summary,
                    digest=digest,
                    screenshot_path=screenshot_path,
                )

            except Exception as e:
                last_error = e
                if attempt < MAX_ATTEMPTS:
                    continue
                await _safe_close_context(ctx)
                raise last_error

        # mypy/линтеру для гарантий
        await _safe_close_context(ctx)
        raise RuntimeError("Unexpected exit from check loop without result")


async def check_once(
    target_url: str,
    data_dir: Path,
    screenshot_on_slots: bool = True,
    headless: bool = True,
) -> CheckResult:
    # Жёсткий таймаут на весь чек — чтобы не было пропусков тиков
    return await asyncio.wait_for(
        _impl_check_once(target_url, data_dir, screenshot_on_slots, headless),
        timeout=TOTAL_TIMEOUT_SECONDS,
    )
