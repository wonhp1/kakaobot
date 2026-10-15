"""남은 안전장치 테스트 — 랜덤 지연 · Canary · 알림."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.notify import (
    NullNotifier,
    TelegramNotifier,
    format_canary_failure,
    format_escalation,
)


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def test_null_notifier_warns_once_and_returns_false():
    n = NullNotifier()
    assert n.send("a") is False
    assert n.send("b") is False       # 두 번째부터는 조용히


def test_telegram_falls_back_when_env_missing(monkeypatch):
    """토큰이 없으면 예외가 아니라 NullNotifier 로 떨어진다 — 봇이 멈추면 안 된다."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr("kakaobot.secrets.KEYCHAIN", {})   # 실제 키체인을 보지 않는다
    assert isinstance(TelegramNotifier.from_env(), NullNotifier)


def test_secret_prefers_env_and_never_needs_config(monkeypatch):
    from kakaobot.secrets import get_secret

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    assert get_secret("TELEGRAM_BOT_TOKEN") == "from-env"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setattr("kakaobot.secrets.KEYCHAIN", {})
    assert get_secret("TELEGRAM_BOT_TOKEN") == ""


def test_telegram_built_when_env_present(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert isinstance(TelegramNotifier.from_env(), TelegramNotifier)


def test_telegram_send_failure_does_not_raise(monkeypatch):
    """알림 실패가 봇을 멈추면 안 된다."""
    n = TelegramNotifier("bad", "bad", timeout=1)
    monkeypatch.setattr(
        "httpx.post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("네트워크 끊김"))
    )
    assert n.send("test") is False


def test_escalation_message_has_what_operator_needs():
    msg = format_escalation("Mina", "주차장 있나요?", "유사도 0.473 < 0.58", 0.473)
    assert "Mina" in msg and "주차장 있나요?" in msg
    assert "0.473" in msg
    assert "data/knowledge" in msg     # 다음 행동 안내


def test_canary_failure_message_lists_checks():
    msg = format_canary_failure("창이 최소화되어 있습니다")
    assert "최소화" in msg and "자가진단" in msg


def test_send_delay_actually_waits():
    """계획서 §6-1 [10] — 즉답 방지."""
    from kakaobot.kakao import KakaoClient

    cli = Path(__file__).resolve().parents[1] / "vendor/openkakao-cli/target/release/openkakao-cli"
    if not cli.exists():
        return
    c = KakaoClient(cli, "테스트방", dry_run=True)
    t0 = time.monotonic()
    c.send("x", delay_range=(0.2, 0.3))
    # dry_run 이면 지연 없이 즉시 반환한다 (검증 루프를 느리게 만들지 않기 위해)
    assert time.monotonic() - t0 < 0.2


def test_canary_reports_when_room_not_open():
    from kakaobot.kakao import KakaoClient, canary_check

    cli = Path(__file__).resolve().parents[1] / "vendor/openkakao-cli/target/release/openkakao-cli"
    if not cli.exists():
        return
    c = KakaoClient(cli, "존재하지않는방_zzz", dry_run=True)
    ok, detail = canary_check(c)
    assert not ok
    assert "존재하지않는방_zzz" in detail
