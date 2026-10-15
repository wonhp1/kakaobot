"""SentLog — 접두어를 떼면서 생긴 빈자리를 메우는 두 번째 자기 메시지 판별."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.sentlog import SentLog


def test_recognizes_what_bot_sent():
    s = SentLog()
    s.add("수강료는 49만원입니다. 궁금한 점 있으신가요?", now=0)
    assert s.contains("수강료는 49만원입니다. 궁금한 점 있으신가요?", now=1)


def test_ignores_whitespace_differences():
    """카카오톡이 줄바꿈·공백을 다르게 보여줘도 같은 문장으로 본다."""
    s = SentLog()
    s.add("네, 가능합니다.\n신청서에 적어주세요.", now=0)
    assert s.contains("네, 가능합니다. 신청서에 적어주세요.", now=1)


def test_unknown_text_is_not_own():
    s = SentLog()
    s.add("수강료는 49만원입니다.", now=0)
    assert not s.contains("수강료가 얼마인가요?", now=1)


def test_expires():
    s = SentLog(max_age_secs=60)
    s.add("안녕하세요", now=0)
    assert s.contains("안녕하세요", now=59)
    assert not s.contains("안녕하세요", now=61)


def test_bot_reply_ending_with_question_would_not_loop():
    """말풍선 판정이 틀려 봇 답이 남의 메시지로 읽혀도, 물음표로 끝나는 봇 답에
    봇이 다시 답하지 않는다 — SentLog 가 막는다."""
    s = SentLog()
    reply = "환불은 개강 전 전액입니다. 더 궁금한 점 있으신가요?"
    s.add(reply, now=0)
    assert s.contains(reply, now=5)
