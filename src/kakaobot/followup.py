"""스레드 안의 추가 질문 알아채기 (2026-09-24).

봇은 방 화면만 읽는다. 수강생이 봇의 댓글 아래에 추가 질문을 달면, '채팅방에 함께
보내기'를 켜지 않는 한 방 화면에는 아무것도 나타나지 않는다. 대신 카카오톡의
**'댓글 알림' 방**에 운영자가 참여한 스레드가 최신순으로 쌓인다. 한 줄에 이렇게 있다
(2026-09-24 실측):

    시각 · 방 이름 · 원글 앞부분 · 최신 댓글 앞부분 · (읽지 않음 표시)

그 창을 열어 두고 **맨 위 몇 줄만** 본다. 대상 방의 줄에서 최신 댓글이 봇이 쓰지
않은 글로 바뀌면 추가 질문일 수 있다는 신호다.

알림 목록에서 스레드를 바로 열 수는 없다. 칸의 메뉴 동작은 목록에만 있고 실제로는
아무 일도 하지 않는다 (2026-09-24 실측). 그래서 알림은 **신호로만** 쓰고, 스레드는
대상 방에서 원글 → 우클릭 → 댓글로 연다 (thread.read_thread). 누가 쓴 댓글인지도
알림에는 없으므로 스레드에서 확인한다.

읽기 비용: 알림 한 줄에 약 0.14초가 든다 (카카오톡이 느리게 답한다). 그래서 맨 위
몇 줄만, 폴링 몇 번에 한 번 읽는다. 다른 방 알림은 방 이름만 보고 내용은 읽지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .thread import ThreadMessage, thread_history

log = logging.getLogger("kakaobot.followup")

NOTICE_WINDOW = "댓글 알림"
UNREAD_ICON = "common icon unread mention"
_ELLIPSIS = "…"
# 알림 글 끝에 붙어 나오는 폭 없는 공백 (U+200B). 눈에 안 보여 이름을 붙여 둔다
_ZWSP = chr(0x200B)


@dataclass(frozen=True)
class Notice:
    """댓글 알림 한 줄 (대상 방 것만 만든다)."""

    room: str
    parent: str  # 원글 앞부분 — 길면 '…' 로 잘린다
    latest: str  # 스레드의 최신 댓글 앞부분
    unread: bool


def _clean(text: str | None) -> str:
    """알림 글 끝에 붙는 폭 없는 공백을 떼고, 공백을 하나로 모은다."""
    return " ".join((text or "").replace(_ZWSP, "").split())


def matches(clipped: str, full: str) -> bool:
    """알림에 보인 앞부분(clipped)이 이 글(full)의 것인가."""
    c, f = _clean(clipped), _clean(full)
    if not c:
        return False
    if c.endswith(_ELLIPSIS):
        # '…'만 있으면 아무 글과도 맞추지 않는다
        stem = c[: -len(_ELLIPSIS)].rstrip()
        return bool(stem) and f.startswith(stem)
    return f == c


def read_notices(
    room: str, top: int = 5, ax=None, window: str = NOTICE_WINDOW
) -> list[Notice] | None:
    """'댓글 알림' 창 맨 위 top 줄에서 room 의 알림만 돌려준다. 창이 없으면 None."""
    from .thread import _AX

    ax = ax or _AX()
    win = next((w for w in ax.windows() if ax.attr(w, "AXTitle") == window), None)
    if win is None:
        return None
    area = next(
        (
            k
            for k in (ax.attr(win, "AXChildren") or [])
            if ax.attr(k, "AXRole") == "AXScrollArea"
        ),
        None,
    )
    table = (
        next(
            (
                k
                for k in (ax.attr(area, "AXChildren") or [])
                if ax.attr(k, "AXRole") == "AXTable"
            ),
            None,
        )
        if area is not None
        else None
    )
    if table is None:
        return []

    out: list[Notice] = []
    rows = [
        r
        for r in (ax.attr(table, "AXChildren") or [])
        if ax.attr(r, "AXRole") == "AXRow"
    ]
    for row in rows[:top]:
        cell = next(
            (
                c
                for c in (ax.attr(row, "AXChildren") or [])
                if ax.attr(c, "AXRole") == "AXCell"
            ),
            None,
        )
        kids = (ax.attr(cell, "AXChildren") or []) if cell is not None else []
        statics = [
            ax.attr(k, "AXValue")
            for k in kids
            if ax.attr(k, "AXRole") == "AXStaticText"
        ]
        # [0] 시각, [1] 방 이름. 다른 방이면 여기서 멈춘다 — 내용은 읽지 않는다
        if len(statics) < 2 or _clean(statics[1]) != room:
            continue
        areas = [
            ax.attr(a, "AXValue")
            for k in kids
            if ax.attr(k, "AXRole") == "AXScrollArea"
            for a in (ax.attr(k, "AXChildren") or [])
            if ax.attr(a, "AXRole") == "AXTextArea"
        ]
        if len(areas) < 2:
            continue
        unread = any(
            ax.attr(k, "AXRole") == "AXImage"
            and ax.attr(k, "AXDescription") == UNREAD_ICON
            for k in kids
        )
        out.append(Notice(room, _clean(areas[0]), _clean(areas[1]), unread))
    return out


class NoticeTracker:
    """이미 본 (원글, 최신 댓글)을 기억한다.

    처음 읽은 화면은 **기준선**이다 — 그때 있던 알림에는 반응하지 않는다. 봇을 다시
    띄울 때마다 옛 추가 질문에 답하면 안 되기 때문이다 (방 화면의 기준선과 같은 이유).
    """

    def __init__(self, capacity: int = 500) -> None:
        self._seen: dict[tuple[str, str], None] | None = None
        self._capacity = capacity

    def fresh(self, notices: list[Notice]) -> list[Notice]:
        keys = [(n.parent, n.latest) for n in notices]
        if self._seen is None:
            self._seen = dict.fromkeys(keys)
            return []
        new = [n for n, k in zip(notices, keys) if k not in self._seen]
        for k in keys:
            self._seen[k] = None
        while len(self._seen) > self._capacity:
            self._seen.pop(next(iter(self._seen)))
        return new

    def reset(self) -> None:
        """다음 읽기를 기준선으로 삼는다 (창을 다시 열었을 때, 멈췄다 재개할 때)."""
        self._seen = None

    @property
    def has_baseline(self) -> bool:
        return self._seen is not None


def parent_candidates(
    texts: list[str], clipped: str, limit: int = 3
) -> list[tuple[str, int]]:
    """방 화면의 글(위→아래 순서)에서 알림의 원글 앞부분에 맞는 것 — 최근 것부터 (전문, nth).

    texts 는 봇이 매 폴링에 쓰는 ax-read 결과로 준다. 접근성 트리를 직접 훑는 것보다
    세 배 빠르다 (2026-09-24 연습방 실측: 칸 53개 3.7초 → ax-read 1.2초, 같은 본문 개수 같음).

    nth 는 **같은 전문** 가운데 최근에서 몇 번째인지다 (thread._open_thread 가 같은 본문
    중 하나를 고르는 방식과 같다). 테스트방처럼 같은 질문이 여러 번 올라와 있으면
    알림이 가리키는 스레드가 가장 최근 원글의 것이 아닐 수 있다 — 호출하는 쪽이 차례로
    열어 알림의 최신 댓글이 있는 스레드를 고른다.
    """
    out: list[tuple[str, int]] = []
    seen: dict[str, int] = {}
    for text in reversed(texts):
        if not text or not matches(clipped, text):
            continue
        out.append((text, seen.get(text, 0)))
        seen[text] = seen.get(text, 0) + 1
        if len(out) >= limit:
            break
    return out


# --- 스레드에서 답할 추가 질문 고르기 -----------------------------------------


@dataclass(frozen=True)
class Followup:
    """스레드 안에서 봇이 답할 추가 질문 — 답을 달 곳과 맥락."""

    parent: str  # 원글 전문
    nth: int  # 같은 본문의 원글 중 최근에서 몇 번째 (0 = 가장 최근)
    asker: str | None
    lines: tuple[str, ...]  # 봇의 마지막 답 뒤에 질문자가 단 댓글 (차례대로)
    history: str  # 원글 → 봇의 답 → … 스레드 대화 (답변 계층의 맥락)

    @property
    def question(self) -> str:
        return "\n".join(self.lines)


def pick_followup(
    parent: str,
    nth: int,
    messages: list[ThreadMessage],
    is_bot_reply: Callable[[str], bool],
    was_answered: Callable[[str | None, str], bool] | None = None,
) -> tuple[Followup | None, str]:
    """스레드 대화에서 봇이 답할 추가 질문을 고른다. (추가 질문, 고르지 않은 이유).

    **마지막 운영자 댓글이 봇의 답일 때만** 고른다. 운영자가 직접 이어 답하고 있는
    스레드에 봇이 끼어들지 않게 한다 — 봇과 운영자는 같은 계정이라 여백으로는 구분이
    안 되고, 봇이 보낸 문장(감사 기록)과 대조해 가린다.

    마지막 봇 답 뒤에 여러 사람이 댓글을 달았으면 **맨 나중 사람**의 댓글만 모은다.

    was_answered 를 주면, 봇의 답 뒤에 새 댓글이 없을 때 **바로 앞 구간**(직전 봇 답과
    마지막 봇 답 사이)에서 아직 답하지 않은 댓글을 찾는다. 봇이 답을 만드는 10~20초
    사이에 같은 사람이 더 달면, 봇의 답이 그 댓글 **뒤에** 붙어 '봇의 답 뒤'만 봐서는
    놓친다. 앞의 사람만 골랐을 때 남은 다른 사람의 댓글도 여기서 차례가 온다.
    """
    outs = [i for i, m in enumerate(messages) if i > 0 and m.outgoing]
    if not outs:
        return None, "운영자 댓글이 없는 스레드"
    last_out = outs[-1]
    if not is_bot_reply(messages[last_out].text):
        return None, "마지막 운영자 댓글이 봇의 답이 아님 (운영자가 직접 답하는 중)"

    def unanswered(i: int) -> bool:
        m = messages[i]
        return not m.outgoing and bool(m.text)

    idx = [i for i in range(last_out + 1, len(messages)) if unanswered(i)]
    if not idx and was_answered is not None and len(outs) >= 2:
        idx = [
            i
            for i in range(outs[-2] + 1, last_out)
            if unanswered(i) and not was_answered(messages[i].sender, messages[i].text)
        ]
    if not idx:
        return None, "봇의 답 뒤에 새 댓글 없음"
    asker = messages[idx[-1]].sender
    chosen = {i for i in idx if messages[i].sender == asker}
    lines = tuple(messages[i].text for i in sorted(chosen))
    # 맥락은 마지막 봇 답까지의 대화에서 이번 질문 줄을 뺀 것
    history = thread_history(
        [m for i, m in enumerate(messages[: last_out + 1]) if i not in chosen]
    )
    return Followup(parent, nth, asker, lines, history), ""
