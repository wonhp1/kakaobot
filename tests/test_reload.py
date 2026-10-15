"""설정 다시 읽기 — 긴급 정지 후 재개해도 옛 질문에 답하면 안 된다.

실제 있었던 버그: 설정이 바뀔 때마다 실행 상태까지 통째로 다시 만들어서
enabled=false→true 로 풀자마자 화면의 옛 메시지 20개를 새 메시지로 보고,
레이트 리밋 이력도 사라진 상태로 옛 질문 여러 개에 답했다.
"""

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot.kakao import Message
from kakaobot.memory import ConversationMemory
from kakaobot.service import Service

CLI = ROOT / "vendor/openkakao-cli/target/release/openkakao-cli"


def write_config(path: Path, enabled: bool, room: str = "테스트방", cooldown: int = 60):
    path.write_text(f"""
[mode]
current = "claude-cli"
enabled = {"true" if enabled else "false"}
dry_run = true
[room]
name = "{room}"
[rate_limit]
user_cooldown_secs = {cooldown}
room_per_minute = 5
[cli]
openkakao_path = "{CLI}"
[logging]
dir = "logs"
""", encoding="utf-8")


def bump_mtime(path: Path):
    """mtime 해상도 때문에 같은 초 안의 수정이 무시되지 않게 한다."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


@pytest.fixture
def svc(tmp_path):
    if not CLI.exists():
        pytest.skip("openkakao-cli 가 빌드되어 있지 않다")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = cfg_dir / "config.toml"
    write_config(cfg, enabled=True)
    s = Service(cfg)
    s.cfg_path = cfg
    return s


def screen():
    return [Message(f"옛 질문 {i}?", "Mina", False, "2026. 9. 18.") for i in range(20)]


def test_emergency_stop_and_resume_keeps_baseline(svc):
    """긴급 정지 → 재개 뒤에도 직전 목록이 남아 있어야 옛 메시지를 새것으로 보지 않는다."""
    svc._last_seen = screen()

    write_config(svc.cfg_path, enabled=False); bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert not svc._cfg.enabled
    assert len(svc._last_seen) == 20

    write_config(svc.cfg_path, enabled=True); bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert svc._cfg.enabled
    assert len(svc._last_seen) == 20, "재개하면서 기준선이 비면 옛 질문 20개를 새것으로 본다"


def test_reload_keeps_rate_limit_history(svc):
    svc._rate.commit("Mina", now=time.monotonic())
    write_config(svc.cfg_path, enabled=True, cooldown=90); bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert svc._rate.user_cooldown_secs == 90          # 새 값은 반영되고
    assert not svc._rate.check("Mina").passed          # 방금 답한 사람의 쿨다운은 유지된다


def test_reload_keeps_conversation_memory(svc):
    svc._memory.add_user("Mina", "수강료 얼마예요?")
    write_config(svc.cfg_path, enabled=True, cooldown=30); bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert "수강료" in svc._memory.history("Mina")


def test_reload_keeps_breaker_error_count(svc):
    """설정을 고친다고 연속 오류 횟수가 0 으로 돌아가면 서킷 브레이커를 우회할 수 있다."""
    svc._breaker.record_error(); svc._breaker.record_error()
    write_config(svc.cfg_path, enabled=True, cooldown=30); bump_mtime(svc.cfg_path)
    svc._reload_config()
    svc._breaker.record_error()
    assert svc._breaker.tripped


def test_unchanged_file_is_not_reloaded(svc):
    svc._last_seen = screen()
    before = svc._kakao
    svc._reload_config()                 # mtime 그대로
    assert svc._kakao is before


def test_memory_configure_keeps_contents():
    m = ConversationMemory(turns=5)
    for i in range(4):
        m.add_user("Mina", f"말{i}", now=i)
    m.configure(turns=5, max_age_secs=3600)
    assert "말0" in m.history("Mina", now=5)
    m.configure(turns=1, max_age_secs=3600)          # 창이 줄면 오래된 것부터 밀린다
    h = m.history("Mina", now=5)
    assert "말3" in h and "말0" not in h
