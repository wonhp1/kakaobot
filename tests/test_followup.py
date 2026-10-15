"""스레드 안의 추가 질문 (2026-09-24).

봇이 단 댓글 아래에 수강생이 다시 물으면, '채팅방에 함께 보내기'를 끄는 한 방 화면에는
아무것도 나타나지 않는다. 카카오톡 '댓글 알림' 방을 신호로 삼아 그 스레드를 열어 읽고
같은 스레드에 답한다. 카카오톡은 가짜 접근성 트리로 흉내 낸다.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import service as service_mod
from kakaobot import thread as thread_mod
from kakaobot.answering import Outcome
from kakaobot.followup import (
    UNREAD_ICON,
    Notice,
    NoticeTracker,
    matches,
    parent_candidates,
    pick_followup,
    read_notices,
)
from kakaobot.kakao import Message, SendResult
from kakaobot.pipeline import RateLimiter
from kakaobot.sentlog import SentLog
from kakaobot.service import Service
from kakaobot.thread import ThreadError, ThreadMessage, read_thread, reply_in_thread

ZWSP = chr(0x200B)
ROOM = "연습방"
CLI = ROOT / "vendor/openkakao-cli/target/release/openkakao-cli"


# --- 가짜 접근성 트리 ----------------------------------------------------------


def el(role, children=(), **extra):
    e = {"AXRole": role, "AXChildren": list(children)}
    e.update(extra)
    return e


class BaseAX:
    def __init__(self, windows):
        self._windows = list(windows)
        self.reads = []  # (요소 id, 속성) — 무엇을 읽었는지
        self.actions = []

    def windows(self):
        return list(self._windows)

    def attr(self, e, name):
        if not isinstance(e, dict):
            return None
        self.reads.append((e.get("id"), name))
        return e.get(name)

    def walk(self, e, depth=0, max_depth=14):
        yield e
        if depth < max_depth:
            for child in e.get("AXChildren", []):
                yield from self.walk(child, depth + 1, max_depth)

    def set_value(self, e, value):
        e["AXValue"] = value
        return 0

    def texts(self, win):
        """thread._AX.texts 와 같은 규칙."""
        table = next((e for e in self.walk(win) if e.get("AXRole") == "AXTable"), None)
        if table is None:
            return []
        return [
            c.get("AXValue")
            for e in self.walk(table)
            if e.get("AXRole") == "AXCell"
            for c in e.get("AXChildren", [])
            if c.get("AXRole") == "AXTextArea"
        ]


def room_window(texts, title=ROOM):
    """방 창. 칸 id 는 q0, q1, … (위에서부터)."""
    cells = [
        el("AXCell", [el("AXTextArea", AXValue=t)], id=f"q{i}")
        for i, t in enumerate(texts)
    ]
    table = el("AXTable", [el("AXRow", [c]) for c in cells])
    bar = el("AXScrollBar", AXOrientation="AXVerticalOrientation", AXValue=1.0)
    return el("AXWindow", [el("AXScrollArea", [table, bar])], AXTitle=title, id="room")


# --- 댓글 알림 창 ---------------------------------------------------------------


def notice_row(i, room, parent, latest, unread=False):
    kids = [
        el("AXStaticText", AXValue=f"오후 3:0{i}", id=f"time{i}"),
        el("AXStaticText", AXValue=room, id=f"room{i}"),
        el("AXScrollArea", [el("AXTextArea", AXValue=parent + ZWSP, id=f"parent{i}")]),
        el("AXScrollArea", [el("AXTextArea", AXValue=latest + ZWSP, id=f"latest{i}")]),
    ]
    if unread:
        kids.append(el("AXImage", AXDescription=UNREAD_ICON))
    return el("AXRow", [el("AXCell", kids)])


def notice_window(rows):
    return el(
        "AXWindow", [el("AXScrollArea", [el("AXTable", rows)])], AXTitle="댓글 알림"
    )


def test_reads_only_the_target_room_rows_at_the_top():
    rows = [
        notice_row(0, ROOM, "이거 왜이래요??", "그럼 윈도우는요?", unread=True),
        notice_row(1, "다른 방", "남의 원글", "남의 댓글"),
        notice_row(2, ROOM, "수강료가 얼마인가요…", "수강료는 49만원입니다…"),
        notice_row(3, ROOM, "맨 위 3줄 밖", "읽지 않는다"),
    ]
    ax = BaseAX([notice_window(rows)])
    got = read_notices(ROOM, top=3, ax=ax)
    assert got == [
        Notice(ROOM, "이거 왜이래요??", "그럼 윈도우는요?", True),
        Notice(ROOM, "수강료가 얼마인가요…", "수강료는 49만원입니다…", False),
    ]
    # 다른 방 알림은 방 이름만 보고 내용은 읽지 않는다
    read_ids = {i for i, name in ax.reads if name == "AXValue"}
    assert not {"parent1", "latest1"} & read_ids
    assert "room1" in read_ids
    # 맨 위 top 줄 밖은 건드리지 않는다
    assert not {"room3", "parent3", "latest3"} & read_ids


def test_missing_notice_window_is_none():
    assert read_notices(ROOM, ax=BaseAX([room_window(["안녕"])])) is None


@pytest.mark.parametrize(
    "clipped,full,ok",
    [
        ("이거 왜이래요…", "이거 왜이래요?? 화면이 이상해요", True),
        ("이거 왜이래요??", "이거 왜이래요??", True),
        ("이거 왜", "이거 왜이래요??", False),  # '…' 이 없으면 전체가 같아야 한다
        ("첫 줄…", "첫 줄\n둘째 줄", True),  # 여러 줄 글은 공백으로 본다
        ("수강료 얼마예요?" + ZWSP, "수강료 얼마예요?", True),
        ("…", "아무 글", False),  # 앞부분이 비면 맞추지 않는다
        ("", "아무 글", False),
    ],
)
def test_matches_clipped_text(clipped, full, ok):
    assert matches(clipped, full) is ok


def test_notice_tracker_first_read_is_a_baseline():
    t = NoticeTracker()
    old = Notice(ROOM, "원글", "옛 댓글", False)
    new = Notice(ROOM, "원글", "새 댓글", True)
    assert t.fresh([old]) == []  # 기동 때 있던 알림에는 반응하지 않는다
    assert t.fresh([new, old]) == [new]
    assert t.fresh([new, old]) == []  # 한 번만
    t.reset()
    assert t.fresh([Notice(ROOM, "원글", "또 새 댓글", True)]) == []


def test_notice_tracker_forgets_the_oldest_beyond_capacity():
    t = NoticeTracker(capacity=2)
    t.fresh([])
    for k in range(3):
        t.fresh([Notice(ROOM, "원글", f"댓글 {k}", False)])
    assert t.fresh([Notice(ROOM, "원글", "댓글 0", False)]) != []


def test_parent_candidates_newest_first_with_duplicates():
    room = [
        "이거 왜이래요??",
        "다른 말",
        "이거 왜이래요??",
        "이거 왜이래요?? 추가로",
        "마지막 말",
    ]
    assert parent_candidates(room, "이거 왜이래요…") == [
        ("이거 왜이래요?? 추가로", 0),
        ("이거 왜이래요??", 0),
        ("이거 왜이래요??", 1),  # 같은 본문의 두 번째로 최근 것
    ]
    assert parent_candidates(room, "이거 왜이래요??") == [
        ("이거 왜이래요??", 0),
        ("이거 왜이래요??", 1),
    ]
    assert parent_candidates(room, "이거 왜이래요…", limit=1) == [
        ("이거 왜이래요?? 추가로", 0)
    ]
    assert parent_candidates(room, "없는 글") == []


# --- 스레드에서 추가 질문 고르기 ---------------------------------------------


PARENT = "이거 왜이래요??"
BOT = "화면에 보이는 명령어를 그대로 복사해서 터미널에 붙여넣고 엔터를 누르면 됩니다."


def tm(text, sender="Mina", outgoing=False):
    return ThreadMessage(text, outgoing, None if outgoing else sender)


def test_picks_the_comments_after_the_bots_answer():
    msgs = [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")]
    fu, why = pick_followup(PARENT, 0, msgs, {BOT}.__contains__)
    assert why == ""
    assert fu.asker == "Mina" and fu.lines == ("그럼 윈도우는요?",)
    assert fu.question == "그럼 윈도우는요?"
    assert f"Mina: {PARENT}" in fu.history and f"봇: {BOT}" in fu.history
    assert "그럼 윈도우는요?" not in fu.history  # 질문은 맥락이 아니라 질문으로 간다


def test_split_question_lines_are_joined():
    msgs = [
        tm(PARENT),
        tm(BOT, outgoing=True),
        tm("아 그리고"),
        tm("윈도우는 어떻게 해요?"),
    ]
    fu, _ = pick_followup(PARENT, 0, msgs, {BOT}.__contains__)
    assert fu.question == "아 그리고\n윈도우는 어떻게 해요?"


def test_only_the_last_askers_comments():
    msgs = [
        tm(PARENT),
        tm(BOT, outgoing=True),
        tm("저도요", "Mina"),
        tm("맥은요?", "Kim"),
        tm("m1 이에요", "Kim"),
    ]
    fu, _ = pick_followup(PARENT, 0, msgs, {BOT}.__contains__)
    assert fu.asker == "Kim" and fu.lines == ("맥은요?", "m1 이에요")


@pytest.mark.parametrize(
    "msgs,reason",
    [
        ([tm(PARENT), tm("저도 궁금해요")], "운영자 댓글이 없는"),
        ([tm(PARENT), tm(BOT, outgoing=True)], "새 댓글 없음"),
        # 봇이 답한 뒤 운영자가 직접 이어 답했다 — 봇은 끼어들지 않는다
        (
            [
                tm(PARENT),
                tm(BOT, outgoing=True),
                tm("그럼요?"),
                tm("제가 직접 볼게요", outgoing=True),
                tm("네 감사해요!"),
            ],
            "운영자가 직접",
        ),
    ],
)
def test_nothing_to_answer(msgs, reason):
    fu, why = pick_followup(PARENT, 0, msgs, {BOT}.__contains__)
    assert fu is None and reason in why


# --- 봇이 보낸 글 · 레이트 리밋 -------------------------------------------------


def test_sentlog_matches_clipped_bot_text():
    log = SentLog()
    log.add(BOT)
    assert log.matches_clipped("화면에 보이는 명령어를…")
    assert log.matches_clipped(BOT + ZWSP)
    assert not log.matches_clipped("화면에 안 보이는…")
    assert not log.matches_clipped("…")
    assert not log.matches_clipped("화면에 보이는")  # '…' 없이 앞부분만이면 다른 글이다


def test_check_room_ignores_the_user_cooldown_but_not_the_room_cap():
    r = RateLimiter(user_cooldown_secs=60, room_per_minute=2)
    r.commit("Mina", now=0)
    assert not r.check("Mina", now=1).passed
    assert r.check_room(now=1).passed
    r.commit("Kim", now=2)
    assert not r.check_room(now=3).passed
    assert r.check_room(now=61.5).passed


# --- 스레드 창 열기 ----------------------------------------------------------


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def thread_window(wid, texts):
    cells = [el("AXCell", [el("AXTextArea", AXValue=t)]) for t in texts]
    table = el("AXTable", [el("AXRow", [c]) for c in cells])
    # 입력창은 부모가 스크롤 영역인 글 칸이다 (thread.reply_in_thread 가 AXParent 로 가린다)
    composer = el("AXTextArea", AXValue="", id=f"composer-{wid}")
    composer_area = el("AXScrollArea", [composer])
    composer["AXParent"] = composer_area
    return el(
        "AXWindow",
        [
            el("AXScrollArea", [table]),
            el("AXCheckBox", AXTitle="채팅방에 함께 보내기", AXValue=1),
            composer_area,
            el("AXButton", AXTitle="전송", send_of=wid),
        ],
        AXTitle="",
        id=wid,
        AXCloseButton=el("AXButton", close_of=wid),
    )


def aux_window(wid):
    return el(
        "AXWindow",
        [],
        AXTitle="댓글 달린 메시지",
        id=wid,
        AXCloseButton=el("AXButton", close_of=wid),
    )


class ThreadAX(BaseAX):
    """방 창 + 운영자가 열어 둔 창들. 원글의 '댓글'을 고르면 새 스레드 창과 부속 창이 뜬다."""

    def __init__(self, room_texts, threads, existing=(), open_new=True):
        self.room = room_window(room_texts)
        super().__init__([self.room, *existing])
        self.threads = threads  # 칸 id → 스레드 글 목록
        self.open_new = open_new
        self.menu_for = None
        self.opened = 0

    def window(self, wid):
        return next((w for w in self._windows if w.get("id") == wid), None)

    def perform(self, e, action):
        self.actions.append((e.get("id") or e.get("AXTitle") or e["AXRole"], action))
        kids = self.room["AXChildren"]
        if action == "AXShowMenu":
            self.menu_for = e["id"]
            kids.append(el("AXMenu", [el("AXMenuItem", AXTitle="댓글")]))
        elif action == "AXPick":
            self.room["AXChildren"] = [c for c in kids if c["AXRole"] != "AXMenu"]
            if self.open_new:
                self.opened += 1
                n = self.opened
                self._windows.append(
                    thread_window(f"new{n}", self.threads[self.menu_for])
                )
                self._windows.append(aux_window(f"aux-new{n}"))
        elif action == "AXCancel":
            self.room["AXChildren"] = [c for c in kids if c["AXRole"] != "AXMenu"]
        elif action == "AXPress" and "close_of" in e:
            self._windows = [w for w in self._windows if w.get("id") != e["close_of"]]
        elif action == "AXPress" and "send_of" in e:
            win = self.window(e["send_of"])
            composer = next(
                x for x in self.walk(win) if str(x.get("id", "")).startswith("composer")
            )
            table = next(x for x in self.walk(win) if x["AXRole"] == "AXTable")
            table["AXChildren"].append(
                el(
                    "AXRow",
                    [el("AXCell", [el("AXTextArea", AXValue=composer["AXValue"])])],
                )
            )
            composer["AXValue"] = ""
        return 0


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(thread_mod, "time", FakeTime())


def ids(ax):
    return [w.get("id") for w in ax.windows()]


def test_uses_the_new_thread_window_and_leaves_the_operators_open_windows(clock):
    """운영자가 스레드 창을 열어 두고 쓰는 중일 수 있다. 봇은 자기가 연 창만 쓰고 닫는다."""
    mine = thread_window("operator-thread", ["운영자가 보던 원글", "쓰던 댓글"])
    ax = ThreadAX(
        [PARENT],
        {"q0": [PARENT, BOT, "그럼 윈도우는요?"]},
        existing=[mine, aux_window("operator-aux")],
    )
    got = read_thread(ROOM, PARENT, ax=ax)
    assert [m.text for m in got] == [PARENT, BOT, "그럼 윈도우는요?"]
    assert ids(ax) == [
        "room",
        "operator-thread",
        "operator-aux",
    ]  # 봇이 연 두 창만 닫힘


def test_nth_opens_the_older_duplicate(clock):
    ax = ThreadAX(
        [PARENT, "다른 말", PARENT], {"q0": [PARENT, "옛 스레드"], "q2": [PARENT]}
    )
    got = read_thread(ROOM, PARENT, nth=1, ax=ax)
    assert ("q0", "AXShowMenu") in ax.actions
    assert [m.text for m in got] == [PARENT, "옛 스레드"]


def test_no_new_window_is_an_error_and_nothing_is_closed(clock):
    """같은 스레드를 운영자가 이미 열어 두면 새 창이 생기지 않는다 — 손대지 않고 멈춘다."""
    mine = thread_window("operator-thread", [PARENT, BOT])
    ax = ThreadAX([PARENT], {"q0": [PARENT, BOT]}, existing=[mine], open_new=False)
    with pytest.raises(ThreadError):
        read_thread(ROOM, PARENT, ax=ax)
    assert ids(ax) == ["room", "operator-thread"]
    assert not any(a == "AXPress" for _, a in ax.actions)


def test_reply_goes_into_the_thread_that_has_the_followup(clock, monkeypatch):
    ax = ThreadAX(
        [PARENT, PARENT], {"q0": [PARENT, BOT, "그럼 윈도우는요?"], "q1": [PARENT, BOT]}
    )
    monkeypatch.setattr(thread_mod, "_AX", lambda: ax)
    r = reply_in_thread(
        ROOM, PARENT, "윈도우는 이렇게 합니다.", nth=1, expect="그럼 윈도우는요?"
    )
    assert r.ok and r.thread_after[-1] == "윈도우는 이렇게 합니다."
    assert ("q0", "AXShowMenu") in ax.actions
    assert ids(ax) == ["room"]


def test_reply_refuses_a_thread_without_the_followup(clock, monkeypatch):
    ax = ThreadAX([PARENT], {"q0": [PARENT, BOT]})
    monkeypatch.setattr(thread_mod, "_AX", lambda: ax)
    with pytest.raises(ThreadError, match="추가 질문이 이 스레드에 없습니다"):
        reply_in_thread(ROOM, PARENT, "답", expect="그럼 윈도우는요?")
    assert not any(a == "AXPress" and w == "전송" for w, a in ax.actions)
    assert ids(ax) == ["room"]


# --- 서비스 흐름 --------------------------------------------------------------


ANSWER = Outcome(
    action="answer", reply="윈도우는 설치 파일을 받아 실행하면 됩니다.", stage="ok"
)


def write_config(path: Path, extra: str = "", enabled: bool = True) -> None:
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
on_similarity = false
[send]
delay_min_secs = 0
delay_max_secs = 0
[cli]
openkakao_path = "{CLI}"
[logging]
dir = "logs"
[canary]
interval_secs = 0
[followup]
enabled = true
top = 5
every_polls = 1
max_answers_per_thread = 3
{extra}
""",
        encoding="utf-8",
    )


