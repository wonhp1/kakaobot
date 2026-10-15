"""댓글 스레드 답장 — 마우스를 쓰지 않는다.

openkakao-cli 에는 답장/댓글 기능이 없어 AX 를 직접 다룬다. 계획서 §3 은 I/O 계층을
직접 만들지 않는다고 했지만, 기성 도구가 이 기능을 아예 제공하지 않으므로 예외로 둔다.

2026-09-18 실측으로 확정한 경로 (KakaoTalk 25.9.1):

    ① 메시지의 AXCell 에 AXShowMenu   → 카카오톡 컨텍스트 메뉴
         복사하기 · 대화 캡처 · 나에게서만 삭제 · 가리기 · 댓글 · 공감 · 공유 · 공지
    ② '댓글' AXMenuItem 에 AXPick      → 스레드 창이 열림
    ③ 입력창 AXTextArea 에 AXValue 설정
    ④ '전송' AXButton 에 AXPress

전 과정에서 커서가 움직이지 않는다 (좌표 추적으로 검증).

⚠️ 카카오톡의 AX 액션 목록은 믿을 수 없다. 반드시 결과를 읽어 검증한다.

    요소          목록에 표시된 액션       실제로 되는 것
    '댓글' 항목    AXPress, AXPick         AXPick 만 (AXPress 는 0 을 반환하고 무시)
    '전송' 버튼    (없음)                  AXPress 가 동작함
    AXRow          (없음)                  —
    AXCell         AXShowMenu              AXShowMenu 동작함

호버 시 나타나는 '↳' 버튼은 실제 마우스가 올라가 있어야만 AX 에 나타나고 액션도 없다.
같은 기능이 컨텍스트 메뉴에 있으므로 그쪽을 쓴다.

2026-09-20 실전 테스트로 추가된 두 가지 (docs/thread-reply.md):

  ① **화면 밖 메시지에는 메뉴가 열리지 않는다.** AXShowMenu 가 0(성공)을 돌려주고도
     아무 일도 일어나지 않는다. 봇이 답을 올리면 화면이 아래로 밀리므로, 앞선 질문은
     금세 화면 밖으로 나간다. 그래서 메뉴를 열기 전에 스크롤바 AXValue 로 대상을
     보이는 영역 안에 넣는다.
  ② **컨텍스트 메뉴는 앱이 아니라 그 창 아래에 붙는다.** 앱 전체를 훑으면 메인 목록
     창(채팅 451개)까지 훑어 12초가 넘는다. 창 아래만 얕게 본다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("kakaobot.thread")

KAKAO_BUNDLE = "com.kakao.KakaoTalkMac"
MIRROR_CHECKBOX = "채팅방에 함께 보내기"
# 스레드를 열면 함께 뜨는 부속 창 (스레드가 달린 메시지 목록)
AUX_WINDOW = "댓글 달린 메시지"


class ThreadError(Exception):
    pass


@dataclass
class ThreadResult:
    ok: bool
    detail: str = ""
    thread_before: list[str] | None = None
    thread_after: list[str] | None = None


@dataclass(frozen=True)
class ThreadMessage:
    """스레드 창의 말풍선 하나. 첫째가 원글이다."""

    text: str
    outgoing: bool          # 운영자(= 봇이 쓰는 계정)가 쓴 댓글
    sender: str | None      # 받은 댓글의 이름표. 이어 쓴 줄은 앞 사람 이름을 물려받는다


# --- AX 저수준 --------------------------------------------------------------

def _ax():
    """pyobjc 를 지연 임포트한다. 테스트 환경에서 모듈 로딩을 막지 않기 위해."""
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
    )
    from AppKit import NSWorkspace

    return (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        NSWorkspace,
    )


def _hspan(ax: "_AX", element) -> tuple[float, float] | None:
    """(x, 너비)."""
    pos, size = ax.attr(element, "AXPosition"), ax.attr(element, "AXSize")
    if pos is None or size is None:
        return None
    try:
        x = float(str(pos).split("x:")[1].split(" ")[0].rstrip(","))
        w = float(str(size).split("w:")[1].split(" ")[0].rstrip(","))
    except (IndexError, ValueError):
        return None
    return x, w


def _rect(ax: "_AX", element) -> tuple[float, float] | None:
    """(y, 높이). AXValue 구조체는 문자열로만 읽히므로 숫자를 뽑아낸다."""
    pos, size = ax.attr(element, "AXPosition"), ax.attr(element, "AXSize")
    if pos is None or size is None:
        return None
    try:
        y = float(str(pos).split("y:")[1].split(" ")[0].rstrip(","))
        h = float(str(size).split("h:")[1].split(" ")[0].rstrip(","))
    except (IndexError, ValueError):
        return None
    return y, h


class _AX:
    def __init__(self) -> None:
        (self._copy, create, self._perform, self._set, ws) = _ax()
        pid = next(
            (a.processIdentifier() for a in ws.sharedWorkspace().runningApplications()
             if a.bundleIdentifier() == KAKAO_BUNDLE),
            None,
        )
        if pid is None:
            raise ThreadError("카카오톡이 실행 중이 아닙니다")
        self.app = create(pid)

    def attr(self, e, name):
        err, v = self._copy(e, name, None)
        return v if err == 0 else None

    def perform(self, e, action) -> int:
        return self._perform(e, action)

    def set_value(self, e, value) -> int:
        return self._set(e, "AXValue", value)

    def walk(self, e, depth=0, max_depth=14):
        yield e
        if depth < max_depth:
            for ch in (self.attr(e, "AXChildren") or []):
                yield from self.walk(ch, depth + 1, max_depth)

    def windows(self):
        return self.attr(self.app, "AXWindows") or []

    def texts(self, win) -> list[str]:
        """창의 메시지 목록(AXTable)에서 본문만 뽑는다."""
        table = next((e for e in self.walk(win) if self.attr(e, "AXRole") == "AXTable"), None)
        if table is None:
            return []
        return [
            self.attr(c, "AXValue")
            for e in self.walk(table)
            if self.attr(e, "AXRole") == "AXCell"
            for c in (self.attr(e, "AXChildren") or [])
            if self.attr(c, "AXRole") == "AXTextArea"
        ]


def _message_area(ax: _AX, win):
    """메시지 목록 스크롤 영역과 그 안의 표·세로 스크롤바."""
    for e in ax.walk(win, max_depth=8):
        if ax.attr(e, "AXRole") != "AXScrollArea":
            continue
        kids = ax.attr(e, "AXChildren") or []
        table = next((k for k in kids if ax.attr(k, "AXRole") == "AXTable"), None)
        if table is None:
            continue
        bar = next(
            (k for k in kids if ax.attr(k, "AXRole") == "AXScrollBar"
             and ax.attr(k, "AXOrientation") == "AXVerticalOrientation"),
            None,
        )
        return e, table, bar
    return None, None, None


def _scroll_into_view(ax: _AX, win, cell) -> tuple[object, float] | None:
    """대상 셀을 보이는 영역 안으로 끌어온다. 스크롤했으면 (스크롤바, 원래 값).

    화면 밖 메시지에는 컨텍스트 메뉴가 열리지 않는다 (2026-09-20 실측).
    """
    area, table, bar = _message_area(ax, win)
    view, box = (_rect(ax, area) if area else None), _rect(ax, cell)
    if view is None or box is None:
        return None
    if view[0] <= box[0] and box[0] + box[1] <= view[0] + view[1]:
        return None                                   # 이미 다 보인다
    if bar is None or table is None:
        raise ThreadError("메시지 목록을 스크롤할 수 없습니다")
    tbl = _rect(ax, table)
    span = tbl[1] - view[1] if tbl else 0
    if span <= 0:
        return None
    old = ax.attr(bar, "AXValue")
    offset = (box[0] - tbl[0]) - max(0.0, (view[1] - box[1]) / 2)   # 가운데로
    ax.set_value(bar, max(0.0, min(1.0, offset / span)))
    time.sleep(0.4)
    return bar, (old if old is not None else 1.0)


def _context_menu(ax: _AX, win, timeout: float = 2.5):
    """AXShowMenu 로 연 컨텍스트 메뉴. **그 창 아래**에 붙는다 (앱 전체를 훑지 않는다)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        menu = next(
            (e for e in ax.walk(win, max_depth=6) if ax.attr(e, "AXRole") == "AXMenu"),
            None,
        )
        if menu is not None and (ax.attr(menu, "AXChildren") or []):
            return menu
        time.sleep(0.15)
    return None


