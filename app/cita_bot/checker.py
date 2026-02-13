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


# --- timeouts / attempts ---
MAX_ATTEMPTS = 2
NAV_TIMEOUT_MS = 45_000
CLICK_TIMEOUT_MS = 30_000
TOTAL_TIMEOUT_SECONDS = 70  # оставляем константу (может использоваться в bot.py)

NO_SLOTS_PATTERNS = [
    r"No hay horas disponibles",
    r"Inténtelo de nuevo",
]


class EmptyPageError(RuntimeError):
    """Сайт отдал пустую/заглушечную страницу (часто soft-block/защита)."""


class ContinueNotFoundError(RuntimeError):
    """Не нашли Continue/Continuar (возможна смена разметки/iframe/заглушка)."""


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


def _fire_and_forget(coro) -> asyncio.Task:
    """
    Запускаем корутину фоном и гарантируем, что исключение НЕ станет
    'Future exception was never retrieved'.
    """
    t = asyncio.create_task(coro)

    def _done(_t: asyncio.Task):
        try:
            _t.result()
        except Exception:
            pass

    t.add_done_callback(_done)
    return t


async def _dump_debug(page: Page, data_dir: Path, prefix: str) -> tuple[str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    png = data_dir / "screenshots" / f"{prefix}_{ts}.png"
    html = data_dir / "screenshots" / f"{prefix}_{ts}.html"

    # при закрытии страницы/контекста не должны валить основной поток
    try:
        await page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass

    try:
        html.write_text(await page.content(), encoding="utf-8")
    except Exception:
        try:
            html.write_text("", encoding="utf-8")
        except Exception:
            pass

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


async def _safe_close_context(ctx: Optional[BrowserContext]) -> None:
    if ctx is None:
        return
    try:
        await ctx.close()
    except TargetClosedError:
        pass
    except Exception:
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

    ctx: Optional[BrowserContext] = None
    try:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                viewport={"width": 1280, "height": 720},
            )
            page = await ctx.new_page()

            async def _safe_accept(dialog):
                try:
                    await dialog.accept()
                except Exception:
                    pass

            # ВАЖНО: без "голого create_task", иначе будут Future exception was never retrieved
            page.on("dialog", lambda d: _fire_and_forget(_safe_accept(d)))

            last_error: Optional[Exception] = None

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    # небольшой джиттер (Playwright ждёт МС)
                    await page.wait_for_timeout(random.randint(150, 650))

                    resp = await page.goto(
                        target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                    )
                    await _safe_networkidle(page, 20_000)
                    await page.wait_for_timeout(random.randint(200, 700))

                    status = None
                    try:
                        if resp:
                            status = resp.status
                    except Exception:
                        status = None

                    # Если "пусто" — это ключевой сигнал. Не ждём минутами, сразу отдаём вверх.
                    body_text = ""
                    try:
                        body_text = await page.inner_text("body")
                    except Exception:
                        body_text = ""

                    normalized = _normalize(body_text or "")
                    if len(normalized) < 5:
                        png, html = await _dump_debug(
                            page, data_dir, f"fail_empty_s{status}_a{attempt}"
                        )
                        raise EmptyPageError(
                            f"Empty page (status={status}). Debug: {png} {html}"
                        )

                    btn = await _find_continue_anywhere(page)
                    if not btn:
                        png, html = await _dump_debug(
                            page, data_dir, f"fail_no_continue_s{status}_a{attempt}"
                        )
                        raise ContinueNotFoundError(
                            f"Continue not found (status={status}). Debug: {png} {html}"
                        )

                    await btn.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                    await btn.click(timeout=CLICK_TIMEOUT_MS)

                    await _safe_networkidle(page, 20_000)
                    await page.wait_for_timeout(random.randint(250, 800))

                    body_text2 = ""
                    try:
                        body_text2 = await page.inner_text("body")
                    except Exception:
                        body_text2 = ""

                    normalized2 = _normalize(body_text2 or "")
                    if len(normalized2) < 20:
                        png, html = await _dump_debug(
                            page, data_dir, f"fail_empty_after_click_a{attempt}"
                        )
                        raise EmptyPageError(f"Empty after click. Debug: {png} {html}")

                    has_slots = not _looks_like_no_slots(normalized2)
                    summary = normalized2[:350]
                    digest = _make_digest(normalized2)

                    screenshot_path = None
                    if has_slots and screenshot_on_slots:
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        out = data_dir / "screenshots" / f"slots_{ts}.png"
                        try:
                            await page.screenshot(path=str(out), full_page=True)
                            screenshot_path = str(out)
                        except Exception:
                            screenshot_path = None

                    return CheckResult(
                        checked_at=checked_at,
                        has_slots=has_slots,
                        summary=summary,
                        digest=digest,
                        screenshot_path=screenshot_path,
                    )

                except asyncio.CancelledError:
                    # если отменили сверху — корректно пробрасываем, cleanup будет в finally
                    raise
                except Exception as e:
                    last_error = e
                    if attempt < MAX_ATTEMPTS:
                        continue
                    raise last_error

            raise RuntimeError("Unexpected exit without result")
    finally:
        await _safe_close_context(ctx)


async def check_once(
    target_url: str,
    data_dir: Path,
    screenshot_on_slots: bool = True,
    headless: bool = True,
) -> CheckResult:
    # ВАЖНО: убрали asyncio.wait_for отсюда.
    # Таймаут делай на уровне bot.py (monitor_tick), иначе получишь CancelledError внутри Playwright,
    # а затем каскад TargetClosedError.
    return await _impl_check_once(target_url, data_dir, screenshot_on_slots, headless)
