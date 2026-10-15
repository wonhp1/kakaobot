"""운영자 알림 (계획서 §6-3 에스컬레이션 루프, D4 = 텔레그램).

§6-3 의 지식베이스 성장 루프는 **운영자가 에스컬레이션을 인지해야** 돌아간다.
알림이 없으면 로그를 직접 열어보기 전까지 아무도 모르고 루프가 끊긴다.

알림 실패가 봇을 멈춰서는 안 된다. 여기서 나는 오류는 전부 삼키고 로그만 남긴다.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

log = logging.getLogger("kakaobot.notify")

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "TELEGRAM_CHAT_ID"


class Notifier(ABC):
    @abstractmethod
    def send(self, text: str) -> bool: ...


class NullNotifier(Notifier):
    """알림 채널이 설정되지 않았을 때. 조용히 넘어가되 한 번은 경고한다."""

    def __init__(self) -> None:
        self._warned = False

    def send(self, text: str) -> bool:
        if not self._warned:
            log.warning(
                "알림 채널이 설정되지 않았습니다. 에스컬레이션은 logs/audit.jsonl "
                "에만 기록됩니다 (계획서 §6-3 루프가 끊깁니다)."
            )
            self._warned = True
        return False


class TelegramNotifier(Notifier):
    """텔레그램 Bot API.

    토큰은 환경변수에서만 읽는다 — 설정 파일에 쓰지 않는다 (계획서 §6-4 원칙).
      TELEGRAM_BOT_TOKEN : @BotFather 에서 발급
      TELEGRAM_CHAT_ID   : 알림을 받을 대화방 id
    """

    def __init__(self, token: str, chat_id: str, timeout: int = 10) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    @classmethod
    def from_env(cls, chat_id: str = "") -> Notifier:
        from .secrets import get_secret

        token = get_secret(TELEGRAM_TOKEN_ENV)
        chat = (chat_id or get_secret(TELEGRAM_CHAT_ENV)).strip()
        if not token or not chat:
            missing = []
            if not token:
                missing.append(TELEGRAM_TOKEN_ENV)
            if not chat:
                missing.append(f"{TELEGRAM_CHAT_ENV} (또는 config 의 telegram_chat_id)")
            log.warning("텔레그램 설정이 없습니다: %s", ", ".join(missing))
            return NullNotifier()
        return cls(token, chat)

    def send(self, text: str) -> bool:
        import httpx

        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.error("텔레그램 전송 실패 %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as exc:
            # 알림 실패로 봇이 멈추면 안 된다
            log.error("텔레그램 전송 오류: %s", exc)
            return False


def build_notifier(cfg) -> Notifier:
    channel = str(cfg.get("notify", "channel", "none")).lower()
    if channel == "telegram":
        return TelegramNotifier.from_env(cfg.get("notify", "telegram_chat_id", ""))
    return NullNotifier()


def format_escalation(sender: str, question: str, reason: str, score: float) -> str:
    """운영자가 보고 바로 대응할 수 있게 만든다."""
    return (
        "🔔 답변 근거 없음 — 확인이 필요합니다\n"
        f"\n질문자: {sender}"
        f"\n질문: {question}"
        f"\n\n사유: {reason}"
        f"\n유사도: {score:.3f}"
        "\n\n답변 후 data/knowledge/ 에 문서를 추가하면"
        "\n다음부터 자동 응답됩니다."
    )


def format_canary_failure(detail: str) -> str:
    return (
        "🚨 봇 자가진단 실패\n"
        f"\n{detail}"
        "\n\n조용히 먹통이 된 상태일 수 있습니다. 확인해주세요."
        "\n체크: 카카오톡 실행 / 대상 방이 별도 창 / 창 최소화 여부"
    )