# --- 공개 API ---------------------------------------------------------------

def _thread_windows(ax: _AX) -> list:
    """열려 있는 스레드 창 전부 — 제목이 비어 있고 동시전송 체크박스가 있는 창."""
    return [
        w for w in ax.windows()
        if not ax.attr(w, "AXTitle")
        and any(ax.attr(e, "AXTitle") == MIRROR_CHECKBOX for e in ax.walk(w))
    ]


def _aux_windows(ax: _AX) -> list:
    return [w for w in ax.windows() if ax.attr(w, "AXTitle") == AUX_WINDOW]


def _is_new(window, before: list) -> bool:
    # AX 요소는 == 로 같은 창인지 비교된다 (CFEqual, 2026-09-24 확인)
    return not any(window == b for b in before)


@dataclass
class _Opened:
    """봇이 연 스레드 창. 닫을 때 **봇이 연 창만** 닫으려고 연 뒤의 차이를 기억한다."""

    window: object
    scrolled: tuple | None
    aux_before: list


def _open_thread(
    ax: _AX,
    room: str,
    target_text: str,
    target_sender: str | None = None,
    nth: int = 0,
) -> _Opened:
    """원글 칸 → 메뉴 '댓글' → 스레드 창.

    ThreadError 가 나면 스레드 창은 열리지 않았고, 움직인 스크롤은 되돌려 놓았다.
    같은 본문이 여러 개면 **최근에서 nth 번째**를 고른다 (0 = 가장 최근).

    **새로 열린 스레드 창만** 쓴다. 운영자가 스레드 창을 열어 두고 댓글을 쓰는 중일
    수 있다 — 예전처럼 '제목 없는 첫 스레드 창'을 잡으면 그 창에 쓰거나 그 창을 닫는다.
    같은 스레드를 운영자가 이미 열어 두었으면 새 창이 생기지 않으므로 손대지 않고 멈춘다.
    """
    room_win = next((w for w in ax.windows() if ax.attr(w, "AXTitle") == room), None)
    if room_win is None:
        raise ThreadError(f"'{room}' 방이 별도 창으로 열려 있지 않습니다")
    threads_before = _thread_windows(ax)
    aux_before = _aux_windows(ax)

    def target_cells():
        cells = [
            e for e in ax.walk(room_win)
            if ax.attr(e, "AXRole") == "AXCell"
            and any(ax.attr(c, "AXValue") == target_text
                    for c in (ax.attr(e, "AXChildren") or []))
        ]
        if target_sender:
            cells = [
                e for e in cells
                if any(ax.attr(c, "AXRole") == "AXStaticText"
                       and ax.attr(c, "AXValue") == target_sender
                       and ax.attr(c, "AXHelp") is None
                       for c in (ax.attr(e, "AXChildren") or []))
            ] or cells  # 연속 메시지는 이름 라벨이 생략되므로 못 찾으면 원래 목록으로
        return cells

    # ① 대상 메시지 셀 — 최근에서 nth 번째
    cells = target_cells()
    if len(cells) <= nth:
        raise ThreadError(f"대상 메시지를 찾을 수 없습니다: {target_text!r} (최근 {nth + 1}번째)")
    cell = cells[-1 - nth]

    # ①-a 화면 안으로. 밖에 있으면 메뉴가 열리지 않는다 (AXShowMenu 가 0 을 돌려줘도)
    scrolled = _scroll_into_view(ax, room_win, cell)
    try:
        if scrolled:
            cells = target_cells()
            cell = cells[-1 - nth] if len(cells) > nth else None
            view = _rect(ax, _message_area(ax, room_win)[0])
            box = _rect(ax, cell) if cell is not None else None
            if box is None or not (view[0] <= box[0] and box[0] + box[1] <= view[0] + view[1]):
                raise ThreadError("대상 메시지를 화면 안으로 가져오지 못했습니다")

        if ax.perform(cell, "AXShowMenu") != 0:
            raise ThreadError("컨텍스트 메뉴를 열지 못했습니다")

        # ② '댓글' — AXPick 이어야 한다
        menu = _context_menu(ax, room_win)
        if menu is None:
            raise ThreadError("컨텍스트 메뉴가 열리지 않았습니다")
        item = next(
            (e for e in (ax.attr(menu, "AXChildren") or [])
             if ax.attr(e, "AXRole") == "AXMenuItem" and ax.attr(e, "AXTitle") == "댓글"),
            None,
        )
        if item is None:
            ax.perform(menu, "AXCancel")
            raise ThreadError("컨텍스트 메뉴에 '댓글' 항목이 없습니다")
        ax.perform(item, "AXPick")

        # ③ 새로 열린 스레드 창
        thread = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and thread is None:
            time.sleep(0.3)
            thread = next(
                (w for w in _thread_windows(ax) if _is_new(w, threads_before)), None
            )
        if thread is None:
            raise ThreadError(
                "스레드 창이 새로 열리지 않았습니다 (같은 스레드 창이 이미 열려 있을 수 있음)"
            )
    except ThreadError:
        _close_new_aux(ax, aux_before)
        _restore_scroll(ax, scrolled)
        raise
    return _Opened(thread, scrolled, aux_before)