class FakeKakao:
    def __init__(self):
        self.room = ROOM
        self.result = SendResult(True, "thread")
        self.replies = []
        self.scrolled = False

    def reply(
        self, target, text, mode="thread", mirror_to_room=True, nth=0, expect=None
    ):
        self.replies.append(
            {
                "target": target.text,
                "text": text,
                "nth": nth,
                "expect": expect,
                "mirror": mirror_to_room,
            }
        )
        return self.result

    def windows_visible(self):
        return getattr(self, "visible", True)

    def room_visible(self):
        return getattr(self, "visible", True)

    def scrolled_up(self):
        return self.scrolled

    def read(self, count=20):
        return [Message(t, "Mina", False, None) for t in self.room_texts]

    def thread_comment_rows(self, count):
        """방 창의 글 줄과 '함께 보낸 스레드 댓글' 표시. 댓글로 볼 글은 thread_comments 에 넣는다.
        화면은 read 와 같다 (AwayKakao 는 screen). 사진·파일 자리표시는 창에서 글 줄이 아니다."""
        screen = getattr(self, "screen", None)
        texts = (
            [m.text for m in screen] if screen is not None else list(self.room_texts)
        )
        marks = getattr(self, "thread_comments", set())
        rows = [(t, t in marks) for t in texts if t not in ("[사진]", "[파일]")]
        return rows[-count:]


