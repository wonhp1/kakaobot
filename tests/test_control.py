"""메뉴바 ↔ 서비스 약속 — 잠금, 설정 쓰기(주석 보존), 상태 표시."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import control

CONFIG = """# 카카오봇 설정
[mode]
current = "claude-cli"       # 주석은 남아야 한다
enabled = true
dry_run = true

[room]
name = "연습방"

[local]
endpoint = ""
model = ""

[send]
reply_mode = "thread"
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def test_set_config_keeps_comments_and_other_values(root):
    path = root / "config" / "config.toml"
    control.set_config_values(
        path, {("mode", "enabled"): False, ("mode", "dry_run"): False}
    )
    text = path.read_text(encoding="utf-8")
    assert "# 주석은 남아야 한다" in text and "# 카카오봇 설정" in text
    cfg = control.read_config(path)
    assert cfg["mode"] == {"current": "claude-cli", "enabled": False, "dry_run": False}
    assert cfg["room"]["name"] == "연습방"


def test_set_config_changes_mtime_so_service_reloads(root):
    path = root / "config" / "config.toml"
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    control.set_config_values(path, {("mode", "enabled"): False})
    assert path.stat().st_mtime_ns != before


def test_atomic_write_keeps_permissions(root):
    path = root / "config" / "config.toml"
    os.chmod(path, 0o600)
    control.set_config_values(path, {("mode", "enabled"): False})
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".config.toml.*"))  # 임시 파일이 남지 않는다


def test_lock_prevents_second_service(root):
    a = control.ServiceLock(root)
    a.acquire()
    try:
        assert control.running_pid(root) == os.getpid()
        with pytest.raises(control.AlreadyRunning):
            control.ServiceLock(root).acquire(retries=2, wait=0.01)
    finally:
        a.release()
    assert control.running_pid(root) is None


def test_lock_is_released_when_process_dies(root):
    """강제 종료돼도 낡은 잠금이 남지 않는다 — flock 은 프로세스와 함께 풀린다."""
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from kakaobot import control\n"
        "control.ServiceLock(%r).acquire(); print('locked', flush=True); time.sleep(30)\n"
    ) % (str(ROOT / "src"), str(root))
    p = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    try:
        assert p.stdout.readline().strip() == "locked"
        assert control.running_pid(root) == p.pid
    finally:
        p.kill()
        p.wait()
    assert control.running_pid(root) is None


def test_today_counts(root):
    audit = root / "logs" / "audit"
    filtered = root / "logs" / "filtered"
    audit.mkdir(parents=True)
    filtered.mkdir(parents=True)
    from datetime import date

    day = date.today().isoformat()
    (audit / f"{day}.jsonl").write_text(
        "\n".join(
            json.dumps({"event": e})
            for e in ["answer", "answer", "escalate", "llm_error"]
        )
        + "\n{깨진 줄\n",
        encoding="utf-8",
    )
    (filtered / f"{day}.jsonl").write_text(
        '{"event":"filtered"}\n' * 5, encoding="utf-8"
    )
    c = control.today_counts(root / "logs")
    assert (
        c["answer"] == 2
        and c["escalate"] == 1
        and c["llm_error"] == 1
        and c["filtered"] == 5
    )


# --- 메뉴바 표시 --------------------------------------------------------------


@pytest.fixture
def menu(root, monkeypatch):
    rumps = pytest.importorskip("rumps")  # noqa: F841
    from kakaobot import menubar

    monkeypatch.setattr(menubar, "allowed_send_chats", lambda: ["연습방"])
    alerts = []
    monkeypatch.setattr(
        menubar.BotMenu,
        "_alert",
        lambda self, title, message, ok="확인", cancel=None: alerts.append(title)
        or True,
    )
    m = menubar.BotMenu(root=root, config_path=root / "config" / "config.toml")
    m.timer.stop()
    m.alerts = alerts
    return m


def write_status(root, **kw):
    st = {
        "pid": os.getpid(),
        "state": "running",
        "updated_ts": time.time(),
        "stats": {"last_error": "", "last_error_at": ""},
    }
    st.update(kw)
    control.write_status(root, st)


def test_menu_shows_off_when_no_service(menu):
    assert menu.title == "🤖⚪"
    assert menu.i_service.title == "봇 시작"


def test_menu_shows_dry_run_and_real_send(menu, root):
    lock = control.ServiceLock(root)
    lock.acquire()
    try:
        write_status(root)
        menu.refresh()
        assert menu.title == "🤖🧪"
        control.set_config_values(
            root / "config" / "config.toml", {("mode", "dry_run"): False}
        )
        menu.refresh()
        assert menu.title == "🤖🟢"
    finally:
        lock.release()


def test_emergency_stop_writes_config(menu, root):
    menu.toggle_enabled(None)
    assert (
        control.read_config(root / "config" / "config.toml")["mode"]["enabled"] is False
    )
    assert menu.i_enabled.title == "▶︎ 답변 재개"


def test_real_send_needs_confirmation(menu, root, monkeypatch):
    from kakaobot import menubar

    monkeypatch.setattr(menubar.BotMenu, "_alert", lambda self, *a, **k: False)  # 취소
    menu.toggle_dry_run(None)
    assert (
        control.read_config(root / "config" / "config.toml")["mode"]["dry_run"] is True
    )


def test_local_mode_refused_until_configured(menu, root):
    """실행 도구 미정(D7) — 빈 [local] 로 바꾸면 봇이 설정 오류로 멈춘다. 메뉴에서 먼저 막는다."""
    menu._mode_setter("local")(None)
    assert (
        control.read_config(root / "config" / "config.toml")["mode"]["current"]
        == "claude-cli"
    )
    assert menu.alerts and "로컬 LLM" in menu.alerts[-1]
    assert menu._mode_items["local"].title == "로컬 LLM (미설정)"


def test_room_outside_allowlist_is_flagged(menu, root, monkeypatch):
    from kakaobot import menubar

    monkeypatch.setattr(menubar, "allowed_send_chats", lambda: ["나와의 채팅"])
    menu.refresh()
    assert "허용 목록에 없음" in menu.i_room.title
