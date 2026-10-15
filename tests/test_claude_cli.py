"""claude -p 어댑터 — 도구 차단이 전제 조건이므로 그것부터 검증한다."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.llm import ClaudeCliAdapter


class FakeProc:
    def __init__(self, stdout="답변", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def capture_argv(**proc_kw):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc(**proc_kw)

    return seen, fake_run


def test_tools_are_disabled_via_empty_allowlist():
    """계획서 §6-6 1항 — 도구가 열려 있으면 인젝션이 실행으로 이어진다.

    차단 목록이 아니라 빈 화이트리스트여야 한다. 차단 목록은 새 도구가
    추가되면 빠뜨리지만 빈 화이트리스트는 기본이 거부다.
    """
    seen, fake_run = capture_argv()
    with patch("subprocess.run", fake_run):
        ClaudeCliAdapter("claude-haiku-4-5").complete("sys", "user")
    argv = seen["argv"]
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""


def test_runs_outside_project_directory():
    """파일 노출면을 줄인다."""
    seen, fake_run = capture_argv()
    with patch("subprocess.run", fake_run):
        ClaudeCliAdapter("m").complete("sys", "user")
    assert seen["kwargs"]["cwd"] == "/tmp"


def test_system_prompt_passed_through():
    seen, fake_run = capture_argv()
    with patch("subprocess.run", fake_run):
        ClaudeCliAdapter("m").complete("내 시스템 지시", "질문")
    argv = seen["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "내 시스템 지시"
    assert argv[-1] == "질문"


def test_nonzero_exit_is_reported_not_raised():
    seen, fake_run = capture_argv(stdout="", stderr="boom", returncode=1)
    with patch("subprocess.run", fake_run):
        a = ClaudeCliAdapter("m").complete("s", "u")
    assert not a.ok and "boom" in a.error


def test_timeout_is_reported_not_raised():
    import subprocess

    def raises(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("subprocess.run", raises):
        a = ClaudeCliAdapter("m", timeout=1).complete("s", "u")
    assert not a.ok and "시간 초과" in a.error


def test_mode_label_is_claude_cli():
    seen, fake_run = capture_argv()
    with patch("subprocess.run", fake_run):
        a = ClaudeCliAdapter("m").complete("s", "u")
    assert a.mode == "claude-cli" and a.ok