class FakeAnswerer:
    def __init__(self, outcome=ANSWER):
        self.outcome = outcome
        self.calls = []

    def answer(self, question, history="", screen_text="", extra_queries=()):
        self.calls.append((question, history))
        return self.outcome


@pytest.fixture
def svc(tmp_path, monkeypatch):
    if not CLI.exists():
        pytest.skip("openkakao-cli 가 빌드되어 있지 않다")
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "config.toml"
    write_config(cfg)
    s = Service(cfg)
    s.cfg_path = cfg
    s._kakao = FakeKakao()
    s._answerer = FakeAnswerer()
    s._ensure_answerer = lambda: s._answerer
    s._bot_replies.add(BOT)

    # 카카오톡 대신: 알림 목록, 방 화면의 글, 스레드 내용
    s.fake = {"notices": [], "room": [PARENT], "threads": {}, "opened": []}
    s._kakao.room_texts = s.fake["room"]

    def fake_read_notices(room, top=5):
        return s.fake["notices"]

    def fake_read_thread(room, parent, target_sender=None, nth=0, ax=None):
        s.fake["opened"].append((parent, nth))
        try:
            return s.fake["threads"][(parent, nth)]
        except KeyError:
            raise ThreadError("스레드 창이 새로 열리지 않았습니다") from None

    monkeypatch.setattr(service_mod, "read_notices", fake_read_notices)
    monkeypatch.setattr(service_mod, "read_thread", fake_read_thread)
    return s


