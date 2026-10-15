"""`python -m kakaobot --test-notify` — 봇과 같은 경로로 시험 알림 한 통 (2026-09-26)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import __main__ as cli
from kakaobot import notify


def config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[mode]\ncurrent = "claude-cli"\n[room]\nname = "시험방"\n'
        '[cli]\nopenkakao_path = "x"\n[notify]\nchannel = "telegram"\n',
        encoding="utf-8",
    )
    return p


def test_sends_one_message_through_the_bots_notifier(tmp_path, monkeypatch):
    sent = []

    class Fake(notify.Notifier):
        def send(self, text):
            sent.append(text)
            return True

    monkeypatch.setattr(notify, "build_notifier", lambda cfg: Fake())
    assert cli.main(["--config", str(config(tmp_path)), "--test-notify"]) == 0
    assert len(sent) == 1 and "알림 시험" in sent[0]


def test_missing_telegram_settings_fail_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notify, "build_notifier", lambda cfg: notify.NullNotifier())
    assert cli.main(["--config", str(config(tmp_path)), "--test-notify"]) == 1
    assert "setup-secrets.sh" in capsys.readouterr().err
