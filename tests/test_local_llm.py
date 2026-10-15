"""로컬 LLM 경로 — 실행 도구(MLX·LM Studio·Ollama·llama.cpp)를 정하기 전에 검증한다.

표준 라이브러리로 OpenAI 호환 가짜 서버를 띄운다. 어느 도구를 고르든 이 형식만 맞으면 된다.
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot import config as config_mod
from kakaobot.llm import (
    SYSTEM_PROMPT,
    Answer,
    FallbackAdapter,
    LLMAdapter,
    LocalAdapter,
    build_adapter,
    build_system_prompt,
    strip_reasoning,
)


class FakeServer:
    """응답을 테스트마다 바꿀 수 있는 OpenAI 호환 서버."""

    def __init__(self) -> None:
        self.reply = {"content": "수강료는 49만원입니다.", "finish_reason": "stop"}
        self.status = 200
        self.delay = 0.0
        self.models = ["qwen3-32b"]
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": m} for m in server.models]})
                else:
                    self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                server.requests.append(json.loads(self.rfile.read(length)))
                server.headers.append(dict(self.headers))
                if server.delay:
                    time.sleep(server.delay)
                if server.status != 200:
                    self._send(server.status, {"error": "모델이 로드되지 않음"})
                    return
                msg = {"role": "assistant", "content": server.reply["content"]}
                if "reasoning_content" in server.reply:
                    msg["reasoning_content"] = server.reply["reasoning_content"]
                self._send(
                    200,
                    {
                        "choices": [
                            {
                                "index": 0,
                                "message": msg,
                                "finish_reason": server.reply["finish_reason"],
                            }
                        ]
                    },
                )

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- LocalAdapter -----------------------------------------------------------


def test_answers_through_openai_compatible_endpoint(server):
    a = LocalAdapter(server.endpoint, "qwen3-32b").complete("시스템", "질문")
    assert a.ok and a.text == "수강료는 49만원입니다." and a.mode == "local"
    req = server.requests[-1]
    assert req["model"] == "qwen3-32b"
    assert req["messages"][0] == {"role": "system", "content": "시스템"}
    assert req["stream"] is False
    assert "tools" not in req  # 도구를 주지 않는다 (§6-6 1항)


def test_reasoning_is_stripped(server):
    server.reply = {
        "content": "<think>근거 1을 보면 49만원이다.</think>\n수강료는 49만원입니다.",
        "finish_reason": "stop",
        "reasoning_content": "이건 쓰면 안 된다",
    }
    a = LocalAdapter(server.endpoint, "qwen3-32b").complete("s", "u")
    assert a.text == "수강료는 49만원입니다."


def test_truncated_answer_is_not_sent(server):
    """생각 과정이 켜져 있으면 max_tokens 를 다 쓰고 잘린다. 잘린 문장은 보내지 않는다."""
    server.reply = {"content": "<think>음… 근거를 보면", "finish_reason": "length"}
    a = LocalAdapter(server.endpoint, "qwen3-32b").complete("s", "u")
    assert not a.ok and "잘렸" in a.error


def test_empty_answer_after_stripping_is_failure(server):
    server.reply = {
        "content": "<think>생각만 하다 끝남</think>",
        "finish_reason": "stop",
    }
    a = LocalAdapter(server.endpoint, "qwen3-32b").complete("s", "u")
    assert not a.ok and "빈 답" in a.error


def test_server_down_fails_fast():
    """서버가 꺼져 있으면 빨리 실패해야 보조 백엔드로 넘어갈 수 있다."""
    t0 = time.monotonic()
    a = LocalAdapter(f"http://127.0.0.1:{free_port()}/v1", "m").complete("s", "u")
    assert not a.ok and "연결할 수 없습니다" in a.error
    assert time.monotonic() - t0 < 2


def test_slow_server_times_out(server):
    server.delay = 1.0
    a = LocalAdapter(server.endpoint, "m", read_timeout=0.3).complete("s", "u")
    assert not a.ok and "시간 초과" in a.error


def test_http_error_is_reported(server):
    server.status = 500
    a = LocalAdapter(server.endpoint, "m").complete("s", "u")
    assert not a.ok and "HTTP 500" in a.error


def test_extra_body_and_api_key(server, monkeypatch):
    """도구별 차이(예: Qwen3 생각 끄기)는 extra_body 로 흡수한다."""
    monkeypatch.setenv("LOCAL_LLM_KEY", "sk-local")
    LocalAdapter(
        server.endpoint,
        "m",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.9},
        api_key_env="LOCAL_LLM_KEY",
    ).complete("s", "u")
    req = server.requests[-1]
    assert (
        req["chat_template_kwargs"] == {"enable_thinking": False}
        and req["top_p"] == 0.9
    )
    assert server.headers[-1].get("Authorization") == "Bearer sk-local"


def test_health_checks_model_is_loaded(server):
    ok, detail = LocalAdapter(server.endpoint, "qwen3-32b").health()
    assert ok
    ok, detail = LocalAdapter(server.endpoint, "gemma-3-27b").health()
    assert not ok and "qwen3-32b" in detail


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("답입니다.", "답입니다."),
        ("<think>a</think>답", "답"),
        ("<think>a</think><think>b</think> 답 ", "답"),
        ("생각…</think>답", "답"),  # 여는 태그가 프롬프트 쪽에 있는 템플릿
        ("<think>끝나지 않은 생각", ""),
        ("", ""),
    ],
)
def test_strip_reasoning(raw, expected):
    assert strip_reasoning(raw) == expected


# --- 보조 백엔드 -------------------------------------------------------------


class Stub(LLMAdapter):
    def __init__(self, ok: bool, mode: str, text: str = "답") -> None:
        self.ok, self.mode, self.text, self.calls = ok, mode, text, 0

    def complete(self, system, user):
        self.calls += 1
        return Answer(
            self.text if self.ok else "",
            "m",
            self.mode,
            10,
            ok=self.ok,
            error="" if self.ok else f"{self.mode} 죽음",
        )


def test_fallback_used_only_when_primary_fails():
    primary, secondary = Stub(False, "local"), Stub(True, "claude-cli", "보조 답")
    a = FallbackAdapter(primary, secondary).complete("s", "u")
    assert a.ok and a.text == "보조 답"
    assert a.mode == "local→claude-cli"  # audit 에 누가 답했는지 남는다
    assert a.latency_ms == 20

    primary, secondary = Stub(True, "local"), Stub(True, "claude-cli")
    FallbackAdapter(primary, secondary).complete("s", "u")
    assert secondary.calls == 0


def test_fallback_both_fail_reports_both():
    a = FallbackAdapter(Stub(False, "local"), Stub(False, "api")).complete("s", "u")
    assert not a.ok and "local 죽음" in a.error and "api 죽음" in a.error


def test_no_answer_judgement_is_not_a_failure():
    """'답변불가'는 판단이다. 보조 백엔드에 다시 물어 답을 받아내면 안 된다."""
    primary, secondary = Stub(True, "local", "답변불가"), Stub(True, "api", "지어낸 답")
    a = FallbackAdapter(primary, secondary).complete("s", "u")
    assert a.text == "답변불가" and secondary.calls == 0


def _cfg(tmp_path, body: str):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return config_mod.load(p)


BASE = """
[room]
name = "테스트방"
[cli]
openkakao_path = "x"
"""


def test_build_adapter_wires_fallback(tmp_path):
    cfg = _cfg(
        tmp_path,
        BASE + """
