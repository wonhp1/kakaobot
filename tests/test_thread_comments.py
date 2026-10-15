"""스레드 댓글을 '채팅방에 함께 보내기'로 방에도 올린 글 (2026-09-26 운영자 결정).

방에서 답하지 않고 '댓글 알림'으로 그 스레드를 열어 **그 스레드에서** 답한다. 예전에는 방
폴링이 먼저 보고 방에서 답했다 — 함께 보낸 글에는 댓글을 달 수 없어 일반 메시지로 나갔고,
스레드 쪽은 '방에서 이미 답했다'며 스레드를 열지 않아, 함께 보내기를 끄고 먼저 단 댓글
(실전 12번)까지 놓쳤다.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kakaobot.kakao import Message
from kakaobot.service import _thread_comment_flags
from kakaobot.thread import ThreadError, thread_comment_rows

from test_away import AwayKakao
from test_followup import (
    BOT,
    PARENT,
    ROOM,
    audit,
    baseline,
    post,
    svc,
    tm,
)  # noqa: F401
from test_thread_scroll import element

HELP = "2026. 9. 26."  # 시각 라벨의 AXHelp (날짜) — 이름표에는 없다


# --- 방 화면에서 가려내기 (실측 좌표, KakaoTalk 25.9.1 '테스트방') -----------


class WindowAX:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return self._windows

    def attr(self, e, name):
        return e.get(name)

    def walk(self, e, depth=0, max_depth=14):
        yield e
        if depth < max_depth:
            for child in e.get("AXChildren", []):
                yield from self.walk(child, depth + 1, max_depth)


def cell(
    y, h, text=None, text_y=None, text_h=16, name_y=None, button=None, time_y=None
):
    kids = []
    if button is not None:
        by, bh = button
        kids.append(element("AXButton", y=by, h=bh, AXTitle=""))
    if name_y is not None:
        kids.append(
            element("AXStaticText", y=name_y, h=18, AXValue="수강생B")
        )
    if time_y is not None:
        kids.append(
            element("AXStaticText", y=time_y, h=16, AXValue="14:28", AXHelp=HELP)
        )
    if text is not None:
        kids.append(element("AXTextArea", y=text_y, h=text_h, AXValue=text))
    return element(
        "AXRow", y=y, h=h, children=[element("AXCell", y=y, h=h, children=kids)]
    )


# 실측 그대로
ASKED = cell(
    -432, 66, "10-1) 결석하면 어떻게 하나요?", text_y=-390, name_y=-423
)  # 보통 말풍선
PARENT_ROW = cell(
    165, 64, "11-2) 언제까지 되나요?", text_y=178, button=(205, 22), time_y=187
)
BOT_MIRRORED = cell(229, 278, "· 결석하면 …", text_y=296, text_h=176, button=(483, 22))
COMMENT = cell(
    507,
    142,
    "13) 카드 할부도 되나요?",
    text_y=598,
    name_y=516,
    button=(625, 22),
    time_y=607,
)
PLAIN_BOT = cell(649, 42, "네, 신용카드는 3개월 무이자 할부가 가능합니다.", text_y=667)
PHOTO = cell(700, 200)  # 글 상자가 없는 줄


def window(*rows):
    table = element("AXTable", y=-1435, h=2200, children=list(rows))
    bar = element("AXScrollBar", AXOrientation="AXVerticalOrientation", AXValue=1.0)
    area = element("AXScrollArea", y=0, h=700, children=[table, bar])
    return element("AXWindow", children=[area], AXTitle=ROOM)


def test_room_rows_tell_thread_comments_apart():
    ax = WindowAX([window(ASKED, PARENT_ROW, BOT_MIRRORED, COMMENT, PLAIN_BOT, PHOTO)])
    assert thread_comment_rows(ROOM, 10, ax=ax) == [
        ("10-1) 결석하면 어떻게 하나요?", False),
        (
            "11-2) 언제까지 되나요?",
            False,
        ),  # 스레드가 달린 원글 — 버튼만 있고 인용 칸이 없다
        ("· 결석하면 …", True),  # 봇이 함께 보낸 답
        ("13) 카드 할부도 되나요?", True),  # 수강생이 함께 보낸 댓글
        ("네, 신용카드는 3개월 무이자 할부가 가능합니다.", False),
    ]
    # 맨 아래부터 글 줄만 센다
    assert [t for t, _ in thread_comment_rows(ROOM, 2, ax=ax)] == [
        "13) 카드 할부도 되나요?",
        "네, 신용카드는 3개월 무이자 할부가 가능합니다.",
    ]


def test_consecutive_comment_without_a_name_label_is_still_a_thread_comment():
    """같은 사람이 이어 보내 이름표가 없으면 칸 맨 위부터 잰다."""
    row = cell(0, 120, "그럼 할인은요?", text_y=62, button=(89, 22))
    assert thread_comment_rows(ROOM, 5, ax=WindowAX([window(row)])) == [
        ("그럼 할인은요?", True)
    ]


def test_a_quote_with_a_big_card_below_is_not_a_thread_comment():
    """인용 칸이 있어도 아래 칸이 링크 미리보기처럼 크면 스레드 버튼이 아니다."""
    row = cell(0, 200, "이 글 보셨어요?", text_y=62, button=(89, 80))
    assert thread_comment_rows(ROOM, 5, ax=WindowAX([window(row)])) == [
        ("이 글 보셨어요?", False)
    ]


def test_missing_room_window_is_an_error():
    with pytest.raises(ThreadError):
        thread_comment_rows(ROOM, 5, ax=WindowAX([]))


# --- ax-read 결과와 맞추기 ------------------------------------------------------


def m(text, sender="Kim", outgoing=False):
    return Message(text, sender, outgoing, None)


def test_flags_are_matched_from_the_newest_line():
    msgs = [m("a"), m("b"), m("c")]
    assert _thread_comment_flags(msgs, [("a", False), ("b", False), ("c", True)]) == [
        False,
        False,
        True,
    ]


def test_photo_placeholders_are_skipped():
    msgs = [m("a"), m("[사진]"), m("c")]
    assert _thread_comment_flags(msgs, [("a", True), ("c", False)]) == [
        True,
        False,
        False,
    ]


def test_a_line_that_arrived_after_reading_is_skipped():
    msgs = [m("a"), m("b")]
    rows = [("a", False), ("b", True), ("방금 올라온 글", False)]
    assert _thread_comment_flags(msgs, rows) == [False, True]


def test_a_mismatch_stops_matching_and_keeps_the_rest_in_the_room():
    """어긋나면 거기서 멈춘다 — 잘못 맞춰 방 질문을 빼느니 방에서 답한다."""
    assert _thread_comment_flags([m("x"), m("b")], [("a", True), ("b", True)]) == [
        False,
        True,
    ]
    assert _thread_comment_flags([m("z")], [("a", True)]) == [False]


# --- 방 폴링 → '댓글 알림' → 같은 스레드 ----------------------------------------

Q12 = "12) 그럼 할인 혜택은 뭐가 있어요?"  # 함께 보내기 끔 — 방에는 안 보인다
Q13 = "13) 카드 할부도 되나요?"  # 함께 보내기 켬 — 방에도 보인다


def room_with(svc, *after):
    """방 화면: 원글과 봇이 함께 보낸 답을 기준선으로 잡고, 다음 확인에 after 가 보이게 한다."""
    fake = AwayKakao(screen=[m(PARENT, "Mina"), m(BOT, None, outgoing=True)])
    svc._kakao = fake
    svc._poll_once()  # 기준선
    fake.screen = [*fake.screen, *after]
    return fake


def filtered(svc):
    path = svc._filtered_log.path_for()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_mirrored_comment_is_answered_in_its_thread_with_the_earlier_comment(svc):
    """실전 12·13번 (2026-09-26): 12는 함께 보내기를 끄고, 13은 켜고 같은 스레드에 달았다."""
    baseline(svc)
    fake = room_with(svc, m(Q13, "Mina"))
    fake.thread_comments = {Q13}
    post(svc, Q13, [tm(PARENT), tm(BOT, outgoing=True), tm(Q12), tm(Q13)])

    svc._poll_once()
    assert fake.replies == []  # 방에서는 답하지 않는다
    assert filtered(svc)[-1]["stage"] == "thread_comment"

    svc._poll_followups()
    assert fake.replies == [
        {
            "target": PARENT,
            "text": fake.replies[0]["text"],
            "nth": 0,
            "expect": Q13,
            "mirror": True,
        }
    ]
    question, _ = svc._answerer.calls[-1]
    assert question.split() == f"{Q12} {Q13}".split()  # 두 댓글을 한 질문으로
    ev = audit(svc)[-1]
    assert ev["origin"] == "thread" and ev["parent"] == PARENT


def test_room_questions_next_to_a_thread_comment_are_still_answered_in_the_room(svc):
    baseline(svc)
    fake = room_with(svc, m(Q13, "Mina"), m("수강료가 얼마인가요?", "Lee"))
    fake.thread_comments = {Q13}
    svc._poll_once()
    assert [r["target"] for r in fake.replies] == ["수강료가 얼마인가요?"]


def test_without_the_notice_window_thread_comments_are_answered_in_the_room(svc):
    """'댓글 알림' 창이 없으면 스레드를 알 길이 없다 — 방에서라도 답한다 (예전처럼)."""
    fake = room_with(svc, m(Q13, "Mina"))
    fake.thread_comments = {Q13}
    svc._notice_missing_logged = True
    svc._poll_once()
    assert [r["target"] for r in fake.replies] == [Q13]


def test_when_the_room_rows_cannot_be_read_messages_stay_in_the_room(svc, caplog):
    fake = room_with(svc, m(Q13, "Mina"))

    def broken(count):
        raise ThreadError("'연습방' 방이 별도 창으로 열려 있지 않습니다")

    fake.thread_comment_rows = broken
    with caplog.at_level("WARNING", logger="kakaobot.service"):
        svc._poll_once()
    assert [r["target"] for r in fake.replies] == [Q13]
    assert any(
        "스레드 댓글인지 확인하지 못했습니다" in r.getMessage() for r in caplog.records
    )


def test_earlier_comment_is_answered_even_if_the_latest_was_answered_in_the_room(svc):
    """알림에는 최신 댓글(13)만 보인다. 13을 방에서 답했어도 스레드를 열어 12에 답한다."""
    baseline(svc)
    svc._handle(m(Q13, "Mina"))  # 방에서 먼저 답함 ('댓글 알림' 창이 없던 때처럼)
    post(svc, Q13, [tm(PARENT), tm(BOT, outgoing=True), tm(Q12), tm(Q13)])
    svc._poll_followups()
    assert [r["expect"] for r in svc._kakao.replies[1:]] == [Q12]
    question, _ = svc._answerer.calls[-1]
    assert question == Q12