def _close_new_aux(ax: _AX, aux_before: list) -> None:
    for w in _aux_windows(ax):
        if _is_new(w, aux_before):
            b = ax.attr(w, "AXCloseButton")
            if b is not None:
                ax.perform(b, "AXPress")


def _close_thread(ax: _AX, opened: _Opened) -> None:
    """봇이 연 스레드 창은 매번 닫는다. 운영자가 열어 둔 창은 그대로 둔다."""
    _restore_scroll(ax, opened.scrolled)
    btn = ax.attr(opened.window, "AXCloseButton")
    if btn is not None:
        ax.perform(btn, "AXPress")
    _close_new_aux(ax, opened.aux_before)


def reply_in_thread(
    room: str,
    target_text: str,
    reply: str,
    mirror_to_room: bool = True,
    target_sender: str | None = None,
    nth: int = 0,
    expect: str | None = None,
) -> ThreadResult:
    """`room` 방에서 `target_text` 메시지에 댓글로 `reply` 를 단다.

    같은 본문이 여러 개면 **가장 최근 것**을 고른다 — 봇은 방금 들어온 질문에 답하기
    때문이다. `target_sender` 가 주어지면 그 발신자의 메시지로 좁힌다.

    스레드 안의 추가 질문에 답할 때는 `nth`(같은 본문 중 최근에서 몇 번째)와
    `expect`(그 스레드에 있어야 할 추가 질문)를 준다. 스레드에 `expect` 가 없으면
    엉뚱한 스레드다 — 보내지 않고 멈춘다.
    """
    ax = _AX()
    opened = _open_thread(ax, room, target_text, target_sender, nth)
    thread = opened.window
    try:
        parent = ax.texts(thread)
        if not parent or parent[0] != target_text:
            raise ThreadError(f"엉뚱한 스레드가 열렸습니다: {parent[:1]!r}")
        # 추가 질문을 찾을 때(read_thread)와 같은 규칙으로 읽어 대조한다
        if expect is not None and expect not in [m.text for m in parse_thread(ax, thread)[1:]]:
            raise ThreadError("추가 질문이 이 스레드에 없습니다 — 같은 본문의 다른 원글일 수 있습니다")

        # 동시전송 체크박스
        cb = next((e for e in ax.walk(thread) if ax.attr(e, "AXRole") == "AXCheckBox"), None)
        if cb is None:
            raise ThreadError(f"'{MIRROR_CHECKBOX}' 체크박스를 찾을 수 없습니다")
        want = 1 if mirror_to_room else 0
        if ax.attr(cb, "AXValue") != want:
            ax.perform(cb, "AXPress")
            time.sleep(0.3)
        if ax.attr(cb, "AXValue") != want:
            raise ThreadError("'채팅방에 함께 보내기' 상태를 바꾸지 못했습니다")

        # 입력
        composers = [
            e for e in ax.walk(thread)
            if ax.attr(e, "AXRole") == "AXTextArea"
            and ax.attr(ax.attr(e, "AXParent"), "AXRole") == "AXScrollArea"
        ]
        if not composers:
            raise ThreadError("스레드 입력창을 찾을 수 없습니다")
        composer = composers[-1]
        if ax.attr(composer, "AXValue"):
            raise ThreadError("입력창에 작성 중인 내용이 있어 덮어쓰지 않습니다")
        ax.set_value(composer, reply)
        time.sleep(0.3)
        if ax.attr(composer, "AXValue") != reply:
            raise ThreadError("입력창에 내용을 넣지 못했습니다")

        # 전송 — 액션 목록엔 없지만 AXPress 가 동작한다
        before = ax.texts(thread)
        send = next(
            (e for e in ax.walk(thread)
             if ax.attr(e, "AXRole") == "AXButton" and ax.attr(e, "AXTitle") == "전송"),
            None,
        )
        if send is None:
            # 입력창에 넣은 글을 비워 두고 나간다 — 다음 시도나 사람이 헷갈리지 않게
            ax.set_value(composer, "")
            raise ThreadError("'전송' 버튼을 찾을 수 없습니다")
        # 여기서부터는 ThreadError 를 내지 않는다. 호출하는 쪽은 ThreadError 를
        # '아직 안 보냈다'는 뜻으로 받아 일반 메시지로 대신 보낸다.
        ax.perform(send, "AXPress")

        # 검증 — 반환값을 믿지 말고 결과를 읽는다
        deadline = time.monotonic() + 5
        after = before
        while time.monotonic() < deadline:
            time.sleep(0.4)
            after = ax.texts(thread)
            if len(after) > len(before) and after[-1] == reply:
                return ThreadResult(True, "전송 확인", before, after)
        return ThreadResult(False, "전송을 확인하지 못했습니다", before, after)
    finally:
        _close_thread(ax, opened)


