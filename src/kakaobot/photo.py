"""질문과 함께 올라온 사진의 글자를 읽는다 (docs/screenshots.md).

접근성 트리에는 사진의 픽셀도 파일 단서도 없다 (2026-09-21 확인 — AXImage 의
AXEmbeddedImageDescription 은 None). 대신 사진 말풍선의 우클릭 메뉴에서
'저장하기'를 고르면, 카카오톡이 원본을 다운로드 폴더에
`KakaoTalk_Photo_<시각>.png` 로 곧바로 저장한다. 그 파일을 기기 안에서 글자
인식(kakaobot.ocr)하고 **바로 지운다** — 수강생 화면이 운영자 맥에 쌓이지 않게.

2026-09-21 실측 (실측 스크립트는 저장소에 없음):
  - 봇이 골라도 앞에 있는 앱이 바뀌지 않는다 (0.035초 간격 69번 기록 중 0번)
  - 1.8초 안에 원본 해상도(2486×502)로 저장, 글자 인식 결과가 원본과 같다
  - 한 번 저장한 사진은 메뉴 항목이 '폴더열기'로 바뀐다 → 같은 사진을 두 번 받지 않는다

**언제 읽나: 질문과 함께 온 사진만** (2026-09-21 운영자 결정). 사진만 올린
메시지에는 반응하지 않는다 — 결과 자랑 같은 사진에 엉뚱하게 답하지 않도록.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .kakao import Message

log = logging.getLogger("kakaobot.photo")

# openkakao ax-read 가 사진 말풍선 자리에 넣는 글자 (ax_send.rs:965)
PHOTO = "[사진]"
FILE = "[파일]"
MEDIA = (PHOTO, FILE)

SAVE_ITEM = "저장하기"
# 이보다 작은 그림은 프로필 사진·아이콘이다 (실측 34×34). 사진 말풍선은 훨씬 크다.
MIN_PHOTO_PX = 60
FILE_GLOB = "KakaoTalk_Photo_*"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp"}
_SIZE = re.compile(r"w:([\d.]+) h:([\d.]+)")


class PhotoError(Exception):
    """사진을 받아 오는 도중 멈췄다. 질문은 글만으로 계속 처리한다."""


# --- 어느 사진인가 ------------------------------------------------------------


def nearby_photo(
    messages: list[Message], index: int, before: int = 4, after: int = 3
) -> int | None:
    """질문(messages[index])과 같은 묶음에 있는 사진이 **뒤에서 몇 번째 사진인지**.

    같은 사람이 이어 보낸 메시지만 본다. 다른 사람의 말, 내 메시지, 공지를
    만나면 거기서 멈춘다 — 남의 사진을 이 질문에 붙이면 안 된다. 질문 **앞**을
    먼저 본다 (실제 기록: 사진을 올린 뒤 "이곳 비번은 뭔가요?" 식으로 묻는다).
    없으면 뒤를 본다.

    돌려주는 값은 **질문 기준 상대 위치**다 (-1 = 바로 앞 줄, +1 = 바로 뒤 줄).
    사진이 없거나 질문자를 모르면 None.

    예전에는 '화면의 사진 중 뒤에서 몇 번째'를 돌려줬다. 그런데 카카오톡은 사진
    줄을 **저절로 깜빡인다** — 2026-09-21 15:49 실측, 스크롤도 새 메시지도 없이 사진 줄
    3개 중 2개가 3초간 목록에서 빠졌다. 그 사이에 세면 **남의 사진**을 받는다. 질문
    말풍선은 깜빡이지 않으므로 그것을 기준으로 삼는다 (save_photo).
    """
    q = messages[index]
    if q.sender is None or q.outgoing:
        return None

    def same_burst(m: Message) -> bool:
        return not m.outgoing and not m.is_system_notice and m.sender == q.sender

    target = None
    for j in range(index - 1, max(-1, index - 1 - before), -1):
        if not same_burst(messages[j]):
            break
        if messages[j].text == PHOTO:
            target = j
            break
    if target is None:
        for j in range(index + 1, min(len(messages), index + 1 + after)):
            if not same_burst(messages[j]):
                break
            if messages[j].text == PHOTO:
                target = j
                break
    if target is None:
        return None
    return target - index


# --- 접근성 트리에서 사진 말풍선 ----------------------------------------------


def _rows(ax, win) -> list:
    """메시지 표의 칸을 **줄 순서대로 전부** — 날짜 구분선·공지·깜빡이는 사진 줄까지.

    ax-read 는 글도 그림도 없는 줄을 건너뛰지만, 여기서는 건너뛰지 않는다. 사진 줄이
    그림을 다시 그리느라 잠깐 비어도 **줄 자체는 표에 남아 있으므로** 질문으로부터의
    거리가 흔들리지 않는다.
    """
    table = next(
        (e for e in ax.walk(win, max_depth=8) if ax.attr(e, "AXRole") == "AXTable"),
        None,
    )
    if table is None:
        return []
    return [c for c in ax.walk(table, max_depth=3) if ax.attr(c, "AXRole") == "AXCell"]


def _text_of(ax, cell) -> str | None:
    area = next(
        (e for e in ax.walk(cell, max_depth=6) if ax.attr(e, "AXRole") == "AXTextArea"),
        None,
    )
    return ax.attr(area, "AXValue") if area is not None else None


def _is_photo(ax, cell) -> bool:
    """글 입력 영역 없이 사진 크기의 그림이 있는 칸 (프로필 사진·이모티콘은 작다)."""
    return _text_of(ax, cell) is None and _largest_image_px(ax, cell) >= MIN_PHOTO_PX


def _largest_image_px(ax, cell) -> float:
    best = 0.0
    for e in ax.walk(cell, max_depth=6):
        if ax.attr(e, "AXRole") == "AXImage":
            m = _SIZE.search(str(ax.attr(e, "AXSize")))
            if m:
                best = max(best, float(m.group(1)), float(m.group(2)))
    return best


def _existing(save_dir: Path) -> set[Path]:
    return set(save_dir.glob(FILE_GLOB)) if save_dir.is_dir() else set()


def wait_for_new_file(
    save_dir: Path,
    before: set[Path],
    timeout: float = 8.0,
    poll: float = 0.2,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Path | None:
    """저장하기 뒤에 새로 생긴 사진 파일. **크기가 두 번 연속 같을 때** 돌려준다.

    다 쓰이기 전에 읽으면 글자 인식이 반쪽 그림을 본다.
    """
    deadline = clock() + timeout
    candidate, last_size = None, -1
    while clock() < deadline:
        fresh = [
            p
            for p in _existing(save_dir) - before
            if p.suffix.lower() in _IMAGE_SUFFIXES
        ]
        if fresh:
            newest = max(fresh, key=lambda p: p.stat().st_mtime)
            size = newest.stat().st_size
            if newest == candidate and size == last_size and size > 0:
                return newest
            candidate, last_size = newest, size
        sleep(poll)
    return None


def save_photo(
    room: str,
    question_text: str,
    offset: int,
    save_dir: Path,
    timeout: float = 8.0,
    ax=None,
    tries: int = 3,
    wait: float = 1.0,
) -> Path | None:
    """질문 말풍선에서 offset 줄 떨어진 사진의 '저장하기'를 골라 저장된 파일을 돌려준다.

    None 인 경우: 질문이나 그 사진이 화면에 없음, 이모티콘처럼 작은 그림, 이미 저장된
    사진(메뉴가 '폴더열기'), 시간 안에 파일이 안 생김. 어느 쪽이든 질문은 글만으로
    계속 처리하면 된다.

    사진 줄이 그림을 다시 그리는 중이면 잠깐 기다렸다 다시 본다 (실측 3초가량).
    **어떤 경로로 끝나든 연 메뉴는 닫고 스크롤은 되돌린다.** 2026-09-21 시험 코드가
    항목을 못 찾은 채 끝나 카카오톡 창에 메뉴가 1분쯤 열려 있었다.
    """
    from .thread import (
        ThreadError,
        _AX,
        _context_menu,
        _restore_scroll,
        _scroll_into_view,
    )

    ax = ax or _AX()
    win = next((w for w in ax.windows() if ax.attr(w, "AXTitle") == room), None)
    if win is None:
        raise PhotoError(f"'{room}' 방이 창으로 열려 있지 않습니다")

    def target():
        """(사진 칸, 질문을 찾았는지). 같은 질문이 여러 번이면 가장 최근 것."""
        rows = _rows(ax, win)
        q = next(
            (i for i in range(len(rows) - 1, -1, -1) if _text_of(ax, rows[i]) == question_text),
            None,
        )
        if q is None:
            return None, False
        t = q + offset
        if 0 <= t < len(rows) and _is_photo(ax, rows[t]):
            return rows[t], True
        return None, True

    cell, found_question = target()
    for _ in range(tries - 1):
        if cell is not None or not found_question:
            break
        time.sleep(wait)  # 사진 줄이 그림을 다시 그리는 중일 수 있다
        cell, found_question = target()
    if cell is None:
        log.info(
            "사진 말풍선을 찾지 못했습니다 (질문 %s, 거리 %+d)",
            "있음" if found_question else "없음",
            offset,
        )
        return None

    before = _existing(save_dir)
    scrolled = None
    menu = None
    try:
        try:
            scrolled = _scroll_into_view(ax, win, cell)
        except ThreadError as exc:
            raise PhotoError(str(exc)) from exc
        if scrolled:
            cell, _ = target()  # 스크롤하면 요소를 다시 잡는다
            if cell is None:
                return None
        if ax.perform(cell, "AXShowMenu") != 0:
            return None
        menu = _context_menu(ax, win)
        if menu is None:
            return None
        item = next(
            (
                e
                for e in ax.walk(menu, max_depth=3)
                if ax.attr(e, "AXRole") == "AXMenuItem"
                and ax.attr(e, "AXTitle") == SAVE_ITEM
            ),
            None,
        )
        if item is None:
            log.info("메뉴에 '%s'가 없습니다 (이미 저장한 사진?)", SAVE_ITEM)
            return None
        ax.perform(item, "AXPick")
        menu = None  # 항목을 고르면 메뉴는 스스로 닫힌다
        return wait_for_new_file(save_dir, before, timeout)
    finally:
        if menu is not None:
            ax.perform(menu, "AXCancel")
        leftover = _context_menu(ax, win, timeout=0.3)
        if leftover is not None:
            ax.perform(leftover, "AXCancel")
        _restore_scroll(ax, scrolled)


def read_and_discard(path: Path, max_chars: int = 1000) -> str:
    """글자를 읽고 **파일을 지운다.** 인식이 실패해도 지운다.

    지우는 파일은 봇이 방금 만든 것뿐이다 (저장 전후 목록 차이로 찾은 파일).
    """
    from .ocr import read_image

    text = ""
    try:
        text = read_image(path).text
    except Exception as exc:  # noqa: BLE001 — 사진 때문에 질문 처리가 멈추면 안 된다
        log.warning("사진 글자 인식 실패 (%s): %s", path.name, exc)
    finally:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("저장한 사진을 지우지 못했습니다 (%s): %s", path, exc)
    return text[:max_chars]
