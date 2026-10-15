"""출력 검증 (계획서 §6-1 [9], §6-6 3·4항).

패치 004로 Touch ID 승인 게이트를 제거했으므로 **이 모듈이 마지막 방어선이다.**
사람의 검토 없이 나가는 텍스트를 여기서 막는다.
docs/patches/README.md 의 004 항목 참조.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("kakaobot.guard")

# URL — 오픈채팅에서 봇이 링크를 뿌리면 피싱 통로가 된다 (§6-6 4항)
_URL = re.compile(r"(https?://|www\.)\S+|\b[\w.-]+\.(?:com|net|org|kr|io|me|ly)\b", re.I)

# 계좌번호 — 인젝션으로 유도되는 대표적 사기 패턴
_ACCOUNT = re.compile(r"\b\d{2,6}[- ]\d{2,6}[- ]\d{2,7}\b")

# 전화번호
_PHONE = re.compile(r"\b01[0-9][- ]?\d{3,4}[- ]?\d{4}\b")

# 시스템 프롬프트 유출 신호 (§6-6 4항)
_LEAK = [
    "시스템 프롬프트", "system prompt", "你是", "너는 다음 지시를",
    "<user_message>", "</user_message>", "[근거 ", "이전 지시를 무시",
    "ignore previous", "ignore all previous", "disregard the above",
]

# 인젝션이 유도하는 전형적 문구
_SCAM = ["입금", "송금", "계좌로", "이체", "비밀번호", "인증번호", "OTP"]

# 막힘 단어는 **근거 문서의 문장 그대로**일 때만 통과시킨다 (2026-09-21 운영자 결정).
# 실전에서 "맥 로그인 비밀번호를 입력하세요"(홈브루 설치 안내)가 막혔다. 실제 대화
# 기록에도 "강의 비밀번호" 질문이 있다. 그렇다고 단어를 풀면 "비밀번호를 DM 으로
# 보내 주세요" 같은 주입 문구도 나간다.
#
# 단어 하나나 앞뒤 어절만 보면 안 된다. "비밀번호를 물으면 저에게 DM 으로…" 처럼
# 근거 조각으로 **시작해서** 딴말로 끝나는 문장이 통과한다. 그래서 막힘 단어가 든
# **문장 전체**가 근거에 그대로 있어야 한다. 서식·문장부호·띄어쓰기 차이만 무시한다.
_PUNCT = re.compile(r"[^\w\s]")
_SENTENCE_END = re.compile(r"[.!?。…]+|\n+")


def _plain(text: str) -> str:
    """비교용: 서식·문장부호를 떼고 공백을 하나로."""
    return " ".join(_PUNCT.sub(" ", to_chat_text(text)).split())


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(to_chat_text(text))
    return [s for s in (_plain(part) for part in parts) if s]


def grounded_sentences(text: str, word: str, grounds: str) -> bool:
    """답에서 word 가 든 문장이 **모두** 근거에 그대로 있는가."""
    if not grounds:
        return False
    plain_grounds = f" {_plain(grounds)} "
    hits = [s for s in _sentences(text) if word in s]
    return bool(hits) and all(f" {s} " in plain_grounds for s in hits)


# 카카오톡은 마크다운을 그리지 않고 기호를 그대로 보여준다.
# 2026-09-20 테스트방 실측: "- **1주차 종료 전**: 80% 환불" 이 별표째 올라갔다.
_CODE_FENCE = re.compile(r"^\s*```.*$", re.M)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_BLANK_LINES = re.compile(r"\n{3,}")


def to_chat_text(text: str) -> str:
    """마크다운 서식을 걷어내 채팅에 맞는 평문으로 만든다. 내용은 바꾸지 않는다.

    굵게·기울임·제목·코드 표시를 지우고, 글머리표(- * +)는 '· '로 바꾼다.
    """
    t = _CODE_FENCE.sub("", text or "")
    t = _BOLD.sub(r"\2", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub("· ", t)
    t = _ITALIC.sub(r"\1", t)
    t = _BLANK_LINES.sub("\n\n", t)
    return t.strip()


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    text: str
    reason: str = ""

    def __repr__(self) -> str:
        return f"<{'허용' if self.allowed else '차단'}{': ' + self.reason if self.reason else ''}>"


class OutputGuard:
    def __init__(
        self,
        max_chars: int = 500,
        block_links: bool = True,
        extra_banned: list[str] | None = None,
    ) -> None:
        self._max_chars = max_chars
        self._block_links = block_links
        self._banned = [w.lower() for w in (extra_banned or [])]

    def check(self, text: str, grounds: str = "") -> GuardResult:
        """grounds: 모델에게 준 근거 블록. 막힘 단어가 근거 문장 그대로일 때만 통과시킨다.
        비워 두면 예전처럼 막힘 단어가 나오기만 해도 막는다."""
        # 서식부터 걷어낸다 — 아래 검사는 실제로 나갈 문구에 대해 한다
        t = to_chat_text(text)

        if not t:
            return GuardResult(False, "", "빈 응답")

        if len(t) > self._max_chars:
            # 길이는 자르지 않고 차단한다. 중간에서 끊긴 답변은 오해를 부른다.
            return GuardResult(
                False, t, f"길이 초과 {len(t)}자 > {self._max_chars}자"
            )

        if self._block_links and _URL.search(t):
            return GuardResult(False, t, "외부 링크 포함")

        if _ACCOUNT.search(t):
            return GuardResult(False, t, "계좌번호 형태 포함")

        if _PHONE.search(t):
            return GuardResult(False, t, "전화번호 포함")

        low = t.lower()
        for marker in _LEAK:
            if marker.lower() in low:
                return GuardResult(False, t, f"프롬프트 유출 신호: {marker!r}")

        for word in _SCAM:
            if word in t and not grounded_sentences(t, word, grounds):
                return GuardResult(False, t, f"금전 유도 표현: {word!r} (근거에 없는 문장)")

        for word in self._banned:
            if word in low:
                return GuardResult(False, t, f"금칙어: {word!r}")

        return GuardResult(True, t)