# --- 스레드 읽기 (추가 질문, 2026-09-24) -------------------------------------

def parse_thread(ax: _AX, thread) -> list[ThreadMessage]:
    """스레드 창의 말풍선들. 첫째가 원글이다. '댓글 N개' 구분 줄·빈 칸은 뺀다.

    누가 썼는지는 방 화면과 **같은 여백 규칙**으로 가린다 (2026-09-24 실제 방 스레드 실측):

        받은 댓글  프로필 + 이름표 + 글   왼쪽 여백 63
        보낸 댓글  이름표 없음            오른쪽 여백 21

    그래서 운영자가 직접 단 댓글과 수강생 댓글을 가를 수 있다.
    """
    table = next(
        (e for e in ax.walk(thread, max_depth=8) if ax.attr(e, "AXRole") == "AXTable"),
        None,
    )
    if table is None:
        return []
    out: list[ThreadMessage] = []
    last_sender: str | None = None
    for cell in ax.walk(table, max_depth=3):
        if ax.attr(cell, "AXRole") != "AXCell":
            continue
        area = next(
            (e for e in ax.walk(cell, max_depth=4) if ax.attr(e, "AXRole") == "AXTextArea"),
            None,
        )
        if area is None:
            continue                       # '댓글 N개' 구분 줄, 버튼 칸, 빈 칸
        cb, tb = _hspan(ax, cell), _hspan(ax, area)
        outgoing = False
        if cb and tb:
            left, right = tb[0] - cb[0], (cb[0] + cb[1]) - (tb[0] + tb[1])
            outgoing = right < left
        label = next(
            (ax.attr(k, "AXValue") for k in (ax.attr(cell, "AXChildren") or [])
             if ax.attr(k, "AXRole") == "AXStaticText" and ax.attr(k, "AXHelp") is None
             and ax.attr(k, "AXValue")),
            None,
        )
        if outgoing:
            sender, last_sender = None, None
        else:
            last_sender = label or last_sender
            sender = last_sender
        out.append(ThreadMessage(ax.attr(area, "AXValue") or "", outgoing, sender))
    return out


