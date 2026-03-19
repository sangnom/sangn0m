#!/usr/bin/env python3
"""
Step 1: 수동 로그인 후 세션 저장
이 스크립트를 먼저 실행해서 로그인 + 핸드폰 인증을 직접 하고,
세션을 session.json에 저장합니다.
이후 subscribe.py는 이 세션을 재사용하여 인증 없이 동작합니다.
"""

import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

SESSION_FILE = "session.json"
LOGIN_URL = "https://account.alibabacloud.com/login/login.htm"
CHECK_URL = "https://home-intl.console.aliyun.com/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def save_session() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        log.info("로그인 페이지를 엽니다.")
        log.info("브라우저에서 직접 로그인 + 핸드폰 인증을 완료해주세요.")
        log.info("로그인이 완료되면 자동으로 세션이 저장됩니다.")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # 콘솔 페이지로 이동할 때까지 대기 (최대 5분)
        try:
            await page.wait_for_url("**console**", timeout=300_000)
        except Exception:
            log.warning("콘솔 이동 감지 실패. 현재 URL 기준으로 세션을 저장합니다.")

        # 쿠키 및 스토리지 저장
        storage_state = await context.storage_state()
        Path(SESSION_FILE).write_text(json.dumps(storage_state, indent=2))
        log.info(f"세션 저장 완료: {SESSION_FILE}")
        log.info("이제 subscribe.py를 실행하면 자동으로 로그인 상태가 유지됩니다.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(save_session())
