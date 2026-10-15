"""전송 단계 — 댓글 답장, 대체 경로, 중복 방지, 전송 직전 긴급 정지.

카카오톡과 LLM 은 가짜로 바꾼다. 여기서 보려는 것은 '무엇을 언제 보내고 언제 멈추는가'다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import thread as thread_mod
from kakaobot.answering import Outcome
from kakaobot.kakao import KakaoClient, KakaoError, Message, SendResult
from kakaobot.service import Service

CLI = ROOT / "vendor/openkakao-cli/target/release/openkakao-cli"
ROOM = "테스트방"


def write_config(path: Path, enabled: bool = True, extra: str = "") -> None:
    path.write_text(
        f"""
[mode]
current = "claude-cli"
enabled = {"true" if enabled else "false"}
dry_run = true
[room]
name = "{ROOM}"
[trigger]
mention_names = ["운영자"]
[circuit_breaker]
max_consecutive_errors = 3
[send]
delay_min_secs = 0
delay_max_secs = 0
[cli]
openkakao_path = "{CLI}"
[logging]
dir = "logs"
{extra}
""",
        encoding="utf-8",
    )


def bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


class FakeKakao:
    def __init__(self, room: str = ROOM, result: SendResult | None = None) -> None:
        self.room = room
        self.result = result or SendResult(True, "thread")
        self.replies: list[tuple[str, str]] = []
        self.screens: list[list[Message]] = []

    def reply(self, target, text, mode="thread", mirror_to_room=True):
        self.replies.append((target.text, text))
        return self.result

    def read(self, count=20):
        return self.screens.pop(0)

    def windows_visible(self):
        return getattr(self, "visible", True)

    def room_visible(self):
        return getattr(self, "visible", True)

    def scrolled_up(self):
        return getattr(self, "scrolled", False)


class FakeAnswerer:
    def __init__(self, outcome: Outcome, on_answer=None) -> None:
        self.outcome = outcome
        self.calls = 0
        self.on_answer = on_answer

    def answer(self, question, history="", screen_text="", extra_queries=()):
        self.calls += 1
        self.screen_text = screen_text
        if self.on_answer:
            self.on_answer()
        return self.outcome


ANSWER = Outcome(action="answer", reply="수강료는 49만원입니다.", stage="ok")
ESCALATE = Outcome(action="escalate", reply="확인 후 답변드릴게요.", stage="rag")
SKIP = Outcome(
    action="skip", reply="확인 후 답변드릴게요.", stage="llm", reason="LLM 오류: 시간 초과"
)


def q(text="수강료 얼마예요?", sender="Mina"):
    return Message(text, sender, False, "오후 3:00")


@pytest.fixture
def svc(tmp_path):
    if not CLI.exists():
        pytest.skip("openkakao-cli 가 빌드되어 있지 않다")
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "config.toml"
    write_config(cfg)
    s = Service(cfg)
    s.cfg_path = cfg
    s._kakao = FakeKakao()
    s._answerer = FakeAnswerer(ANSWER)
    s._ensure_answerer = lambda: s._answerer
    return s


def audit_events(svc):
    import json

    path = svc._audit.path_for()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- 서비스 ---------------------------------------------------------------


def test_answer_goes_as_thread_reply_to_the_question(svc):
    svc._handle(q())
    assert svc._kakao.replies == [("수강료 얼마예요?", "수강료는 49만원입니다.")]
    ev = audit_events(svc)[-1]
    assert ev["event"] == "answer" and ev["via"] == "thread" and ev["sent"] is True


def test_escalation_notice_is_also_replied(svc):
    """D11 '항상 안내 문구' — 답을 못 해도 질문자에게 안내 문구를 단다."""
    svc._answerer = FakeAnswerer(ESCALATE)
    svc._handle(q())
    assert svc._kakao.replies == [("수강료 얼마예요?", "확인 후 답변드릴게요.")]


def test_bot_text_is_not_read_back_as_question(svc):
    """접두어를 뗐으니 outgoing 을 못 알아보면 봇 답변을 질문으로 읽을 수 있다."""
    svc._answerer = FakeAnswerer(
        Outcome(action="answer", reply="환불은 가능한가요?", stage="ok")
    )
    svc._handle(q("환불 규정이 궁금해요?"))
    assert len(svc._kakao.replies) == 1

    # 다음 폴링에서 봇이 보낸 문구가 outgoing=False 로 읽혔다고 치자
    svc._handle(Message("환불은 가능한가요?", "홍길동", False, "오후 3:01"))
    assert len(svc._kakao.replies) == 1
    assert svc._answerer.calls == 1


def test_stop_pressed_during_llm_call_cancels_send(svc):
    """LLM 이 답을 만드는 동안(최대 2분) 긴급 정지를 누르면 보내지 않는다."""

    def operator_presses_stop():
        write_config(svc.cfg_path, enabled=False)
        bump_mtime(svc.cfg_path)

    svc._answerer = FakeAnswerer(ANSWER, on_answer=operator_presses_stop)
    fake = svc._kakao
    svc._handle(q())
    assert fake.replies == []
    ev = audit_events(svc)[-1]
    assert ev["via"] == "cancelled" and ev["sent"] is False
    assert svc._stats.cancelled == 1


def test_unverified_send_is_remembered_and_not_retried(svc):
    svc._kakao = FakeKakao(result=SendResult(False, "unverified", "확인 실패"))
    svc._handle(q())
    assert len(svc._kakao.replies) == 1
    # 나갔을 수 있으므로 봇 문구로 기억한다
    assert svc._sent.contains("수강료는 49만원입니다.")
    # 대화 맥락에는 넣지 않는다 (나갔는지 모른다)
    assert "49만원" not in svc._memory.history("Mina")


def test_answer_breaker_trips_despite_successful_polls(svc):
    """실제 있었던 버그: 폴링이 성공할 때마다 오류 횟수가 0 이 되어 LLM 이 계속
    실패해도 브레이커가 작동하지 않았다."""
    svc._answerer = FakeAnswerer(SKIP)
    for i in range(3):
        svc._handle(q(f"질문 {i}번 뭔가요?", sender=f"user{i}"))
        svc._breaker.record_success()  # 사이사이 폴링 성공
    assert svc._answer_breaker.tripped


def test_llm_failure_still_replies_with_notice_and_counts_once(svc):
    """D11 '항상 안내 문구' — LLM 이 죽어도 질문자는 안내를 받는다. 오류는 한 번만 센다."""
    svc._answerer = FakeAnswerer(SKIP)
    svc._handle(q())
    assert svc._kakao.replies == [("수강료 얼마예요?", "확인 후 답변드릴게요.")]
    assert svc._stats.errors == 1
    ev = audit_events(svc)[-1]
    assert ev["event"] == "llm_error" and ev["sent"] is True
    svc._answer_breaker.record_error()
    assert not svc._answer_breaker.tripped            # 2회 — 아직 멈추지 않는다
    svc._answer_breaker.record_error()
    assert svc._answer_breaker.tripped


def test_answer_success_resets_answer_breaker(svc):
    svc._answer_breaker.record_error()
    svc._answer_breaker.record_error()
    svc._handle(q())
    svc._answer_breaker.record_error()
    assert not svc._answer_breaker.tripped


def test_unexpected_error_in_one_message_does_not_kill_the_bot(svc):
    class Boom:
        def answer(self, *a, **k):
            raise RuntimeError("예상 못 한 오류")

    svc._answerer = Boom()
    svc._last_seen = []
    svc._kakao.screens = [[q("첫 질문 뭔가요?", "a"), q("둘째 질문 뭔가요?", "b")]]
    svc._poll_once()  # 예외가 밖으로 새지 않는다
    assert svc._stats.errors == 2
    assert [e["event"] for e in audit_events(svc)] == ["error", "error"]


def test_broken_config_pauses_bot_until_fixed(svc):
    """enabled=false 를 쓰다 오타를 내도 봇이 계속 답하면 안 된다 (fail-closed)."""
    svc.cfg_path.write_text("[mode\nenabled = false", encoding="utf-8")
    bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert svc._paused_reason.startswith("설정 파일 오류")

    svc._reload_config()  # 같은 깨진 파일 — 조용히 기다린다
    assert svc._paused_reason

    write_config(svc.cfg_path, enabled=True)
    bump_mtime(svc.cfg_path)
    bump_mtime(svc.cfg_path)
    svc._reload_config()
    assert svc._paused_reason == ""


def test_missing_baseline_is_rebuilt_without_answering(svc):
    """방을 바꾸다 기준선을 못 잡으면 다음 화면의 옛 메시지에 답하면 안 된다."""
    svc._last_seen = None
    svc._kakao.screens = [[q("옛 질문 1?", "a"), q("옛 질문 2?", "b")]]
    svc._poll_once()
    assert svc._kakao.replies == []
    assert len(svc._last_seen) == 2


def test_batch_stops_when_paused_midway(svc):
    def operator_presses_stop():
        write_config(svc.cfg_path, enabled=False)
        bump_mtime(svc.cfg_path)

    fake = FakeAnswerer(ANSWER, on_answer=operator_presses_stop)
    svc._ensure_answerer = lambda: fake        # 설정을 다시 읽으면 _answerer 는 비워진다
    svc._last_seen = []
    svc._kakao.screens = [[q("첫 질문 뭔가요?", "a"), q("둘째 질문 뭔가요?", "b")]]
    svc._poll_once()
    assert fake.calls == 1  # 둘째 질문은 LLM 도 부르지 않는다


# --- KakaoClient.reply ----------------------------------------------------


@pytest.fixture
def allowlist(tmp_path):
    p = tmp_path / "openkakao.toml"
    p.write_text(f'[safety]\nallowed_send_chats = ["{ROOM}"]\n', encoding="utf-8")
    return p


@pytest.fixture
def client(monkeypatch, allowlist):
    c = KakaoClient(Path(sys.executable), ROOM, dry_run=False, allowlist_path=allowlist)
    c.plain_calls = []

    def fake_plain(text):
        c.plain_calls.append(text)
        return SendResult(True, "plain")

    monkeypatch.setattr(c, "_send_plain", fake_plain)
    return c


def test_thread_error_falls_back_to_plain_message(client, monkeypatch):
    def fail(*a, **k):
        raise thread_mod.ThreadError("대상 메시지를 찾을 수 없습니다")

    monkeypatch.setattr(thread_mod, "reply_in_thread", fail)
    r = client.reply(q(), "답변")
    assert r.ok and r.via == "plain"
    assert "댓글 불가" in r.detail
    assert client.plain_calls == ["답변"]


def test_unconfirmed_thread_send_is_not_resent(client, monkeypatch):
    """전송 버튼을 누른 뒤 확인만 못 했으면 이미 나갔을 수 있다 — 대신 보내면 중복."""
    monkeypatch.setattr(
        thread_mod,
        "reply_in_thread",
        lambda *a, **k: thread_mod.ThreadResult(False, "전송을 확인하지 못했습니다"),
    )
    r = client.reply(q(), "답변")
    assert not r.ok and r.via == "unverified"
    assert client.plain_calls == []


def test_unexpected_thread_exception_is_not_resent(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pyobjc 오류")

    monkeypatch.setattr(thread_mod, "reply_in_thread", boom)
    r = client.reply(q(), "답변")
    assert r.via == "unverified"
    assert client.plain_calls == []


def test_plain_mode_skips_thread(client, monkeypatch):
    monkeypatch.setattr(
        thread_mod,
        "reply_in_thread",
        lambda *a, **k: pytest.fail("댓글 경로를 타면 안 된다"),
    )
    r = client.reply(q(), "답변", mode="plain")
    assert r.via == "plain"


def test_dry_run_sends_nothing(monkeypatch):
    c = KakaoClient(Path(sys.executable), ROOM, dry_run=True)
    monkeypatch.setattr(
        thread_mod,
        "reply_in_thread",
        lambda *a, **k: pytest.fail("dry-run 인데 보냈다"),
    )
    assert c.reply(q(), "답변").via == "dry-run"


@pytest.mark.parametrize(
    "stderr,via",
    [
        (
            "Error: sent the message but no additional exact-text bubble appeared",
            "unverified",
        ),
        ("Error: Return delivery failed after commit began: x", "unverified"),
        ("Error: the exact target's composer already contains a draft", "failed"),
    ],
)
def test_cli_uncertain_outcome_is_distinguished(monkeypatch, stderr, via):
    c = KakaoClient(Path(sys.executable), ROOM, dry_run=False)
    monkeypatch.setattr(c, "window_is_open", lambda: True)
    monkeypatch.setattr(c, "focus_composer", lambda: None)
    monkeypatch.setattr(
        c,
        "_run",
        lambda args, timeout=60: subprocess.CompletedProcess(args, 1, "", stderr),
    )
    assert c._send_plain("답변").via == via


def test_room_outside_allowlist_is_refused_on_thread_path_too(tmp_path, monkeypatch):
    """댓글 경로는 openkakao-cli 를 거치지 않는다. 같은 허용 목록을 여기서도 지켜야
    '테스트방에서만'이 댓글에도 적용된다."""
    allow = tmp_path / "openkakao.toml"
    allow.write_text('[safety]\nallowed_send_chats = ["연습방"]\n', encoding="utf-8")
    c = KakaoClient(Path(sys.executable), "실전방", dry_run=False, allowlist_path=allow)
    monkeypatch.setattr(thread_mod, "reply_in_thread",
                        lambda *a, **k: pytest.fail("허용 목록 밖 방에 댓글을 달았다"))
    monkeypatch.setattr(c, "_send_plain", lambda text: pytest.fail("허용 목록 밖 방에 보냈다"))
    r = c.reply(q(), "답변")
    assert not r.ok and r.via == "failed" and "허용 목록" in r.detail


def test_unreadable_allowlist_refuses_everything(tmp_path, monkeypatch):
    c = KakaoClient(Path(sys.executable), ROOM, dry_run=False,
                    allowlist_path=tmp_path / "없는파일.toml")
    monkeypatch.setattr(thread_mod, "reply_in_thread",
                        lambda *a, **k: pytest.fail("허용 목록을 못 읽었는데 보냈다"))
    assert c.reply(q(), "답변").via == "failed"


def test_messages_posted_during_pause_are_not_answered(svc, monkeypatch):
    """긴급 정지 → 재개. 정지 중에 올라온 질문에 늦게 몰아서 답하면 안 된다."""
    write_config(svc.cfg_path, enabled=True, extra="[canary]\ninterval_secs = 0")
    bump_mtime(svc.cfg_path)
    fake = svc._kakao
    answerer = FakeAnswerer(ANSWER)
    svc._ensure_answerer = lambda: answerer      # 설정을 다시 읽으면 _answerer 는 비워진다
    monkeypatch.setattr("kakaobot.service.KakaoClient", lambda *a, **k: fake)
    fake.window_is_open = lambda: True
    base = q("안녕하세요", "a")
    during = [q("정지 중 질문 1번이요?", "b"), q("정지 중 질문 2번이요?", "c")]
    after = q("재개 뒤 질문이에요?", "d")
    fake.screens = [
        [base],                       # 시작 기준선
        [base],                       # 1회: 새 것 없음
        [base, *during],              # 3회: 재개 직후 — 기준선만
        [base, *during, after],       # 4회: 재개 뒤 새 질문 — 답한다
    ]
    step = {"n": 0}

    def fake_sleep(_):
        step["n"] += 1
        if step["n"] == 1:
            write_config(svc.cfg_path, enabled=False, extra="[canary]\ninterval_secs = 0")
        elif step["n"] == 2:
            write_config(svc.cfg_path, enabled=True, extra="[canary]\ninterval_secs = 0")
        bump_mtime(svc.cfg_path)

    monkeypatch.setattr("kakaobot.service.time.sleep", fake_sleep)
    svc.run(interval=0, max_iterations=4)
    assert [target for target, _ in fake.replies] == ["재개 뒤 질문이에요?"]


def test_service_answers_the_question_without_the_mention(svc):
    fake = FakeAnswerer(ANSWER)
    svc._answerer = fake
    svc._handle(Message("@운영자 녹화본 제공되나요", "Mina", False, "오후 3:00"))
    assert fake.calls == 1
    ev = audit_events(svc)[-1]
    assert ev["question"] == "@운영자 녹화본 제공되나요"      # 원문은 그대로 남기고
    assert ev["llm_question"] == "녹화본 제공되나요"          # 모델에는 뗀 것을 준다


# --- 물음표 없는 질문 (D2 보강) ------------------------------------------------

class FakeKB:
    def __init__(self, score: float) -> None:
        self.score = score
        self.queries: list[str] = []

    def ground(self, question, extra_queries=()):
        self.queries.append(question)
        self.extra = tuple(extra_queries)
        return type("G", (), {"best_score": self.score})()


def statement(text="환불 되나요", sender="Mina"):
    return Message(text, sender, False, "오후 3:00")


def test_question_without_mark_is_answered_when_grounded(svc):
    """어미로 추측하고, 지식베이스와 가까우면 답한다."""
    kb = FakeKB(0.81)
    svc._ensure_kb = lambda: kb
    svc._handle(statement())
    assert svc._kakao.replies == [("환불 되나요", "수강료는 49만원입니다.")]
    assert audit_events(svc)[-1]["event"] == "answer"


def test_hint_uses_the_normal_threshold_but_similarity_alone_needs_more(svc):
    """어미 단서가 있으면 평소 문턱(0.58), 단서가 없으면 높은 문턱(0.70).

    2026-09-20 측정 근거: 자료에 답이 있는 질문이 0.607~0.778 에 걸쳐 있어 0.70 만 쓰면
    절반 이상을 놓친다. 반대로 잡담이 0.581 까지 올라오므로 단서 없이 0.58 은 위험하다.
    """
    svc._ensure_kb = lambda: FakeKB(0.62)
    svc._handle(statement("수업 몇 시에 시작하나요"))      # 어미 단서 있음
    assert len(svc._kakao.replies) == 1

    svc._rate._last_by_user.clear()
    svc._ensure_kb = lambda: FakeKB(0.62)
    svc._handle(statement("수업 시간 안내드립니다"))        # 단서 없음 → 0.70 미달
    assert len(svc._kakao.replies) == 1                  # 늘지 않는다

    svc._rate._last_by_user.clear()
    svc._ensure_kb = lambda: FakeKB(0.75)
    svc._handle(statement("수업 시간 안내드립니다"))        # 단서 없어도 0.70 넘으면 답한다
    assert len(svc._kakao.replies) == 2


def test_guessed_question_stays_silent_when_not_grounded(svc):
    """추측이 빗나가면 안내 문구도 보내지 않는다 — 잡담에 봇이 끼어들지 않게."""
    kb = FakeKB(0.42)
    svc._ensure_kb = lambda: kb
    svc._handle(statement("이거 어떻게 만든 건가"))
    assert svc._kakao.replies == []
    assert svc._answerer.calls == 0                 # LLM 도 부르지 않는다
    ev = json.loads((svc._filtered_log.path_for()).read_text(encoding="utf-8").splitlines()[-1])
    assert ev["stage"] == "weak_no_grounding" and "0.42" in ev["reason"]


def test_guessed_question_stays_silent_when_the_model_declines(svc):
    """근거 점수는 넘겼지만 답변 단계에서 막히면, 추측이었으므로 조용히 넘어간다."""
    svc._ensure_kb = lambda: FakeKB(0.75)
    svc._answerer = FakeAnswerer(ESCALATE)
    svc._handle(statement())
    assert svc._kakao.replies == []
    assert svc._stats.silent == 1
    assert audit_events(svc)[-1]["event"] == "silent"


def test_question_mark_still_gets_the_notice(svc):
    """확실한 질문(물음표)은 근거가 없어도 안내 문구를 받는다 (D11)."""
    svc._ensure_kb = lambda: FakeKB(0.42)
    svc._answerer = FakeAnswerer(ESCALATE)
    svc._handle(q("주차장 있나요?"))
    assert svc._kakao.replies == [("주차장 있나요?", "확인 후 답변드릴게요.")]


def test_plain_chatter_is_measured_then_ignored(svc):
    """어미에 안 걸려도 유사도는 재본다(설정 on_similarity). 멀면 조용히 넘어간다."""
    kb = FakeKB(0.31)
    svc._ensure_kb = lambda: kb
    svc._handle(statement("오늘 날씨 좋네요"))
    assert svc._kakao.replies == []
    assert kb.queries == ["오늘 날씨 좋네요"]


def test_similarity_check_can_be_turned_off(svc):
    """끄면 어미에 걸리는 말만 지식베이스와 대조한다 (메모리·CPU 절약)."""
    kb = FakeKB(0.9)
    svc._ensure_kb = lambda: kb
    svc._on_similarity = False
    svc._handle(statement("오늘 날씨 좋네요"))
    assert kb.queries == []
    svc._handle(statement("환불 되나요"))          # 어미에 걸리면 대조한다
    assert kb.queries == ["환불 되나요"]


# --- 창이 움직여도 옛 질문에 다시 답하지 않는다 (2026-09-21 15:38 실전) ---------------


def screen(*texts):
    return [q(t) if t.endswith("?") else Message(t, "Mina", False, None) for t in texts]


def test_scrolled_up_window_is_not_read(svc):
    svc._last_seen = screen("가", "나")
    svc._kakao.scrolled = True
    svc._kakao.screens = [screen("옛 질문?")]      # 읽으면 안 된다
    svc._poll_once()
    assert svc._kakao.screens and svc._answerer.calls == 0


def test_window_moved_up_then_back_answers_only_the_new_question(svc):
    """맨 아래 → 위로 밀린 화면 → 다시 맨 아래 + 새 질문. 새 질문 하나에만 답한다."""
    bottom = screen("안녕", "수강료 얼마예요?", "감사해요")
    svc._last_seen = bottom
    svc._kakao.screens = [
        screen("오래된 말", "환불 되나요?", "안녕", "수강료 얼마예요?"),   # 마지막('감사해요')이 안 보인다
        bottom + screen("녹화본 있나요?"),
    ]
    svc._poll_once()
    assert svc._answerer.calls == 0 and svc._last_seen == bottom   # 기준선을 지킨다
    svc._poll_once()
    assert svc._answerer.calls == 1                                   # 새 질문 하나에만
    assert svc._kakao.replies[-1][0] == "녹화본 있나요?"


def test_rebaseline_after_being_lost_for_too_long(svc):
    from kakaobot.service import MISALIGNED_LIMIT

    svc._last_seen = screen("가", "나")
    lost = screen("전혀 다른 화면?")
    svc._kakao.screens = [lost] * MISALIGNED_LIMIT
    for _ in range(MISALIGNED_LIMIT):
        svc._poll_once()
    assert svc._answerer.calls == 0
    assert svc._last_seen == lost and svc._misaligned == 0
