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
echo "실행 방법:"
echo "  export ALIBABA_USERNAME='your_alibaba_account_or_email'"
echo "  export ALIBABA_PASSWORD='your_password'"
echo "  python subscribe.py"
echo ""
echo "백그라운드 실행 (로그 파일 저장):"
echo "  nohup python subscribe.py > subscribe.log 2>&1 &"
echo "  tail -f subscribe.log  # 로그 확인"