def audit(s):
    path = s._audit.path_for()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def post(s, latest, thread, parent=PARENT, nth=0):
    """수강생이 스레드에 댓글을 달았다 — 알림 맨 위가 바뀌고 스레드 내용이 바뀐다."""
    s.fake["notices"] = [Notice(ROOM, parent, latest, True)]
    s.fake["threads"][(parent, nth)] = thread


def baseline(s):
    s.fake["notices"] = [Notice(ROOM, PARENT, "화면에 보이는 명령어를…", False)]
    s._poll_followups()


def test_first_read_is_only_a_baseline(svc):
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert svc.fake["opened"] == [] and svc._kakao.replies == []


def test_followup_is_answered_in_the_same_thread_with_its_context(svc):
    baseline(svc)
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert svc._kakao.replies == [
        {
            "target": PARENT,
            "text": ANSWER.reply,
            "nth": 0,
            "expect": "그럼 윈도우는요?",
            "mirror": True,  # '방에도 함께' (2026-09-24 결정)
        }
    ]
    question, history = svc._answerer.calls[0]
    assert question == "그럼 윈도우는요?"
    assert f"봇: {BOT}" in history
    ev = audit(svc)[-1]
    assert (
        ev["event"] == "answer" and ev["origin"] == "thread" and ev["parent"] == PARENT
    )
    # 방금 단 답은 봇의 답으로 기억한다. 추가 질문에 답한 직후라 한 번은 다시 열어 보지만
    # (답하는 사이 달린 댓글), 새 댓글이 없으면 아무것도 보내지 않는다
    svc.fake["opened"].clear()
    post(
        svc,
        ANSWER.reply[:12] + "…",
        [
            tm(PARENT),
            tm(BOT, outgoing=True),
            tm("그럼 윈도우는요?"),
            tm(ANSWER.reply, outgoing=True),
        ],
    )
    svc._poll_followups()
    assert svc.fake["opened"] == [(PARENT, 0)]
    assert len(svc._kakao.replies) == 1


