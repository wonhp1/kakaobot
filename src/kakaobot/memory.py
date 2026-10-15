"""질문자별 대화 기억 (계획서 §6-2).

화면에서 긁어 맥락을 만들면 세 가지가 안 된다:
  1. 봇 자신의 답변이 빠진다 — 자기 메시지 필터가 거르므로
  2. 화면 밖으로 밀려난 발화가 사라진다 — ax-read 는 최근 N 개만 준다
  3. 오래된 맥락이 만료되지 않는다 — 어제 질문에 오늘 질문이 엮인다

그래서 서비스가 질문자별로 직접 기록한다. 트리거되지 않은 발화도 쌓는다 —
"저 질문이 있는데요" / "환불 규정이" / "얼리버드도 되나요?" 처럼 나눠 보내는 경우
앞의 두 줄이 마지막 질문의 맥락이기 때문이다.

전체 기록을 누적하지 않는다. 최근 N 턴 + 시간 만료의 슬라이딩 윈도우만 유지한다.
프로세스 메모리에만 있어 재시작하면 사라진다 — 맥락은 어차피 수십 분짜리다.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Turn:
    role: str       # "user" | "bot"
    text: str
    at: float       # time.monotonic()


class ConversationMemory:
    def __init__(self, turns: int = 5, max_age_secs: float = 1800) -> None:
        self._turns = turns
        self._max_age = max_age_secs
        # 사람당 turns*2 만큼만 보관 (사용자+봇 한 쌍이 한 턴)
        self._log: dict[str, deque[Turn]] = defaultdict(lambda: deque(maxlen=turns * 2))

    def add_user(self, asker: str, text: str, now: float | None = None) -> None:
        self._log[asker].append(Turn("user", text, now if now is not None else time.monotonic()))

    def add_bot(self, asker: str, text: str, now: float | None = None) -> None:
        self._log[asker].append(Turn("bot", text, now if now is not None else time.monotonic()))

    def recent(self, asker: str, now: float | None = None) -> list[Turn]:
        now = now if now is not None else time.monotonic()
        return [t for t in self._log.get(asker, ()) if now - t.at <= self._max_age]

    def history(self, asker: str, now: float | None = None) -> str:
        """LLM 에 넘길 형식. 비어 있으면 빈 문자열 — 재구성을 건너뛰는 신호다."""
        lines = []
        for t in self.recent(asker, now):
            who = "봇" if t.role == "bot" else asker
            lines.append(f"{who}: {t.text}")
        return "\n".join(lines)

    def forget(self, asker: str) -> None:
        self._log.pop(asker, None)

    def clear(self) -> None:
        self._log.clear()

    def configure(self, turns: int, max_age_secs: float) -> None:
        """설정 변경을 반영한다. 쌓인 기록은 유지한다."""
        self._max_age = max_age_secs
        if turns == self._turns:
            return
        self._turns = turns
        old = self._log
        self._log = defaultdict(lambda: deque(maxlen=turns * 2))
        for asker, turns_log in old.items():
            self._log[asker].extend(turns_log)   # maxlen 이 줄었으면 오래된 것부터 밀린다
