"""한 사람이 이어 보낸 메시지는 한 질문으로 (2026-09-26 운영자 결정).

1분마다 방을 보면 "수강료 얼마예요?" "환불은요?" 가 한꺼번에 잡힌다. 따로 답하면 둘째가
1인 쿨다운에 걸려 버려졌다. 이제 한 번 확인할 때 같은 사람이 이어 보낸 메시지는 묶어 한
번에 답하고, 일부만 근거가 있으면 그것만 답한 뒤 나머지는 확인 후 답한다고 알린다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kakaobot.answering import Outcome
from kakaobot.kakao import Message
from kakaobot.llm import PARTIAL_MARK
from kakaobot.pipeline import Verdict
from kakaobot.service import Service

from test_answering import make
from test_away import AwayKakao
from test_followup import audit, svc  # noqa: F401


def q(text, sender="Kim", outgoing=False):
    return Message(text, sender, outgoing, None)


class Recorder:
    """답변 계층 대신 — 무엇을 물었는지 적어 둔다."""

    def __init__(self, outcome=None):
        self.outcome = outcome or Outcome(
            action="answer", reply="답입니다.", stage="ok"
        )
        self.calls = []

    def answer(self, question, history="", screen_text="", extra_queries=()):
        self.calls.append(
            {"question": question, "extra": tuple(extra_queries), "screen": screen_text}
        )
        return self.outcome


def poll(svc, before, after):
    """기준선(before)을 잡고, 다음 확인에 after 가 보이게 한다."""
    fake = AwayKakao(screen=before)
    svc._kakao = fake
    svc._poll_once()
    fake.screen = after
    svc._poll_once()
    return fake


BASE = q("안녕하세요", "a")


def test_two_questions_in_a_row_get_one_answer(svc):
    rec = svc._answerer = Recorder()
    a1, a2 = q("수강료가 얼마인가요?"), q("환불은 언제까지 되나요?")
    fake = poll(svc, [BASE], [BASE, a1, a2])
    assert [r["target"] for r in fake.replies] == [a2.text]  # 마지막 질문에 한 번
    assert len(rec.calls) == 1
    # 모델에는 합친 질문, 검색에는 메시지 하나하나도 따로
    assert "수강료가 얼마인가요?" in rec.calls[0]["question"]
    assert "환불은 언제까지 되나요?" in rec.calls[0]["question"]
    assert rec.calls[0]["extra"] == (a1.text, a2.text)
    ev = audit(svc)[-1]
    assert ev["parts"] == 2 and ev["anchor"] == a2.text


def test_a_lead_that_is_not_a_question_joins_the_question(svc):
    rec = svc._answerer = Recorder()
    fake = poll(svc, [BASE], [BASE, q("수강료가"), q("얼마예요?")])
    assert [r["target"] for r in fake.replies] == ["얼마예요?"]
    assert rec.calls[0]["question"].replace("\n", " ") == "수강료가 얼마예요?"


def test_the_answer_goes_to_the_last_question_not_a_trailing_thanks(svc):
    svc._answerer = Recorder()
    fake = poll(svc, [BASE], [BASE, q("수강료가 얼마인가요?"), q("감사합니다")])
    assert [r["target"] for r in fake.replies] == ["수강료가 얼마인가요?"]


def test_other_peoples_messages_split_the_burst(svc):
    svc._answerer = Recorder()
    svc._rate.user_cooldown_secs = 0  # 운영 설정과 같게 (2026-09-26)
    a1, b1, a2 = (
        q("수강료가 얼마인가요?"),
        q("주차장 있나요?", "Lee"),
        q("환불은 언제까지 되나요?"),
    )
    fake = poll(svc, [BASE], [BASE, a1, b1, a2])
    assert [r["target"] for r in fake.replies] == [a1.text, b1.text, a2.text]


def test_no_per_user_cooldown_between_checks(svc):
    """1인 1분 제한을 없앴다 — 다음 확인 때 같은 사람이 또 물어도 답한다."""
    svc._answerer = Recorder()
    svc._rate.user_cooldown_secs = 0
    a1, a2 = q("수강료가 얼마인가요?"), q("환불은 언제까지 되나요?")
    fake = poll(svc, [BASE], [BASE, a1])
    fake.screen = [BASE, a1, a2]
    svc._poll_once()
    assert [r["target"] for r in fake.replies] == [a1.text, a2.text]


def test_a_photo_in_the_burst_is_read_for_the_merged_question(svc, monkeypatch):
    rec = svc._answerer = Recorder()
    svc._photo_enabled = True
    seen = []

    def fake_read_photo(m):
        seen.append(m.text)
        return "Homebrew 설치 화면 글자", "사진 글자 16자"

    monkeypatch.setattr(svc, "_read_photo", fake_read_photo)
    photo = q("[사진]")
    ask, more = q("이거 왜 이래요?"), q("설치하다가 이렇게 떠요")
    fake = poll(svc, [BASE], [BASE, photo, ask, more])
    assert seen == [ask.text]  # 사진은 질문 말풍선을 기준으로 찾는다
    assert rec.calls[0]["screen"] == "Homebrew 설치 화면 글자"
    assert [r["target"] for r in fake.replies] == [ask.text]


def test_bursts_break_at_other_people_my_messages_and_notices():
    items = list(
        enumerate(
            [
                q("수강료가", "Kim"),
                q("얼마예요?", "Kim"),
                q("봇의 답", None, outgoing=True),
                q("그럼 환불은요?", "Kim"),
                q("Lee님이 들어왔습니다.", None),
                q("안녕하세요", "Lee"),
                q("저도 궁금해요", "Lee"),
            ]
        )
    )
    groups = [[m.text for _, m in g] for g in Service._bursts(items)]
    assert groups == [
        ["수강료가", "얼마예요?"],
        ["봇의 답"],
        ["그럼 환불은요?"],
        ["Lee님이 들어왔습니다."],
        ["안녕하세요", "저도 궁금해요"],
    ]


# --- 일부만 근거가 있을 때 -----------------------------------------------------


def test_partial_answer_keeps_the_grounded_part_and_adds_the_note():
    a, _ = make(reply=f"수강료는 49만원입니다.\n{PARTIAL_MARK}")
    o = a.answer("수강료 얼마예요? 주차장 있나요?")
    assert o.action == "partial"
    assert o.reply == "수강료는 49만원입니다.\n\n나머지는 확인 후 답변드릴게요."


def test_only_the_partial_mark_is_a_full_escalation():
    a, _ = make(reply=PARTIAL_MARK)
    o = a.answer("주차장 있나요? 수료증 나오나요?")
    assert o.action == "escalate" and o.reply == "확인 후 답변드릴게요."


def test_no_answer_alone_still_escalates():
    a, _ = make(reply="답변불가")
    assert a.answer("주차장 있나요?").action == "escalate"


def test_each_message_is_also_searched_on_its_own():
    a, _ = make()
    a.answer(
        "수강료 얼마예요? 환불은요?", extra_queries=("수강료 얼마예요?", "환불은요?")
    )
    assert a.kb.extra == ("수강료 얼마예요?", "환불은요?")


def test_partial_outcome_is_sent_and_the_operator_is_told(svc):
    sent = []
    svc._notifier.send = sent.append
    reply = "수강료는 49만원입니다.\n\n나머지는 확인 후 답변드릴게요."
    svc._answerer = Recorder(
        Outcome(
            action="partial",
            reply=reply,
            stage="ok",
            reason="여러 물음 중 일부는 근거가 없음",
        )
    )
    svc._handle(q("수강료 얼마예요? 주차장 있나요?"))
    assert svc._kakao.replies[-1]["text"] == reply
    assert len(sent) == 1 and "주차장" in sent[0]  # 나머지는 운영자가 확인하도록
    assert svc._stats.answered == 1 and svc._stats.escalated == 1
    assert audit(svc)[-1]["event"] == "partial"


def test_a_guessed_question_with_a_partial_answer_is_still_sent(svc, monkeypatch):
    """추측으로 잡은 말은 근거가 없으면 조용히 넘기지만, 일부라도 답이 있으면 보낸다."""
    svc._answerer = Recorder(
        Outcome(
            action="partial",
            reply="일부 답\n\n나머지는 확인 후 답변드릴게요.",
            stage="ok",
        )
    )
    monkeypatch.setattr(
        svc,
        "_guess_question",
        lambda *a, **k: Verdict(True, "trigger", "의문 어미 0.700", strength="weak"),
    )
    svc._handle(q("수강료 궁금한데 주차는 되나"))
    assert svc._kakao.replies


# --- 실전에서 나온 두 문제 (2026-09-26 13:40~13:44) --------------------------------


def test_a_photo_that_moves_or_vanishes_is_not_the_anchor():
    """직전 화면 끝이 사진이었는데 그 줄이 뒤로 옮겨졌다가 사라졌다. 사진을 기준으로 삼으면
    자리를 못 찾아 그사이 질문 세 개를 버렸다 — 글 메시지로만 자리를 찾는다."""
    from kakaobot.kakao import last_seen_index

    a, q1, ph = q("안녕하세요", "a"), q("9-1) 녹화본은 얼마 동안 볼 수 있어요?"), q("[사진]")
    q2, bot, q3 = q("9-2) 주차장 있나요?"), q("녹화본은 3개월입니다.", None, outgoing=True), q("10-1) 결석하면 어떻게 하나요?")
    before = [a, q1, ph]
    moved = [a, q1, q2, bot, q3, ph]
    assert moved[last_seen_index(before, moved) + 1 :] == [q2, bot, q3, ph]
    vanished = [a, q1, q2, bot, q3]
    assert vanished[last_seen_index(before, vanished) + 1 :] == [q2, bot, q3]
    # 직전 화면이 사진뿐이면 예전처럼 사진으로 맞춘다
    assert last_seen_index([ph], [ph, q2]) == 0


def test_each_line_of_a_multi_line_message_is_searched_on_its_own(svc):
    rec = svc._answerer = Recorder()
    msg = q("9-1) 녹화본은 얼마 동안 볼 수 있어요?\n9-2) 주차장 있나요?\n10-2) 수업 난이도는 어때요?")
    svc._handle(msg)
    assert rec.calls[0]["extra"] == (
        "녹화본은 얼마 동안 볼 수 있어요?", "주차장 있나요?", "수업 난이도는 어때요?",
    )


def test_multi_question_search_keeps_the_best_chunk_for_each_question():
    """점수 순으로 4개만 자르면 한 주제(환불)의 조각들이 자리를 다 차지해 '난이도'가 빠졌다."""
    from test_ocr import retriever_over

    from kakaobot.rag import Chunk

    refund = [Chunk(doc="d", heading=f"환불{k}", text=f"환불 {k}") for k in range(4)]
    level = Chunk(doc="d", heading="난이도", text="난이도")
    table = {c.views[0]: [1.0, 0.0] for c in refund}
    table[level.views[0]] = [0.0, 1.0]
    table["환불은 언제까지? 난이도는?"] = [0.9, 0.5]
    table["환불은 언제까지?"] = [0.95, 0.0]
    table["난이도는?"] = [0.0, 0.8]
    r = retriever_over([*refund, level], table)
    plain = r.search_many(["환불은 언제까지? 난이도는?"], top_k=4)
    assert "난이도" not in [h.chunk.heading for h in plain]
    diverse = r.search_many(
        ["환불은 언제까지? 난이도는?", "환불은 언제까지?", "난이도는?"], top_k=4, diverse=True
    )
    assert "난이도" in [h.chunk.heading for h in diverse]
    assert len(diverse) == 4
    assert [h.score for h in diverse] == sorted((h.score for h in diverse), reverse=True)


def test_an_unfinished_short_line_joins_the_next_line(svc):
    """"환불은" / "언제까지 되나요?" 를 따로 찾으면 '환불 신청 방법'이 끌려오고 '환불 기한'이 빠졌다."""
    lines = ["녹화본은 얼마 동안 볼 수 있어요?", "주차장 있나요?", "환불은", "언제까지 되나요?", "수강료가"]
    assert svc._question_pieces(lines) == [
        "녹화본은 얼마 동안 볼 수 있어요?", "주차장 있나요?", "환불은 언제까지 되나요?", "수강료가",
    ]


def test_numbered_items_left_empty_by_the_model_are_dropped():
    from kakaobot.answering import drop_empty_items

    text = "9-1) 3개월입니다.\n\n9-2) \n\n10-1) 녹화본으로 대체합니다.\n\n11-2)\n"
    assert drop_empty_items(text) == "9-1) 3개월입니다.\n\n10-1) 녹화본으로 대체합니다."
    assert drop_empty_items("· 한 줄 답") == "· 한 줄 답"
