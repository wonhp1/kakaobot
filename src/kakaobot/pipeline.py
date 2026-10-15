"""메시지 파이프라인의 판정 단계 (계획서 §6-1 [1]~[4]).

LLM 이전 단계만 담당한다. 모든 판정은 fail-closed — 불확실하면 통과시키지 않는다.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

from .kakao import Message

log = logging.getLogger("kakaobot.pipeline")


class Verdict:
    """왜 통과/탈락했는지 남긴다. 튜닝(D2, D3)의 근거가 된다.

    `strength` 는 이 판정을 얼마나 믿는가다.
      strong  물음표·멘션 — 분명히 나에게 묻는 말이다
      weak    어미·유사도로 **추측**한 것 — 답할 근거가 없으면 조용히 넘어간다
    """

    def __init__(self, passed: bool, stage: str, reason: str = "",
                 strength: str = "strong") -> None:
        self.passed = passed
        self.stage = stage
        self.reason = reason
        self.strength = strength

    @property
    def is_weak(self) -> bool:
        return self.strength == "weak"

    def __repr__(self) -> str:
        mark = "통과" if self.passed else "탈락"
        weak = "(추측)" if self.is_weak else ""
        return f"<{mark}{weak} @{self.stage}{': ' + self.reason if self.reason else ''}>"


@dataclass
class RateLimiter:
    """사용자별 쿨다운 + 방 전체 분당 상한 (계획서 §6-5)."""

    user_cooldown_secs: int = 60
    room_per_minute: int = 5
    _last_by_user: dict[str, float] = field(default_factory=dict)
    _room_hits: deque[float] = field(default_factory=deque)

    def check(self, sender: str, now: float | None = None) -> Verdict:
        now = now if now is not None else time.monotonic()

        while self._room_hits and now - self._room_hits[0] > 60:
            self._room_hits.popleft()
        if len(self._room_hits) >= self.room_per_minute:
            return Verdict(
                False, "rate_limit",
                f"방 전체 분당 상한 {self.room_per_minute}건 초과",
            )

        last = self._last_by_user.get(sender)
        if last is not None and now - last < self.user_cooldown_secs:
            remain = int(self.user_cooldown_secs - (now - last))
            return Verdict(False, "rate_limit", f"'{sender}' 쿨다운 {remain}초 남음")

        return Verdict(True, "rate_limit")

    def check_room(self, now: float | None = None) -> Verdict:
        """방 전체 분당 상한만 본다. 스레드 안 추가 질문에 쓴다 — 봇의 답을 읽고
        곧바로 되묻는 것은 자연스러워서 1인 쿨다운을 걸지 않는다."""
        now = now if now is not None else time.monotonic()
        while self._room_hits and now - self._room_hits[0] > 60:
            self._room_hits.popleft()
        if len(self._room_hits) >= self.room_per_minute:
            return Verdict(False, "rate_limit", f"방 전체 분당 상한 {self.room_per_minute}건 초과")
        return Verdict(True, "rate_limit")

    def commit(self, sender: str, now: float | None = None) -> None:
        """실제로 답변했을 때만 호출한다."""
        now = now if now is not None else time.monotonic()
        self._last_by_user[sender] = now
        self._room_hits.append(now)


@dataclass
class CircuitBreaker:
    """연속 오류 N회 → 정지. 상태 불명이면 차단으로 친다 (계획서 §6-5)."""

    max_consecutive_errors: int = 3
    _errors: int = 0
    _tripped: bool = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    def record_success(self) -> None:
        self._errors = 0

    def record_error(self) -> None:
        self._errors += 1
        if self._errors >= self.max_consecutive_errors:
            self._tripped = True
            log.error(
                "서킷 브레이커 작동 — 연속 오류 %d회. 자동 정지합니다.", self._errors
            )

    def reset(self) -> None:
        self._errors = 0
        self._tripped = False


# 물음표 없는 한국어 질문 (D2 보강, 2026-09-20). 문장 끝의 의문 어미.
_QUESTION_ENDING = re.compile(
    r"(나요|가요|까요|은가|인가|런가|는지|던가|나여|맞나|되나|될까|있나|없나"
    r"|어때|어떤가|뭐야|뭔가|얼만가|얼마야|언제야|죠|지요)"
    r"[\s.!~ㅎㅋ]*$"
)

# 묻는 행위를 드러내는 말. 문장 어디에 있어도 된다.
_ASKING = re.compile(r"(궁금|여쭤|여쭙|문의|질문\s*(있|이|드)|알려\s*주|가르쳐\s*주|설명\s*해\s*주)")

# 의문사. "얼마에요", "누구세요" 처럼 짧아도 질문이다 (2026-09-20 실전에서 확인).
_WH_WORD = re.compile(r"(언제|어디|누가|누구|무엇|무슨|어떤|어떻게|얼마|몇|왜|뭐|뭘)")


class TriggerJudge:
    """어떤 메시지에 답할지 정한다 (계획서 §6-1 [2], D2).

    D2 결정 (2026-09-19): 물음표로 끝나는 문장 + 운영자를 @멘션한 문장.

    멘션은 '@닉네임' 형태로만 인식한다. 예전엔 "봇" 같은 낱말의 부분 일치로 봤는데,
    "챗봇 만들고 싶어요" 처럼 그 글자가 들어간 모든 문장에 반응했다.
    """

    def __init__(
        self,
        on_question_mark: bool = True,
        on_mention: bool = True,
        mention_names: list[str] | None = None,
        min_length: int = 4,
        on_question_ending: bool = True,
    ) -> None:
        self._qmark = on_question_mark
        self._mention = on_mention
        self._ending = on_question_ending
        self._handles = [f"@{n.strip().lstrip('@')}" for n in (mention_names or []) if n.strip()]
        self._min_length = min_length

    def is_mention(self, text: str) -> bool:
        return self._mention and any(h in text for h in self._handles)

    def strip_mentions(self, text: str) -> str:
        """부르는 말(@닉네임)을 떼어낸 질문 내용만 남긴다.

        멘션은 '나에게 묻는다'는 신호일 뿐 질문 내용이 아니다. 그대로 두면 검색어가
        운영자 이름으로 오염된다 — 2026-09-20 측정: "준비물 뭐 챙겨야 하나요" 0.604 가
        "@홍길동 준비물 뭐 챙겨야 하나요" 에서는 0.534 로 떨어져 임계값(0.58) 아래가 됐다.
        """
        out = text
        for handle in self._handles:
            out = out.replace(handle, " ")
        return " ".join(out.split()) or text.strip()

    def looks_like_question(self, text: str) -> str:
        """물음표 없이도 질문처럼 보이는가. 보이면 그 사유, 아니면 빈 문자열.

        이것만으로 답하지 않는다 — 추측이므로 서비스가 지식베이스와 대조해
        근거가 있을 때만 답한다 (D2 보강, 2026-09-20).
        """
        if not self._ending:
            return ""
        t = text.strip()
        if _QUESTION_ENDING.search(t):
            return "의문 어미"
        if _ASKING.search(t):
            return "묻는 표현"
        if _WH_WORD.search(t):
            return "의문사"
        return ""

    def judge(self, m: Message) -> Verdict:
        # [1] 자기 메시지 필터 — 무한 루프 방지. 가장 먼저 본다.
        if m.is_own:
            return Verdict(False, "self_filter", "봇/운영자 자신의 메시지")

        if m.is_system_notice:
            return Verdict(False, "system_notice", "입퇴장 등 시스템 공지")

        text = m.text.strip()
        if not text:
            return Verdict(False, "empty", "본문 없음")

        if not m.sender:
            # 발신자를 모르면 레이트 리밋도 컨텍스트도 못 만든다 → fail-closed
            return Verdict(False, "no_sender", "발신자를 식별할 수 없음")

        if len(text) < self._min_length and not self.is_mention(text):
            return Verdict(False, "too_short", f"{len(text)}자 < {self._min_length}자")

        if self.is_mention(text):
            return Verdict(True, "trigger", "멘션")

        if self._qmark and text.endswith(("?", "？")):
            return Verdict(True, "trigger", "물음표로 끝남")

        return Verdict(False, "no_trigger", "트리거 조건 불일치")