def test_comment_posted_while_the_bot_was_answering_is_not_lost(svc):
    """봇이 답을 만드는 사이 같은 사람이 더 달면, 봇의 답이 그 댓글 **뒤에** 붙는다.
    알림의 최신 댓글은 봇의 답이 되므로 알림만 보면 놓친다 — 한 번 더 열어 찾는다."""
    baseline(svc)
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert [r["expect"] for r in svc._kakao.replies] == ["그럼 윈도우는요?"]
    # 답이 올라갈 때 스레드: 원글 · 봇 · 질문 · (답하는 사이 달린) 추가 · 봇의 답
    post(
        svc,
        ANSWER.reply[:12] + "…",
        [
            tm(PARENT),
            tm(BOT, outgoing=True),
            tm("그럼 윈도우는요?"),
            tm("윈도우 11도 되나요?"),
            tm(ANSWER.reply, outgoing=True),
        ],
    )
    svc._poll_followups()
    assert [r["expect"] for r in svc._kakao.replies] == [
        "그럼 윈도우는요?",
        "윈도우 11도 되나요?",
    ]
    question, history = svc._answerer.calls[-1]
    assert question == "윈도우 11도 되나요?"
    assert "윈도우 11도 되나요?" not in history and "그럼 윈도우는요?" in history