def read_thread(
    room: str,
    target_text: str,
    target_sender: str | None = None,
    nth: int = 0,
    ax: _AX | None = None,
) -> list[ThreadMessage]:
    """스레드를 열어 읽고 닫는다. **보내지 않는다.**"""
    ax = ax or _AX()
    opened = _open_thread(ax, room, target_text, target_sender, nth)
    try:
        messages = parse_thread(ax, opened.window)
        if not messages or messages[0].text != target_text:
            raise ThreadError(f"엉뚱한 스레드가 열렸습니다: {messages[:1]!r}")
        return messages
    finally:
        _close_thread(ax, opened)


def pending_followups(messages: list[ThreadMessage]) -> list[ThreadMessage]:
    """마지막 운영자 댓글 **뒤에** 달린 받은 댓글 — 아직 답하지 않은 추가 질문.

    원글(첫째)은 빼고 본다. 운영자가 한 번도 쓰지 않은 스레드는 봇이 답한 스레드가
    아니므로 빈 목록 — 그 원글은 방 쪽에서 처리한다.
    """
    last_out = max((i for i, m in enumerate(messages) if i > 0 and m.outgoing), default=None)
    if last_out is None:
        return []
    return [m for m in messages[last_out + 1:] if not m.outgoing]


def thread_history(messages: list[ThreadMessage], limit: int = 6) -> str:
    """답변 계층에 넘길 대화 이력 — 방의 대화 기억(memory)과 같은 형식."""
    lines = []
    for m in messages[-limit:]:
        who = "봇" if m.outgoing else (m.sender or "질문자")
        lines.append(f"{who}: {m.text}")
    return "\n".join(lines)