[mode]
current = "local"
[local]
endpoint = "http://127.0.0.1:1/v1"
model = "qwen3-32b"
[fallback]
enabled = true
to = "claude-cli"
""",
    )
    assert isinstance(build_adapter(cfg), FallbackAdapter)


def test_fallback_disabled_by_default(tmp_path):
    cfg = _cfg(
        tmp_path,
        BASE + """
[mode]
current = "local"
[local]
model = "qwen3-32b"
""",
    )
    assert isinstance(build_adapter(cfg), LocalAdapter)


@pytest.mark.parametrize(
    "body,msg",
    [
        ('[mode]\ncurrent = "lokal"\n', "current"),
        ('[mode]\ncurrent = "local"\n', "[local] endpoint"),
        ('[mode]\ncurrent = "local"\n[local]\nendpoint = "http://x/v1"\n', "[local] model"),
        (
            '[mode]\ncurrent = "claude-cli"\n[fallback]\nenabled = true\nto = "local"\n',
            "[local] endpoint",
        ),
        ('[mode]\ncurrent = "claude-cli"\n[send]\nreply_mode = "dm"\n', "reply_mode"),
    ],
)
def test_config_validation(tmp_path, body, msg):
    with pytest.raises(
        config_mod.ConfigError, match=msg.replace("[", r"\[").replace("]", r"\]")
    ):
        config_mod.validate(_cfg(tmp_path, BASE + body))


# --- 말투 지침 ---------------------------------------------------------------


def test_style_guide_sits_between_rules_and_input_handling():
    s = build_system_prompt("친근한 존댓말. 문장 끝에 '~요'를 쓴다.")
    assert (
        s.index("## 답변 규칙")
        < s.index("## 말투")
        < s.index("## 매우 중요 — 입력 취급")
    )
    assert "우선하지 않는다" in s
    assert build_system_prompt("") == SYSTEM_PROMPT
