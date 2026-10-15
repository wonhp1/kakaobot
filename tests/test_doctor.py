"""설치 점검 (python -m kakaobot --doctor).

클론해서 세팅하는 사람이 무엇이 남았는지 알게 한다. 읽기만 하고, 메시지 내용은 출력하지 않는다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kakaobot.__main__ as main_mod
from kakaobot import config as config_mod
from kakaobot.kakao import Message

ROOM = "연습방"


def make_config(tmp_path, room=ROOM, dry_run=True):
    (tmp_path / "config").mkdir()
    cli = tmp_path / "openkakao-cli"
    cli.write_text("")
    (tmp_path / "data" / "knowledge").mkdir(parents=True)
    (tmp_path / "data" / "knowledge" / "10-수강료.md").write_text("## 수강료\n49만원", encoding="utf-8")
    cfg = tmp_path / "config" / "config.toml"
    cfg.write_text(
        f"""
[mode]
current = "claude-cli"
dry_run = {"true" if dry_run else "false"}
[room]
name = "{room}"
[cli]
openkakao_path = "{cli}"
[claude_cli]
path = "{cli}"
[notify]
channel = "none"
""",
        encoding="utf-8",
    )
    return config_mod.load(cfg)


class FakeClient:
    visible = True

    def __init__(self, cli, room, dry_run=True):
        self.room = room

    def room_visible(self):
        return self.visible

    def read(self, count=20):
        return [Message("비밀 질문 내용", "Mina", False, None), Message("답", None, True, None)]


def patch(monkeypatch, visible=True, allowed=(ROOM,), trusted=True):
    FakeClient.visible = visible
    monkeypatch.setattr("kakaobot.kakao.KakaoClient", FakeClient)
    monkeypatch.setattr("kakaobot.kakao.allowed_send_chats", lambda *a, **k: list(allowed))
    monkeypatch.setattr(main_mod, "_ax_trusted", lambda: trusted)


def test_ready_setup_passes_without_printing_messages(tmp_path, monkeypatch, capsys):
    patch(monkeypatch)
    assert main_mod.doctor(make_config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "최근 메시지 2개 읽음" in out and "준비가 됐습니다" in out
    assert "비밀 질문 내용" not in out          # 내용은 출력하지 않는다


def test_hidden_room_window_and_missing_permission_are_failures(tmp_path, monkeypatch, capsys):
    patch(monkeypatch, visible=False, trusted=False)
    assert main_mod.doctor(make_config(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "❌ 손쉬운 사용 권한" in out and "❌ 방 창" in out and "더블클릭" in out


def test_allowlist_is_optional_in_practice_mode_but_required_for_real_sends(tmp_path, monkeypatch, capsys):
    patch(monkeypatch, allowed=())
    assert main_mod.doctor(make_config(tmp_path, dry_run=True)) == 0
    assert "➖ 전송 허용 목록" in capsys.readouterr().out


def test_empty_room_name_is_reported(tmp_path, monkeypatch, capsys):
    patch(monkeypatch)
    assert main_mod.doctor(make_config(tmp_path, room="")) == 1
    assert "[room] name 이 비어 있습니다" in capsys.readouterr().out
