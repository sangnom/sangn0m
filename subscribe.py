#!/usr/bin/env python3
"""
Alibaba Cloud Lite Plan Auto-Subscriber
매일 GMT+8 00:00에 구독 슬롯이 열릴 때 자동으로 구독을 시도합니다.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, Page, Browser

# ─── 설정 ────────────────────────────────────────────────────────────────────
TARGET_URL = (
    "https://common-buy-intl.alibabacloud.com/coding-plan"
    "?spm=a3c0i.45656938.0.0.4c53TjwJTjwJWl"
    "&accounttraceid=7541db89e8c642a886ef5f025c799a41wjte"
)

# 환경 변수 또는 직접 입력
ALIBABA_USERNAME = os.environ.get("ALIBABA_USERNAME", "")
ALIBABA_PASSWORD = os.environ.get("ALIBABA_PASSWORD", "")

# GMT+8 기준 갱신 시각
RENEWAL_HOUR_GMT8 = 0   # 00:00
RENEWAL_MINUTE_GMT8 = 0

# 갱신 전 미리 대기 시작할 초 (30초 전부터 폴링 시작)
PRE_START_SECONDS = 30

# 폴링 간격 (초) – 슬롯 오픈 직후 버튼 클릭을 위한 간격
POLL_INTERVAL = 0.5

# 최대 시도 횟수 (오픈 후 2분 동안 재시도)
MAX_ATTEMPTS = int(120 / POLL_INTERVAL)

GMT8 = timezone(timedelta(hours=8))

# ─── 로깅 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def seconds_until_next_renewal() -> float:
    """다음 GMT+8 00:00까지 남은 초를 반환합니다."""
    now = datetime.now(GMT8)
    next_renewal = now.replace(
        hour=RENEWAL_HOUR_GMT8,
        minute=RENEWAL_MINUTE_GMT8,
        second=0,
        microsecond=0,
    )
    if next_renewal <= now:
        next_renewal += timedelta(days=1)
    delta = (next_renewal - now).total_seconds()
    return delta


async def login(page: Page) -> None:
    """Alibaba Cloud 계정으로 로그인합니다."""
    log.info("로그인 페이지로 이동 중...")
    await page.goto("https://account.alibabacloud.com/login/login.htm", wait_until="domcontentloaded")

    # 아이디/비밀번호 입력
    await page.wait_for_selector("input[name='loginId'], #loginId, input[type='text']", timeout=15000)

    # 계정 ID 입력
    id_field = page.locator("input[name='loginId'], #loginId, input[type='text']").first
    await id_field.fill(ALIBABA_USERNAME)

    pw_field = page.locator("input[name='loginPassword'], #loginPassword, input[type='password']").first
    await pw_field.fill(ALIBABA_PASSWORD)

    # 로그인 버튼 클릭
    await page.locator("button[type='submit'], .submit-btn, #submit").first.click()

    # 로그인 완료 대기 (2FA가 있을 경우 수동 처리 필요)
    try:
        await page.wait_for_url("**/console**", timeout=30000)
        log.info("로그인 성공!")
    except Exception:
        log.warning("로그인 후 콘솔로 리다이렉트되지 않았습니다. 2FA 또는 추가 인증이 필요할 수 있습니다.")
        log.warning("30초 내에 수동으로 로그인을 완료해주세요...")
        await page.wait_for_timeout(30000)


async def navigate_to_plan_page(page: Page) -> None:
    """구독 페이지로 이동합니다."""
    log.info("구독 페이지로 이동 중...")
    await page.goto(TARGET_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)


async def try_subscribe(page: Page) -> bool:
    """
    구독 버튼을 찾아 클릭합니다.
    성공 시 True, 실패 시 False 반환.
    """
    # 가능한 버튼 셀렉터들 (실제 페이지에 따라 조정 필요)
    button_selectors = [
        "button:has-text('Subscribe')",
        "button:has-text('Buy Now')",
        "button:has-text('立即购买')",
        "button:has-text('立即订阅')",
        "button:has-text('구독')",
        ".buy-btn",
        ".subscribe-btn",
        "[data-spm*='buy']",
        "[data-spm*='subscribe']",
        "a:has-text('Subscribe')",
        "a:has-text('Buy Now')",
    ]

    for selector in button_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=500):
                is_disabled = await btn.is_disabled()
                if not is_disabled:
                    log.info(f"버튼 발견: {selector} - 클릭 시도!")
                    await btn.click()
                    await page.wait_for_timeout(2000)

                    # 구매 확인 모달/버튼 처리
                    confirm_selectors = [
                        "button:has-text('Confirm')",
                        "button:has-text('OK')",
                        "button:has-text('确认')",
                        "button:has-text('확인')",
                        ".confirm-btn",
                        ".ok-btn",
                    ]
                    for confirm_sel in confirm_selectors:
                        confirm_btn = page.locator(confirm_sel).first
                        if await confirm_btn.is_visible(timeout=1000):
                            await confirm_btn.click()
                            log.info("확인 버튼 클릭 완료!")
                            break

                    return True
        except Exception:
            continue

    return False


async def wait_and_subscribe() -> None:
    """메인 로직: 대기 → 로그인 → 구독 시도."""
    if not ALIBABA_USERNAME or not ALIBABA_PASSWORD:
        log.error(
            "환경 변수를 설정해주세요:\n"
            "  export ALIBABA_USERNAME='your_account'\n"
            "  export ALIBABA_PASSWORD='your_password'"
        )
        return

    async with async_playwright() as pw:
        # 브라우저 실행 (headless=False 로 설정하여 진행 상황 확인 가능)
        browser: Browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        try:
            # 1. 로그인
            await login(page)

            # 2. 갱신 시각까지 대기
            secs = seconds_until_next_renewal()
            wait_secs = max(0.0, secs - PRE_START_SECONDS)
            if wait_secs > 60:
                renewal_time = datetime.now(GMT8) + timedelta(seconds=secs)
                log.info(
                    f"다음 갱신 시각(GMT+8): {renewal_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                log.info(f"대기 시간: {wait_secs:.0f}초 ({wait_secs/3600:.1f}시간)")
                # 긴 대기 중에는 주기적으로 상태 출력
                while wait_secs > 60:
                    await asyncio.sleep(min(wait_secs - 60, 60))
                    wait_secs = max(0.0, seconds_until_next_renewal() - PRE_START_SECONDS)
                    remaining = seconds_until_next_renewal()
                    log.info(f"갱신까지 남은 시간: {remaining:.0f}초")

            # 마지막 남은 시간 대기
            final_wait = max(0.0, seconds_until_next_renewal() - PRE_START_SECONDS)
            if final_wait > 0:
                log.info(f"갱신 {PRE_START_SECONDS}초 전... {final_wait:.1f}초 대기 중")
                await asyncio.sleep(final_wait)

            # 3. 구독 페이지로 이동
            await navigate_to_plan_page(page)

            # 4. 00:00 정각까지 정밀 대기
            precise_wait = seconds_until_next_renewal()
            if precise_wait > 0:
                log.info(f"정각까지 {precise_wait:.2f}초 대기...")
                await asyncio.sleep(precise_wait)

            log.info("=== 구독 시도 시작! ===")

            # 5. 구독 버튼 폴링 및 클릭 시도
            for attempt in range(1, MAX_ATTEMPTS + 1):
                log.info(f"시도 {attempt}/{MAX_ATTEMPTS}...")

                # 페이지 새로고침 (첫 시도 후 매 5번마다)
                if attempt == 1 or attempt % 5 == 0:
                    await page.reload(wait_until="domcontentloaded")
                    await page.wait_for_timeout(500)

                success = await try_subscribe(page)
                if success:
                    log.info("구독 성공! 스크린샷을 저장합니다...")
                    await page.screenshot(path="subscription_success.png", full_page=True)
                    log.info("screenshot: subscription_success.png")
                    break

                await asyncio.sleep(POLL_INTERVAL)
            else:
                log.warning("최대 시도 횟수에 도달했습니다. 구독에 실패했습니다.")
                await page.screenshot(path="subscription_failed.png", full_page=True)

        finally:
            await asyncio.sleep(5)  # 결과 확인용
            await browser.close()


if __name__ == "__main__":
    asyncio.run(wait_and_subscribe())
