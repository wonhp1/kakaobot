"""댓글을 달기 전 대상 메시지를 화면 안으로 끌어오는 계산.

2026-09-20 실전 테스트: 화면 밖 메시지에는 컨텍스트 메뉴가 열리지 않는다
(AXShowMenu 가 0 을 돌려주고도 아무 일도 하지 않음). 카카오톡 없이 기하만 검증한다.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.thread import _rect, _restore_scroll, _scroll_into_view


def point(y: float) -> str:
    return f"<AXValue> {{value = x:100.000000 y:{y:.6f} type = kAXValueCGPointType}}"


def size(h: float) -> str:
    return f"<AXValue> {{value = w:367.000000 h:{h:.6f} type = kAXValueCGSizeType}}"


def element(role, y=None, h=None, children=None, **extra):
    e = {"AXRole": role, "AXChildren": children or []}
    if y is not None:
        e["AXPosition"], e["AXSize"] = point(y), size(h)
    e.update(extra)
    return e


class FakeAX:
    """thread.py 가 쓰는 부분만 흉내 낸다."""

    def __init__(self):
        self.sets = []

    def attr(self, element, name):
        return element.get(name)

    def walk(self, element, depth=0, max_depth=14):
        yield element
        if depth < max_depth:
            for child in element.get("AXChildren", []):
                yield from self.walk(child, depth + 1, max_depth)

    def set_value(self, element, value):
        element["AXValue"] = value
        self.sets.append(value)
        return 0


def room(cell_y: float, cell_h: float = 37):
    """실측값 그대로: 보이는 영역 450~882, 표는 -1078 에서 1961 높이."""
    bar = element("AXScrollBar", AXOrientation="AXVerticalOrientation", AXValue=1.0)
    cell = element("AXCell", y=cell_y, h=cell_h)
    table = element("AXTable", y=-1078, h=1961, children=[cell])
    area = element("AXScrollArea", y=450, h=432, children=[table, bar])
    composer = element(
        "AXScrollArea", y=900, h=60, children=[element("AXTextArea", y=900, h=60)]
    )
    return element("AXWindow", y=362, h=640, children=[area, composer]), cell, bar


def test_rect_parses_ax_value_structs():
    ax = FakeAX()
    assert _rect(ax, element("AXCell", y=410, h=37)) == (410.0, 37.0)
    assert _rect(ax, element("AXCell")) is None


def test_visible_message_is_not_scrolled():
    ax = FakeAX()
    win, cell, _ = room(cell_y=600)  # 450~882 안
    assert _scroll_into_view(ax, win, cell) is None
    assert ax.sets == []


def test_offscreen_message_is_brought_into_view():
    """실제로 놓쳤던 배치: 대상이 보이는 영역보다 37px 위에 있었다."""
    ax = FakeAX()
    win, cell, bar = room(cell_y=410)
    scrolled = _scroll_into_view(ax, win, cell)
    assert scrolled is not None and scrolled[0] is bar and scrolled[1] == 1.0
    (value,) = ax.sets
    assert 0.0 <= value < 1.0  # 위로 올린다

    # 그 값이 정말 대상을 보이는 영역 안에 놓는가 — 표를 그만큼 내려 다시 잰다
    offset = value * (1961 - 432)  # 표 높이 - 보이는 높이
    top_of_view_in_table = offset  # 보이는 영역의 표 안 위치
    cell_in_table = 410 - (-1078)
    assert top_of_view_in_table <= cell_in_table <= top_of_view_in_table + 432 - 37


def test_message_below_the_view_scrolls_down():
    ax = FakeAX()
    win, cell, _ = room(cell_y=1000)  # 882 아래
    assert _scroll_into_view(ax, win, cell) is not None
    assert ax.sets and ax.sets[0] <= 1.0


def test_scroll_is_restored_unless_kakao_jumped_to_the_bottom():
    ax = FakeAX()
    bar = element("AXScrollBar", AXValue=0.84)
    _restore_scroll(ax, (bar, 1.0))
    assert ax.sets == [1.0]  # 우리가 움직인 값을 되돌린다

    ax = FakeAX()
    bar = element("AXScrollBar", AXValue=1.0)  # 새 메시지로 카카오톡이 내림
    _restore_scroll(ax, (bar, 0.5))
    assert ax.sets == []

    ax = FakeAX()
    _restore_scroll(ax, None)  # 스크롤한 적 없음
    assert ax.sets == []


def test_missing_scrollbar_is_an_error_not_a_silent_send():
    """스크롤을 못 하면 댓글을 포기하고 일반 메시지로 넘어가야 한다 (조용히 실패 금지)."""
    from kakaobot.thread import ThreadError

    ax = FakeAX()
    cell = element("AXCell", y=410, h=37)
    table = element("AXTable", y=-1078, h=1961, children=[cell])
    area = element("AXScrollArea", y=450, h=432, children=[table])  # 스크롤바 없음
    win = element("AXWindow", y=362, h=640, children=[area])
    with pytest.raises(ThreadError):
        _scroll_into_view(ax, win, cell)
