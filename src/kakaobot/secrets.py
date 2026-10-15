"""자격증명 읽기 — 환경변수 먼저, 없으면 macOS 키체인.

메뉴바에서 띄운 봇은 셸 프로필(~/.zshrc)을 거치지 않아 환경변수가 비어 있을 수 있다.
scripts/setup-secrets.sh 가 키체인에 저장한 값을 여기서 직접 꺼낸다.
값은 로그에 남기지 않는다. 설정 파일에는 절대 쓰지 않는다 (계획서 §6-4).
"""

from __future__ import annotations

import getpass
import os
import subprocess

# setup-secrets.sh 와 같은 이름
KEYCHAIN = {
    "TELEGRAM_BOT_TOKEN": "kakaobot-telegram-token",
    "TELEGRAM_CHAT_ID": "kakaobot-telegram-chat",
    "ANTHROPIC_API_KEY": "kakaobot-anthropic-key",
}


def get_secret(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    service = KEYCHAIN.get(env_name)
    if not service:
        return ""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def has_secret(env_name: str) -> bool:
    return bool(get_secret(env_name))
