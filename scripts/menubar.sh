#!/usr/bin/env bash
# 카카오봇 메뉴바 띄우기. 이미 떠 있으면 아무 일도 하지 않는다.
#
#   bash scripts/menubar.sh
#
# ⚠️ 권한 주체: 메뉴바가 띄운 봇은 메뉴바를 띄운 앱(터미널·Claude 앱 등)의 손쉬운 사용 권한을
# 이어받는다. 권한이 없는 곳에서 띄우면 봇이 카카오톡 창을 읽지 못한다 (docs/permissions-setup.md).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
nohup .venv/bin/python -m kakaobot.menubar >> logs/menubar.log 2>&1 &
echo "메뉴바를 띄웠습니다 (pid $!). 화면 오른쪽 위 🤖 아이콘을 누르세요."
