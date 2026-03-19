#!/usr/bin/env bash
# Alibaba Cloud Auto-Subscribe - 초기 설정 스크립트
set -e

echo "=== 의존성 설치 중 ==="
pip install -r requirements.txt

echo "=== Playwright 브라우저 설치 중 ==="
playwright install chromium

echo ""
echo "=== 설정 완료 ==="
echo ""
echo "실행 방법 (권장):"
echo "  Step 1. python save_session.py   ← 브라우저에서 직접 로그인 + 핸드폰 인증"
echo "  Step 2. python subscribe.py      ← 저장된 세션으로 자동 구독 대기"
echo ""
echo "백그라운드 실행 (Step 2, 로그 파일 저장):"
echo "  nohup python subscribe.py > subscribe.log 2>&1 &"
echo "  tail -f subscribe.log  # 로그 확인"
echo ""
echo "※ 세션이 만료된 경우 save_session.py 를 다시 실행하세요."