def test_mid_answer_gap_uses_only_unanswered_lines():
    msgs = [
        tm(PARENT),
        tm(BOT, outgoing=True),
        tm("A 질문?"),
        tm("B 추가"),
        tm("봇 답 2", outgoing=True),
    ]
    bot = {BOT, "봇 답 2"}.__contains__
    answered = {("Mina", "A 질문?")}
    fu, _ = pick_followup(PARENT, 0, msgs, bot, lambda s, t: (s, t) in answered)
    assert fu.lines == ("B 추가",)
    # 답한 기록을 모르면(was_answered 없음) 앞 구간은 보지 않는다
    assert pick_followup(PARENT, 0, msgs, bot)[0] is None
    # 다 답했으면 없음
    answered.add(("Mina", "B 추가"))
    assert (
        pick_followup(PARENT, 0, msgs, bot, lambda s, t: (s, t) in answered)[0] is None
    )


def test_the_bots_own_latest_comment_does_not_open_the_thread(svc):
    baseline(svc)
    post(svc, "화면에 보이는 명령어를 그대로…", [])
    svc._poll_followups()
    assert svc.fake["opened"] == []


def test_operator_answering_by_hand_is_left_alone(svc):
    baseline(svc)
    post(
        svc,
        "네 감사해요!",
        [
            tm(PARENT),
            tm(BOT, outgoing=True),
            tm("그럼요?"),
            tm("제가 직접 볼게요", outgoing=True),
            tm("네 감사해요!"),
        ],
    )
    svc._poll_followups()
    assert svc._kakao.replies == []


