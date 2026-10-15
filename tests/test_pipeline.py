"""파이프라인 판정 단계 테스트. 무한 루프 방지가 핵심."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.kakao import Message
from kakaobot.kakao import diff_new
from kakaobot.pipeline import CircuitBreaker, RateLimiter, TriggerJudge


def msg(text, sender="Mina", outgoing=False, time="2026. 9. 17."):
    return Message(text=text, sender=sender, outgoing=outgoing, time=time)


def test_self_filter_blocks_outgoing():
    """무한 루프 방지 — outgoing 은 무조건 탈락."""
    v = TriggerJudge().judge(msg("가격이 얼마예요?", sender=None, outgoing=True))
    assert not v.passed and v.stage == "self_filter"


def test_self_filter_blocks_bot_prefix():
    """접두어로도 한 번 더 막는다 (이중 방어)."""
    v = TriggerJudge().judge(msg("🤖 [Sent via openkakao] 가격은 10만원입니다?"))
    assert not v.passed and v.stage == "self_filter"


def test_system_notice_blocked():
    # 시스템 공지는 시각 라벨이 없다 (time=None) — 이름 유무가 아니다.
    v = TriggerJudge().judge(msg("Mina님이 들어왔습니다.", sender=None, time=None))
    assert not v.passed and v.stage == "system_notice"


def test_no_sender_blocked():
    """발신자 불명이면 fail-closed."""
    v = TriggerJudge().judge(msg("이거 얼마예요?", sender=None))
    assert not v.passed


def test_question_mark_triggers():
    v = TriggerJudge().judge(msg("수강료가 얼마인가요?"))
    assert v.passed and v.reason == "물음표로 끝남"


def test_mention_triggers():
    j = TriggerJudge(on_question_mark=False, mention_names=["홍길동"])
    v = j.judge(msg("@홍길동 환불 규정 알려주세요"))
    assert v.passed and v.reason == "멘션"


def test_word_containing_bot_does_not_trigger():
    """실제 있었던 오탐 — '봇' 부분 일치로 '챗봇' 에도 반응했다."""
    j = TriggerJudge(mention_names=["홍길동"])
    assert not j.judge(msg("챗봇 만들고 싶어요")).passed
    assert not j.judge(msg("봇 알려줘")).passed


def test_name_without_at_is_not_a_mention():
    """이름을 부르기만 한 건 멘션이 아니다 — '홍길동 강사님 최고' 에 답하지 않는다."""
    j = TriggerJudge(on_question_mark=False, mention_names=["홍길동"])
    assert not j.judge(msg("홍길동 강사님 최고")).passed


def test_short_mention_is_not_too_short():
    """멘션은 명시적 호출이라 짧아도 받는다."""
    j = TriggerJudge(on_question_mark=False, mention_names=["홍길동"], min_length=20)
    assert j.judge(msg("@홍길동 환불")).passed


def test_mention_name_may_include_at_sign_in_config():
    j = TriggerJudge(on_question_mark=False, mention_names=["@홍길동"])
    assert j.judge(msg("@홍길동 질문 있어요")).passed


def test_plain_statement_ignored():
    v = TriggerJudge().judge(msg("오늘 강의 좋았습니다"))
    assert not v.passed and v.stage == "no_trigger"


def test_too_short_ignored():
    v = TriggerJudge(min_length=4).judge(msg("음?"))
    assert not v.passed and v.stage == "too_short"


def test_rate_limit_user_cooldown():
    r = RateLimiter(user_cooldown_secs=60, room_per_minute=99)
    assert r.check("Mina", now=0).passed
    r.commit("Mina", now=0)
    assert not r.check("Mina", now=30).passed      # 쿨다운 중
    assert r.check("Mina", now=61).passed          # 해제
    assert r.check("다른사람", now=30).passed       # 사용자별 독립


def test_rate_limit_room_cap():
    r = RateLimiter(user_cooldown_secs=0, room_per_minute=2)
    for i in range(2):
        assert r.check(f"u{i}", now=i).passed
        r.commit(f"u{i}", now=i)
    assert not r.check("u3", now=3).passed         # 방 상한 초과
    assert r.check("u3", now=70).passed            # 1분 지나면 해제


def test_circuit_breaker_trips_and_resets():
    b = CircuitBreaker(max_consecutive_errors=3)
    for _ in range(2):
        b.record_error()
    assert not b.tripped
    b.record_success()                              # 성공하면 카운터 초기화
    for _ in range(3):
        b.record_error()
    assert b.tripped
    b.reset()
    assert not b.tripped


def test_system_notice_detected_by_wording():
    """공지는 문구로 가른다. 화면 구조는 연속 메시지와 똑같다 (2026-09-20 실측)."""
    for text in ["Mina님이 들어왔습니다.", "Mina님이 나갔습니다.",
                 "환영합니다. '연습방' 오픈채팅방입니다.", "홍길동님이 방장이 되었습니다",
                 "여기까지 읽었습니다."]:
        notice = msg(text, sender=None, time=None)
        assert notice.is_system_notice, text
        assert not TriggerJudge().judge(notice).passed


def test_consecutive_message_without_time_is_not_a_notice():
    """실제 버그: 시각 라벨은 묶음의 **마지막 줄에만** 붙는다. 시각이 없다고 공지로 보면
    한 번에 올라온 질문 9개 중 7개가 발신자 불명으로 떨어진다."""
    for time in (None, "2026. 9. 20."):
        grouped = msg("환불 규정이 어떻게 되나요?", sender=None, time=time)
        assert not grouped.is_system_notice


def test_sender_inherited_when_whole_burst_has_no_labels():
    """묶음 가운데 줄은 이름도 시각도 없다. 그래도 앞사람 이름을 물려받아야 답할 수 있다."""
    from kakaobot.kakao import _inherit_senders

    raw = [
        msg("결석하면 보강 해주나요?", sender="Mina", time=None),
        msg("할인 혜택이 뭐가 있나요?", sender=None, time=None),
        msg("환불 규정이 어떻게 되나요?", sender=None, time=None),
        msg("그건 어떻게 신청해요?", sender=None, time="2026. 9. 20."),   # 묶음 마지막
    ]
    out = _inherit_senders(raw)
    assert [m.sender for m in out] == ["Mina"] * 4
    assert all(TriggerJudge().judge(m).passed for m in out)


def test_sender_inherited_for_consecutive_messages():
    """카카오톡이 생략한 이름을 앞 발화자에서 물려받는다."""
    from kakaobot.kakao import _inherit_senders

    raw = [
        msg("수강료가 얼마인가요?", sender="Mina"),
        msg("오늘 강의 좋았습니다", sender=None),      # 이름 생략
        msg("환불은 언제까지 되나요?", sender=None),    # 이름 생략
    ]
    out = _inherit_senders(raw)
    assert [m.sender for m in out] == ["Mina", "Mina", "Mina"]


def test_sender_not_inherited_across_system_notice():
    from kakaobot.kakao import _inherit_senders

    raw = [
        msg("안녕하세요", sender="Mina"),
        msg("김철수님이 들어왔습니다.", sender=None, time=None),   # 공지
        msg("이름 없는 메시지", sender=None),
    ]
    out = _inherit_senders(raw)
    assert out[2].sender is None       # 공지를 건너뛰고 물려주지 않는다


def test_diff_new_finds_only_new_messages():
    prev = [msg("a"), msg("b"), msg("c")]
    cur = [msg("b"), msg("c"), msg("d"), msg("e")]   # 창이 밀림
    assert [m.text for m in diff_new(prev, cur)] == ["d", "e"]


def test_diff_new_empty_when_unchanged():
    same = [msg("a"), msg("b")]
    assert diff_new(same, list(same)) == []


def test_diff_new_handles_repeated_question():
    """같은 사람이 같은 질문을 반복해도 두 번째를 새 메시지로 잡는다."""
    prev = [msg("얼마예요?")]
    cur = [msg("얼마예요?"), msg("얼마예요?")]
    assert len(diff_new(prev, cur)) == 1


def test_diff_new_from_empty_baseline():
    assert len(diff_new([], [msg("a"), msg("b")])) == 2


def test_mention_is_stripped_from_the_question():
    """부르는 말이 검색어에 남으면 운영자 이름이 점수를 끌어내린다 (2026-09-20 측정)."""
    j = TriggerJudge(mention_names=["홍길동"])
    assert j.strip_mentions("@홍길동 준비물 뭐 챙겨야 하나요") == "준비물 뭐 챙겨야 하나요"
    assert j.strip_mentions("준비물요 @홍길동 알려주세요") == "준비물요 알려주세요"
    assert j.strip_mentions("준비물 뭐 챙겨야 하나요?") == "준비물 뭐 챙겨야 하나요?"
    assert j.strip_mentions("@홍길동") == "@홍길동"      # 부르기만 한 경우 원문을 남긴다


# --- 물음표 없는 질문 추측 (D2 보강, 2026-09-20) --------------------------------

@pytest.mark.parametrize("text,expected", [
    ("주차 가능한지 궁금해요", "묻는 표현"),
    ("환불 되나요", "의문 어미"),
    ("수업 몇 시에 시작하나요", "의문 어미"),
    ("얼리버드 얼마인가요", "의문 어미"),
    ("할인 혜택 뭐가 있는지 알려주세요", "묻는 표현"),
    ("준비물이 뭔가", "의문 어미"),
    ("수업 자료는 어디서 받나", "의문사"),
    ("환불 규정 어떻게 되는지 문의드립니다", "묻는 표현"),
    ("이번 주 수업은 언제 하는 걸까", "의문사"),
    ("얼마에요", "의문사"),                 # 짧아도 질문이다
    ("누구세요", "의문사"),
    ("몇살이세요", "의문사"),
])
def test_question_without_question_mark_is_recognised(text, expected):
    assert TriggerJudge().looks_like_question(text) == expected


@pytest.mark.parametrize("text", [
    "오늘 날씨 좋네요",
    "감사합니다",
    "네 알겠습니다",
    "수업 잘 들었어요",
    "다들 화이팅",
])
def test_plain_chatter_is_not_treated_as_a_question(text):
    assert TriggerJudge().looks_like_question(text) == ""


def test_question_ending_rule_can_be_turned_off():
    assert TriggerJudge(on_question_ending=False).looks_like_question("환불 되나요") == ""


def test_weak_verdict_is_marked():
    from kakaobot.pipeline import Verdict

    assert Verdict(True, "trigger", "의문 어미 0.72", strength="weak").is_weak
    assert not Verdict(True, "trigger", "물음표로 끝남").is_weak


def test_diff_new_is_not_fooled_when_a_name_label_scrolls_out():
    """2026-09-21 실전 결함: 묶음의 이름표 줄이 창 위로 밀려나면 아래 줄들이 물려받던
    이름을 잃어 서명이 바뀌었다. 그러면 겹침을 못 찾아 화면 전체를 새 메시지로 봤다
    (재현: 새 메시지 0개인데 9개를 새것으로 봄). 서명은 그 줄의 이름표로 만든다."""
    from kakaobot.kakao import _inherit_senders

    def raw(text, label=""):
        return Message(text, label or None, False, None, label=label)

    burst = [raw("몇살이세요", "Mina"), raw("얼마에요"), raw("어떻게 들어가요"), raw("누구세요")]
    wide = _inherit_senders([raw("안녕하세요", "Kim")] + burst)   # 이름표 줄이 창 안에 있다
    narrow = _inherit_senders(burst[1:])                           # 이름표 줄이 창 밖으로 밀렸다
    assert [m.sender for m in wide[-3:]] == ["Mina"] * 3
    assert [m.sender for m in narrow] == [None] * 3                 # 물려받을 이름이 없다
    assert diff_new(wide, narrow) == []                              # 그래도 새 메시지는 없다

    grown = _inherit_senders(burst[1:] + [raw("[사진]", "Lee"), raw("이거 왜 이래요?")])
    assert [m.text for m in diff_new(wide, grown)] == ["[사진]", "이거 왜 이래요?"]


def test_inherited_sender_keeps_the_rows_own_label():
    from kakaobot.kakao import _inherit_senders

    out = _inherit_senders([Message("a", "Mina", False, None, label="Mina"),
                            Message("b", None, False, None, label="")])
    assert out[1].sender == "Mina" and out[1].label == ""


def test_diff_new_fails_closed_when_the_window_moved_up():
    """2026-09-21 15:38 실전: 창이 위로 밀리자 화면 전체를 새것으로 보고 옛 질문에 또 답했다."""
    bottom = [msg(t) for t in ("m5", "m6", "m7", "m8", "m9")]
    moved_up = [msg(t) for t in ("m2", "m3", "m4", "m5", "m6")]   # 직전 마지막(m9)이 안 보인다
    assert diff_new(bottom, moved_up) == []


def test_last_seen_is_found_by_its_context_among_repeats():
    """같은 문구가 여러 번 있어도 앞뒤 맥락이 가장 길게 맞는 자리를 고른다."""
    from kakaobot.kakao import last_seen_index

    prev = [msg("질문A?"), msg("확인 후 답변드릴게요.", None), msg("질문B?"), msg("확인 후 답변드릴게요.", None)]
    cur = prev[1:] + [msg("질문C?"), msg("확인 후 답변드릴게요.", None)]
    assert last_seen_index(prev, cur) == 2          # 두 번째 '확인 후…' 가 아니라 맥락이 맞는 자리
    assert [m.text for m in diff_new(prev, cur)] == ["질문C?", "확인 후 답변드릴게요."]


def test_diff_new_survives_photo_rows_flickering():
    """2026-09-21 15:49 실측: 스크롤도 새 메시지도 없이 사진 줄 2개가 3초간 목록에서 빠지고
    그만큼 위쪽 옛 메시지가 들어왔다가 되돌아왔다. 옛 방식은 이때 화면 전체를 새것으로 봤다."""
    normal = [msg(t) for t in ("오늘 날씨 좋네요", "몇살이세요", "[사진]", "[사진]", "[사진]",
                               "이거 왜이래요??", "확인 후 답변드릴게요.")]
    flickered = [msg(t) for t in ("네", "감사합니다", "오늘 날씨 좋네요", "몇살이세요", "[사진]",
                                  "이거 왜이래요??", "확인 후 답변드릴게요.")]
    assert diff_new(normal, flickered) == []
    assert diff_new(flickered, normal) == []
    assert [m.text for m in diff_new(flickered, normal + [msg("새 질문?")])] == ["새 질문?"]
