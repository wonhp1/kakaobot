"""봇이 최근에 보낸 문장 (자기 메시지 판별의 두 번째 관문).

답변 앞의 '🤖 [Sent via openkakao]' 접두어를 떼면서(2026-09-19) 사라진 이중 방어를
대신한다. 접두어는 방에 보였지만 이건 보이지 않는다.

첫 번째 관문은 말풍선 위치(outgoing)다. 창 크기가 바뀌는 등으로 그 판정이 틀리면
봇의 답이 남의 메시지로 읽힐 수 있고, 답에 물음표가 있으면 봇이 자기 말에 답하는
무한 루프가 생긴다. 봇은 자기가 보낸 문장을 정확히 알고 있으므로 대조한다.
"""

from __future__ import annotations

import time
from collections import deque


class SentLog:
    def __init__(self, capacity: int = 200, max_age_secs: float = 3600) -> None:
        self._items: deque[tuple[str, float]] = deque(maxlen=capacity)
        self._max_age = max_age_secs

    @staticmethod
    def _norm(text: str) -> str:
        # 카카오톡이 앞뒤 공백·줄바꿈을 다르게 보여줄 수 있어 정규화한다
        return " ".join(text.split())

    def add(self, text: str, now: float | None = None) -> None:
        if text:
            self._items.append((self._norm(text), now if now is not None else time.monotonic()))

    def contains(self, text: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        key = self._norm(text)
        return any(t == key and now - at <= self._max_age for t, at in self._items)

    def matches_clipped(self, clipped: str, now: float | None = None) -> bool:
        """앞부분만 보이는 글('…' 로 잘림)이 봇이 최근 보낸 문장인가 (댓글 알림용)."""
        now = now if now is not None else time.monotonic()
        key = self._norm(clipped.replace(chr(0x200B), ""))
        if key.endswith("…"):
            stem = key[:-1].rstrip()
            return bool(stem) and any(
                t.startswith(stem) and now - at <= self._max_age for t, at in self._items
            )
        return self.contains(key, now)