def test_older_duplicate_parent_is_found_by_the_latest_comment(svc):
    svc.fake["room"][:] = [PARENT, "다른 말", PARENT]
    baseline(svc)
    svc.fake["threads"][(PARENT, 0)] = [tm(PARENT), tm(BOT, outgoing=True)]
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
        nth=1,
    )
    svc._poll_followups()
    assert svc.fake["opened"] == [(PARENT, 0), (PARENT, 1)]
    assert svc._kakao.replies[0]["nth"] == 1


def test_parent_scrolled_out_of_the_room_is_recorded(svc):
    baseline(svc)
    post(svc, "그럼 윈도우는요?", [], parent="오래전 원글")
    svc._poll_followups()
    assert svc._kakao.replies == []
    assert audit(svc)[-1]["event"] == "followup_skipped"


def test_user_cooldown_does_not_block_a_thread_followup(svc):
    """봇의 답을 읽고 곧바로 되묻는 것은 자연스럽다 — 1인 60초 쿨다운을 걸지 않는다."""
    baseline(svc)
    svc._rate.commit("Mina")  # 방금 방에서 답을 받았다
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert len(svc._kakao.replies) == 1


def test_room_cap_still_applies(svc):
    baseline(svc)
    svc._rate.room_per_minute = 1
    svc._rate.commit("Kim")
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert svc._kakao.replies == []
    assert audit(svc)[-1]["event"] == "rate_limited"


def test_at_most_three_answers_per_thread(svc):
    baseline(svc)
    for k in range(4):
        post(
            svc,
            f"추가 질문 {k}번이요?",
            [tm(PARENT), tm(BOT, outgoing=True), tm(f"추가 질문 {k}번이요?")],
        )
        svc._poll_followups()
    assert len(svc._kakao.replies) == 3
    ev = audit(svc)[-1]
    assert ev["event"] == "rate_limited" and "스레드 하나에" in ev["reason"]


def test_followup_mirrored_to_the_room_is_not_answered_twice(svc):
    """'채팅방에 함께 보내기'를 켜고 물으면 같은 글이 방에도 뜬다."""
    baseline(svc)
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    svc._handle(Message("그럼 윈도우는요?", "Mina", False, "오후 3:01"))
    assert len(svc._kakao.replies) == 1


def test_room_answer_first_is_not_repeated_in_the_thread(svc):
    """방에서 먼저 답한 글은 스레드에서 다시 답하지 않는다. 그래도 스레드는 연다 — 알림에는
    최신 댓글 하나만 보여, 그 앞의 다른 추가 질문을 놓치지 않으려면 열어 봐야 한다 (2026-09-26)."""
    baseline(svc)
    svc._handle(Message("그럼 윈도우는요?", "Mina", False, "오후 3:01"))
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert len(svc._kakao.replies) == 1 and svc.fake["opened"] == [(PARENT, 0)]


def test_scrolled_up_room_waits(svc):
    baseline(svc)
    svc._kakao.scrolled = True
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert svc.fake["opened"] == []
    svc._kakao.scrolled = False  # 내려오면 그때 본다
    svc._poll_followups()
    assert len(svc._kakao.replies) == 1


def test_missing_notice_window_is_reported_once_and_rebaselined(svc, caplog):
    baseline(svc)
    svc.fake["notices"] = None
    with caplog.at_level("WARNING", logger="kakaobot.service"):
        svc._poll_followups()
        svc._poll_followups()
    assert sum("댓글 알림" in r.getMessage() for r in caplog.records) == 1
    # 창이 다시 열리면 그때 화면은 기준선 — 그 사이 댓글에 늦게 답하지 않는다
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._poll_followups()
    assert svc._kakao.replies == []


def test_disabled_does_nothing(svc):
    svc._followup_enabled = False
    svc.fake["notices"] = None  # 읽으면 경고가 난다
    svc._poll_followups()
    assert not svc._notice_missing_logged


