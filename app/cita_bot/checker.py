from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

log = logging.getLogger("cita_bot.checker")

NO_SLOTS_PATTERNS = [
    r"No hay horas disponibles",
    r"Inténtelo de nuevo",
]

# Это не "обход". Это корректный backoff, чтобы не триггерить защиту при нестабильных страницах.
SOFT_BACKOFF_SECONDS_RANGE = (8, 18)


@dataclass(frozen=True)
class CheckResult:
    checked_at: str
    has_slots: bool
    summary: str
    digest: str
    screenshot_path: Optional[str] = None


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _looks_like_no_slots(page_text: str) -> bool:
    txt = _normalize(page_text)
    return any(re.search(p, txt, re.IGNORECASE) for p in NO_SLOTS_PATTERNS)


def _make_digest(text: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


async def _dump_debug(page, data_dir: Path, prefix: str) -> tuple[str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    png = data_dir / "screenshots" / f"{prefix}_{ts}.png"
    html = data_dir / "screenshots" / f"{prefix}_{ts}.html"
    await page.screenshot(path=str(png), full_page=True)
    html.write_text(await page.content(), encoding="utf-8")
    return str(png), str(html)


async def check_once(
    target_url: str,
    data_dir: Path,
    screenshot_on_slots: bool = True,
    headless: bool = True,
) -> CheckResult:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Если будет alert("Welcome...") — примем. Если не будет — это ок.
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        # Небольшой jitter перед началом действий (не "обход", а стабилизация и снижение синхронных запросов)
        await page.wait_for_timeout(random.randint(200, 900))

        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(random.randint(700, 1400))

        # Кнопка Continue / Continuar: пробуем разные варианты
        candidates = [
            page.locator("#idCaptchaButton"),
            page.get_by_role("button", name=re.compile(r"continue", re.I)),
            page.get_by_role("button", name=re.compile(r"continuar", re.I)),
            page.get_by_text("Continue", exact=False),
            page.get_by_text("Continuar", exact=False),
            page.locator('input[type="submit"]'),
        ]

        btn = None
        for c in candidates:
            try:
                if await c.count() > 0:
                    btn = c.first
                    break
            except Exception:
                continue

        if not btn:
            # Сохраним, что реально открылось (часто это баннер/предупреждение/заглушка)
            png, html = await _dump_debug(page, data_dir, "fail_no_continue")
            await browser.close()

            # Мягкая пауза, чтобы не долбить сайт при "странном" состоянии
            await asyncio.sleep(random.randint(*SOFT_BACKOFF_SECONDS_RANGE))
            raise RuntimeError(f"Continue button not found. Debug saved: {png} {html}")

        # Дождёмся видимости и кликнем
        await btn.wait_for(state="visible", timeout=60_000)
        await btn.click(timeout=60_000)

        # Ждём либо перехода на services, либо появления характерного контента
        try:
            await page.wait_for_url(re.compile(r"#services"), timeout=60_000)
        except Exception:
            # если не перешло, снимем диагностику
            png, html = await _dump_debug(page, data_dir, "fail_no_services")
            await browser.close()
            await asyncio.sleep(random.randint(*SOFT_BACKOFF_SECONDS_RANGE))
            raise RuntimeError(f"Did not reach #services. Debug saved: {png} {html}")

        await page.wait_for_timeout(random.randint(900, 1700))

        body_text = await page.inner_text("body")
        normalized = _normalize(body_text)

        has_slots = not _looks_like_no_slots(normalized)
        summary = normalized[:350]
        digest = _make_digest(normalized)

        screenshot_path = None
        if has_slots and screenshot_on_slots:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out = data_dir / "screenshots" / f"slots_{ts}.png"
            await page.screenshot(path=str(out), full_page=True)
            screenshot_path = str(out)

        await browser.close()

        return CheckResult(
            checked_at=checked_at,
            has_slots=has_slots,
            summary=summary,
            digest=digest,
            screenshot_path=screenshot_path,
        )