def _restore_scroll(ax: _AX, scrolled) -> None:
    """우리가 움직인 스크롤만 되돌린다. 새 메시지로 카카오톡이 맨 아래로 내렸으면 그대로 둔다."""
    if not scrolled:
        return
    bar, old = scrolled
    now = ax.attr(bar, "AXValue")
    if now is not None and abs(float(now) - 1.0) < 0.001:
        return
    ax.set_value(bar, old)


# --- 방에 함께 올라온 스레드 댓글 (2026-09-26) ---------------------------------
#
# 수강생이 스레드 댓글을 '채팅방에 함께 보내기'로 쓰면 같은 글이 방에도 뜬다. 방 화면에서는
# 말풍선 위에 **원글 인용 칸**이, 아래에 **스레드로 가는 버튼**이 붙는다 (실측, KakaoTalk
# 25.9.1 '테스트방'). 스레드가 달린 원글도 아래에 버튼('댓글 N')이 있지만 인용 칸은
# 없다. 인용 칸의 글(원글)은 AX 에 드러나지 않는다 — 어느 스레드인지는 '댓글 알림'으로 안다.
#
#     칸 위 → 글 상자 여백     보통 말풍선 13~18   함께 보낸 댓글 62~67  (이름표가 있으면 그 아래부터)
#     말풍선 아래 버튼         높이 22 — 원글의 '댓글 N'과 함께 보낸 댓글의 버튼이 같다

QUOTE_GAP = 40      # 이보다 떨어져 있으면 인용 칸이 있다고 본다
LINK_MAX_H = 30     # 말풍선 아래 버튼 높이 상한 — 링크 미리보기 같은 큰 칸은 빼려고


def _thread_comment_row(ax: _AX, cell) -> tuple[str | None, bool]:
    """(본문, 스레드에서 함께 보낸 댓글인가). 글 상자가 없는 칸(사진·날짜 줄)은 (None, False)."""
    kids = ax.attr(cell, "AXChildren") or []
    area = next((k for k in kids if ax.attr(k, "AXRole") == "AXTextArea"), None)
    if area is None:
        return None, False
    text = ax.attr(area, "AXValue") or ""
    box, top = _rect(ax, area), _rect(ax, cell)
    if box is None or top is None:
        return text, False
    link, head = False, top[0]
    for k in kids:
        role = ax.attr(k, "AXRole")
        if role not in ("AXButton", "AXStaticText"):
            continue
        r = _rect(ax, k)
        if r is None:
            continue
        if role == "AXButton" and r[0] >= box[0] + box[1] - 1 and r[1] <= LINK_MAX_H:
            link = True
        elif (role == "AXStaticText" and ax.attr(k, "AXHelp") is None
              and r[0] + r[1] <= box[0]):
            head = max(head, r[0] + r[1])          # 이름표 (시각 라벨은 AXHelp 에 날짜가 있다)
    return text, link and box[0] - head >= QUOTE_GAP


def thread_comment_rows(room: str, count: int, ax: _AX | None = None) -> list[tuple[str, bool]]:
    """방 창 맨 아래 **글 줄** `count` 개의 (본문, 스레드에서 함께 보낸 댓글인가). 오래된 것부터.

    읽기만 한다. 글 상자가 없는 줄(사진·날짜 구분)은 뺀다 — 그래야 ax-read 결과와 1:1 로
    맞는다 (실측: 글 줄은 순서·본문이 ax-read 와 같다).
    """
    ax = ax or _AX()
    win = next((w for w in ax.windows() if ax.attr(w, "AXTitle") == room), None)
    if win is None:
        raise ThreadError(f"'{room}' 방이 별도 창으로 열려 있지 않습니다")
    _, table, _ = _message_area(ax, win)
    if table is None:
        raise ThreadError("메시지 목록을 찾지 못했습니다")
    out: list[tuple[str, bool]] = []
    for row in reversed(ax.attr(table, "AXChildren") or []):
        if len(out) >= count:
            break
        if ax.attr(row, "AXRole") != "AXRow":
            continue
        cell = next(iter(ax.attr(row, "AXChildren") or []), None)
        if cell is None:
            continue
        text, flag = _thread_comment_row(ax, cell)
        if text is not None:
            out.append((text, flag))
    out.reverse()
    return out