def test_bot_replies_survive_a_restart_through_the_audit_log(svc):
    d = svc._audit.directory
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date.today().isoformat()}.jsonl").write_text(
        json.dumps(
            {"event": "answer", "via": "thread", "reply": "재시작 전 답"},
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {"event": "answer", "via": "dry-run", "reply": "보내지 않은 답"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    svc._load_bot_replies()
    assert svc._bot_replies.contains("재시작 전 답")
    assert not svc._bot_replies.contains("보내지 않은 답")


def test_followups_posted_during_a_pause_are_not_answered(svc, monkeypatch):
    """긴급 정지 → 재개. 정지 중에 달린 추가 질문에 늦게 답하면 안 된다 (방 메시지와 같은 규칙)."""
    import os

    fake = svc._kakao
    fake.window_is_open = lambda: True
    fake.read = lambda count=20: [
        Message(PARENT, "Mina", False, None),
        Message("안녕하세요", "a", False, None),
    ]
    monkeypatch.setattr("kakaobot.service.KakaoClient", lambda *a, **k: fake)
    answerer = svc._answerer
    svc._ensure_answerer = lambda: answerer
    svc.fake["notices"] = [Notice(ROOM, PARENT, "화면에 보이는 명령어를…", False)]

    def rewrite(enabled):
        write_config(svc.cfg_path, enabled=enabled)
        st = svc.cfg_path.stat()
        os.utime(svc.cfg_path, (st.st_atime, st.st_mtime + 5 + len(steps)))

    steps = []

    def fake_sleep(_):
        steps.append(1)
        if len(steps) == 1:
            rewrite(False)  # 정지
        elif len(steps) == 2:  # 정지 중 추가 질문, 그리고 재개
            post(
                svc,
                "정지 중 추가 질문이요?",
                [tm(PARENT), tm(BOT, outgoing=True), tm("정지 중 추가 질문이요?")],
            )
            rewrite(True)
        elif len(steps) == 3:  # 재개 뒤 추가 질문 — 답한다
            post(
                svc,
                "재개 뒤 추가 질문이요?",
                [
                    tm(PARENT),
                    tm(BOT, outgoing=True),
                    tm("정지 중 추가 질문이요?"),
                    tm("재개 뒤 추가 질문이요?"),
                ],
            )

    monkeypatch.setattr("kakaobot.service.time.sleep", fake_sleep)
    svc.run(interval=0, max_iterations=4)
    assert [r["expect"] for r in fake.replies] == ["재개 뒤 추가 질문이요?"]
    # 정지 중 댓글만으로는 답하지 않는다. 재개 뒤 새 댓글이 오면 봇의 답 뒤 같은 사람의
    # 댓글은 한 질문으로 묶는다 — 아직 아무도 답하지 않은 말이라 함께 본다
    question, _ = answerer.calls[-1]
    # (모델에 갈 때 멘션을 떼며 줄바꿈은 공백이 된다 — 방 질문과 같다)
    assert question == "정지 중 추가 질문이요? 재개 뒤 추가 질문이요?"


def test_turning_the_feature_back_on_rebaselines(svc, monkeypatch):
    """[followup] enabled 를 껐다 켜면, 꺼져 있던 동안 달린 추가 질문에는 답하지 않는다."""
    import os

    fake, answerer = svc._kakao, svc._answerer
    monkeypatch.setattr("kakaobot.service.KakaoClient", lambda *a, **k: fake)
    svc._ensure_answerer = lambda: answerer  # 설정을 다시 읽으면 _answerer 는 비워진다
    baseline(svc)
    on = svc.cfg_path.read_text(encoding="utf-8")
    off = on.replace("[followup]\nenabled = true", "[followup]\nenabled = false")
    assert off != on

    def rewrite(text, bump):
        svc.cfg_path.write_text(text, encoding="utf-8")
        st = svc.cfg_path.stat()
        os.utime(svc.cfg_path, (st.st_atime, st.st_mtime + bump))
        svc._reload_config()

    rewrite(off, 5)
    assert not svc._followup_enabled
    post(
        svc,
        "꺼져 있을 때 질문이요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("꺼져 있을 때 질문이요?")],
    )
    svc._poll_followups()
    rewrite(on, 10)
    assert svc._followup_enabled
    svc._poll_followups()  # 다시 켠 뒤 첫 화면은 기준선
    assert fake.replies == []
    post(
        svc,
        "켠 뒤 질문이요?",
        [
            tm(PARENT),
            tm(BOT, outgoing=True),
            tm("꺼져 있을 때 질문이요?"),
            tm("켠 뒤 질문이요?"),
        ],
    )
    svc._poll_followups()
    assert [r["expect"] for r in fake.replies] == ["켠 뒤 질문이요?"]
