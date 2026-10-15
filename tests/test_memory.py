"""질문자별 대화 기억 (계획서 §6-2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.memory import ConversationMemory


def test_empty_history_for_new_asker():
    assert ConversationMemory().history("Mina") == ""


def test_split_question_is_kept_as_context():
    """물음표 없는 앞 발화가 뒤 질문의 맥락이 된다."""
    m = ConversationMemory()
    m.add_user("Mina", "저 질문이 있는데요", now=0)
    m.add_user("Mina", "환불 규정이", now=1)
    h = m.history("Mina", now=2)
    assert "저 질문이 있는데요" in h and "환불 규정이" in h


def test_bot_answers_are_in_context():
    """화면 긁기 방식에선 빠지던 봇 답변이 들어간다."""
    m = ConversationMemory()
    m.add_user("Mina", "수강료 얼마예요?", now=0)
    m.add_bot("Mina", "49만원입니다.", now=1)
    h = m.history("Mina", now=2)
    assert "Mina: 수강료 얼마예요?" in h
    assert "봇: 49만원입니다." in h


def test_askers_are_isolated():
    """오픈채팅은 여러 명이 섞인다. 남의 발화가 끼면 지시어가 흐려진다."""
    m = ConversationMemory()
    m.add_user("Mina", "수강료 얼마예요?", now=0)
    m.add_user("철수", "주차장 있나요?", now=1)
    assert "주차장" not in m.history("Mina", now=2)
    assert "수강료" not in m.history("철수", now=2)


def test_old_context_expires():
    """어제 질문에 오늘 질문이 엮이면 안 된다."""
    m = ConversationMemory(max_age_secs=1800)
    m.add_user("Mina", "수강료 얼마예요?", now=0)
    assert m.history("Mina", now=1799) != ""
    assert m.history("Mina", now=1801) == ""


def test_window_keeps_only_recent_turns():
    m = ConversationMemory(turns=2)          # 사용자+봇 2쌍 = 4개
    for i in range(10):
        m.add_user("Mina", f"말{i}", now=i)
    h = m.history("Mina", now=10)
    assert "말9" in h and "말6" in h
    assert "말5" not in h


def test_forget():
    m = ConversationMemory()
    m.add_user("Mina", "안녕", now=0)
    m.forget("Mina")
    assert m.history("Mina", now=1) == ""
