"""카카오톡 대화 내보내기 → 지식베이스 조각 초안.

    # 1) 카카오톡에서 대화방 → 메뉴 → 대화 내용 내보내기 (.txt)
    # 2) 누가 운영자인지 확인 (발신자 목록만 보고 끝냄)
    .venv/bin/python scripts/build_knowledge.py 대화.txt
    # 3) 초안 만들기
    .venv/bin/python scripts/build_knowledge.py 대화.txt --operator "홍길동"
    # 4) data/knowledge/draft/ 의 초안을 검수해 data/knowledge/ 로 옮긴다

하는 일:
  ① 파싱        내보내기 파일의 두 가지 형식을 모두 읽는다
  ② 짝짓기      수강생 질문 → 그 뒤 운영자 답변
  ③ 개인정보 제거  이름·전화·이메일·계좌·링크 (**LLM 에 보내기 전에** 여기서 지운다)
  ④ 주제 묶기    같은 뜻의 질문을 임베딩으로 묶는다 (이 Mac 안에서)
  ⑤ 초안 작성    묶음마다 '질문을 제목으로 한 짧은 조각'을 만든다 (LLM, --no-llm 으로 끌 수 있음)

하지 않는 일 — 사람이 해야 한다:
  - 옛 답과 지금 규정이 다를 때 무엇이 맞는지 고르기 (⚠️ 로 표시만 한다)
  - 개별 사정("이번만 봐드릴게요")을 규정에서 걸러내기
  - 최종 문장 다듬기

조각을 짧게, 제목을 수강생 말투로 두는 이유는 docs/rag-tuning.md 측정 참조
("얼마에요" 0.519 → 제목으로 쓰면 0.592).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import config as config_mod  # noqa: E402
from kakaobot.kakao import _SYSTEM_NOTICE  # noqa: E402
from kakaobot.pipeline import TriggerJudge  # noqa: E402

# --- 내보내기 형식 (2026-09-20 실제 파일로 확인) ----------------------------
# macOS 카카오톡:  2026. 7. 7. 23:47, 이름 : 본문
#                 2026. 7. 7. 22:32: 홍길동님이 들어왔습니다.   ← 공지(콤마 없음)
#                 2026년 7월 7일 화요일                          ← 날짜 구분선
# 모바일:          [이름] [오후 1:26] 본문   (날짜 구분선은 --- 로 감쌈)
_MAC = re.compile(
    r"^(?P<y>\d{4})\. (?P<m>\d{1,2})\. (?P<d>\d{1,2})\. (?P<h>\d{1,2}):(?P<min>\d{2}), "
    r"(?P<who>.+?) : (?P<text>.*)$"
)
_MAC_NOTICE = re.compile(r"^\d{4}\. \d{1,2}\. \d{1,2}\. \d{1,2}:\d{2}: ")
_MAC_AMPM = re.compile(
    r"^(?P<y>\d{4})년 (?P<m>\d{1,2})월 (?P<d>\d{1,2})일 (?P<ampm>오전|오후) "
    r"(?P<h>\d{1,2}):(?P<min>\d{2}), (?P<who>.+?) : (?P<text>.*)$"
)
_MOBILE = re.compile(
    r"^\[(?P<who>.+?)\] \[(?P<ampm>오전|오후) (?P<h>\d{1,2}):(?P<min>\d{2})\] (?P<text>.*)$"
)
_DATE_LINE = re.compile(r"^-*\s*(?P<y>\d{4})년 (?P<m>\d{1,2})월 (?P<d>\d{1,2})일.*?-*$")
_HEADER = re.compile(r"^(\ufeff?Talk_|저장한 날짜)")

# 본문이 아닌 것
_NOT_TEXT = {
    "사진",
    "동영상",
    "이모티콘",
    "음성메시지",
    "삭제된 메시지입니다.",
    "(이모티콘)",
}
_ATTACH = re.compile(r"^(파일: |사진 \d+장|샵검색: )")

# 개인정보 — LLM 에 보내기 전에 지운다
_SCRUB = [
    (re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]"),
    (re.compile(r"\b\d{2,6}[- ]\d{2,6}[- ]\d{2,7}\b"), "[계좌번호]"),
    (re.compile(r"(https?://|www\.)\S+"), "[링크]"),
]


@dataclass
class Msg:
    at: datetime
    who: str
    text: str
    # 이 발화에 사진·동영상이 딸려 있었나 (merge_bursts 가 채운다)
    has_photo: bool = False


@dataclass
class QA:
    at: datetime
    question: str
    answer: str
    has_photo: bool = False


def parse_export(path: Path) -> tuple[list[Msg], list[str]]:
    """내보내기 파일을 메시지 목록으로. 못 읽은 줄도 함께 돌려준다."""
    messages: list[Msg] = []
    unparsed: list[str] = []
    day: datetime | None = None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _HEADER.match(line) or _MAC_NOTICE.match(line):
            continue  # 파일 머리말, 입퇴장 공지
        if m := _DATE_LINE.match(line):
            day = datetime(int(m["y"]), int(m["m"]), int(m["d"]))
            continue
        m = _MAC.match(line) or _MAC_AMPM.match(line) or _MOBILE.match(line)
        if m:
            groups = m.groupdict()
            hour = int(m["h"])
            if groups.get("ampm"):  # 12시간제 표기
                hour = hour % 12 + (12 if m["ampm"] == "오후" else 0)
            if groups.get("y"):
                base = datetime(int(m["y"]), int(m["m"]), int(m["d"]))
            elif day is not None:
                base = day
            else:
                unparsed.append(line)
                continue
            messages.append(
                Msg(
                    base + timedelta(hours=hour, minutes=int(m["min"])),
                    m["who"].strip(),
                    m["text"].strip(),
                )
            )
            continue
        if messages and not line.startswith("---"):
            messages[-1].text += "\n" + line  # 여러 줄 메시지의 이어지는 줄
        else:
            unparsed.append(line)
    return messages, unparsed


def merge_bursts(messages: list[Msg], gap_secs: int = 60) -> list[Msg]:
    """같은 사람이 잇따라 보낸 발화를 하나로 합친다.

    카톡에서는 한 질문을 서너 번 나눠 보낸다. 합치지 않으면 질문 하나가 여러 조각으로
    갈리고 짝짓기도 어긋난다 (실측: 4,415개 → 3,388개).
    """
    out: list[Msg] = []
    for m in messages:
        # 사진·동영상은 내보내기에서 "사진" 한 단어로 나온다. 그대로 붙이면
        # "사진 이곳 비번은 뭔가요?" 같은 질문이 생긴다. 글자는 버리되
        # **딸려 있었다는 사실은 남긴다** — 화면이 필요한 질문이라는 확실한 증거다.
        photo = m.text.strip() in _NOT_TEXT or bool(_ATTACH.match(m.text.strip()))
        text = "" if photo else m.text
        if (
            out
            and out[-1].who == m.who
            and (m.at - out[-1].at).total_seconds() <= gap_secs
        ):
            out[-1].text = f"{out[-1].text} {text}".strip()
            out[-1].has_photo = out[-1].has_photo or photo
        else:
            out.append(Msg(m.at, m.who, text, photo))
    return [m for m in out if m.text or m.has_photo]


# 화면을 같이 봐야만 뜻이 통하는 말.
# (실측: 수강생 단톡방 질문의 36%가 이런 형태 — "여기 맞을까요", "이렇게 나와요 ㅠㅠ")
_DEIXIS_START = re.compile(
    r"^(이거|이게|여기|저기|그거|이렇게|저렇게|이건|맞나요|맞을까요|그럼)"
)
_DEIXIS_ANY = re.compile(r"(이거|이게|여기|이렇게|이런식|저거|그거|이런거|위에|아래)")


def self_contained(question: str) -> bool:
    """혼자 읽어도 뜻이 통하는 질문인가."""
    q = re.sub(r"[?？!.,~ㅠㅜㅎㅋ\s]+$", "", question)
    if len(q) < 8 or _DEIXIS_START.match(q):
        return False
    return not (_DEIXIS_ANY.search(q) and len(q) < 25)


def needs_screen(question: str, had_photo: bool = False) -> bool:
    """혼자서는 안 통하지만, **화면만 붙이면** 통하는 질문인가.

    이런 짝을 버리면 안 된다. 질문이 "이거 맞나요" 라 쓸모없어 보여도, 그때
    올라온 화면에는 "zsh: command not found" 같은 **검색 가능한 말**이 적혀
    있다. 화면을 문서에 붙여 두면(kakaobot.ocr) 그 말이 검색어가 되어,
    나중에 같은 화면을 본 다른 사람이 그 문구를 글로 쳐서 물었을 때 걸린다.

    판단 근거는 둘이다.
      - **사진이 실제로 딸려 있었다** (had_photo) — 추측이 아니라 기록이다.
        지시어가 없는 질문이어도, 사진이 있었다면 그 화면에 답의 근거가 있다.
      - 지시어뿐이라 글만으로는 안 통한다 ("이거 맞나요").

    너무 짧은 말("넵", "감사합니다")은 화면을 붙여도 건질 게 없으므로 뺀다.
    """
    q = re.sub(r"[?？!.,~ㅠㅜㅎㅋ\s]+$", "", question)
    if len(q) < 4:
        return False
    if had_photo:
        return True
    if self_contained(question):
        return False
    return bool(_DEIXIS_START.match(q) or _DEIXIS_ANY.search(q))


def is_content(text: str) -> bool:
    t = text.strip()
    return (
        bool(t)
        and t not in _NOT_TEXT
        and not _ATTACH.match(t)
        and not _SYSTEM_NOTICE.search(t)
    )


def scrub(text: str, names: list[str]) -> str:
    out = text
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 2:
            out = out.replace(name, "○○")
    for pattern, replacement in _SCRUB:
        out = pattern.sub(replacement, out)
    return out


def pair_qa(
    messages: list[Msg], operator: str, window_min: int, min_answer: int
) -> list[QA]:
    """수강생 질문 → 그 뒤 운영자 답변. 답이 없거나 너무 짧으면 버린다."""
    judge = TriggerJudge()
    pairs: list[QA] = []
    for i, m in enumerate(messages):
        if m.who == operator or not is_content(m.text):
            continue
        if not (
            m.text.rstrip().endswith(("?", "？")) or judge.looks_like_question(m.text)
        ):
            continue
        answer: list[str] = []
        for nxt in messages[i + 1 :]:
            if nxt.at - m.at > timedelta(minutes=window_min):
                break
            if nxt.who == operator:
                if is_content(nxt.text):
                    answer.append(nxt.text.strip())
            elif answer:
                break  # 운영자 답변이 끝났다
            elif nxt.text.rstrip().endswith(("?", "？")):
                break  # 답 오기 전에 다른 질문이 왔다
        body = "\n".join(answer).strip()
        if len(body) >= min_answer:
            pairs.append(QA(m.at, m.text.strip(), body, m.has_photo))
    return pairs


def cluster(pairs: list[QA], model_name: str, threshold: float) -> list[list[QA]]:
    """**답변**을 기준으로 묶는다. 이 Mac 안에서만 계산한다.

    질문으로 묶으면 두 가지가 어긋난다 (2026-09-20 시험):
      - "환불 되나요"(환불 비율)와 "환불 어떻게 신청해요"(신청 절차)는 질문이 비슷해
        한 덩어리가 되지만 답이 전혀 다르다 → 한쪽 답이 사라진다
      - "얼마에요"(옛 39만원)와 "수강료가 얼마인가요"(49만원)는 질문이 덜 비슷해 갈라지고,
        옛 금액이 경고 없이 조각으로 남는다
    한 조각은 '하나의 사실'이어야 하므로 같은 사실을 말하는 답끼리 묶는다.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    vectors = model.encode(
        [p.answer for p in pairs], normalize_embeddings=True, show_progress_bar=False
    )
    groups: list[list[int]] = []
    centers: list = []
    for i, vector in enumerate(vectors):
        best, score = -1, threshold
        for g, center in enumerate(centers):
            similarity = float(center @ vector)
            if similarity >= score:
                best, score = g, similarity
        if best < 0:
            groups.append([i])
            centers.append(vector)
        else:
            groups[best].append(i)
            members = groups[best]
            centers[best] = sum(vectors[j] for j in members) / len(members)
    groups.sort(key=len, reverse=True)
    return [[pairs[i] for i in g] for g in groups]


DRAFT_PROMPT = """오픈채팅 운영자의 실제 답변 기록을 지식베이스 조각으로 정리한다.

출력 형식 (이것만 출력한다):
## <수강생이 가장 흔하게 쓰는 질문 표현 그대로>
<답변 2~3문장>

규칙:
- **다시 물어볼 만한 일반적인 질문이 아니면** (그 사람 화면·그날 사정에만 해당하는 것)
  다른 말 없이 `SKIP` 한 줄만 출력한다.
- **주어진 답변에 있는 내용만** 쓴다. 없는 사실을 채우지 않는다.
- 답이 "아니오/불가"면 제목에 결론을 박는다: `## 환불 되나요 (→ 불가)`.
  임베딩은 "가능"과 "불가"를 잘 구분하지 못한다.
- 제목은 수강생의 말투 그대로 쓴다. "수강료 안내" 같은 요약 제목을 쓰지 않는다.
- 답변이 서로 어긋나면(예: 금액이 다름) 정리하지 말고 첫 줄에 `⚠️ 답변이 엇갈립니다` 를 쓰고
  각각을 날짜와 함께 나열한다.
- 특정 개인의 사정으로 보이면(예: "이번만", "○○님은") 첫 줄에 `⚠️ 개별 사정일 수 있습니다` 를 쓴다.
- 링크·전화번호·계좌번호는 넣지 않는다. 마크다운 강조(**)를 쓰지 않는다."""


# 화면을 봐야 뜻이 통하는 짝 전용. 질문이 "이거 맞나요" 라 제목으로 쓸 수 없으니,
# **답변에서** 무슨 상황이었는지를 되짚어 제목을 짓게 한다.
DRAFT_PROMPT_SCREEN = """오픈채팅 운영자의 답변 기록을 지식베이스 조각으로 정리한다.
질문은 화면(스크린샷)을 같이 봐야 뜻이 통하는 말이라 그대로 쓸 수 없다.

출력 형식 (이것만 출력한다):
## <어떤 상황인지 — 수강생이 그 화면을 보고 글로 칠 법한 말>
<답변 2~3문장>

규칙:
- 제목은 **답변 내용에서 되짚어** 짓는다. 답이 "터미널 껐다 켜세요" 면
  `## 터미널에서 명령어가 먹히지 않을 때` 처럼 상황을 적는다.
- 답변만 봐서는 무슨 상황인지 **짐작할 수 없으면** 다른 말 없이 `SKIP` 한 줄만 출력한다.
  억지로 지어내면 엉뚱한 화면에 이 답이 달린다.
- 화면에 떴을 법한 오류 문구를 **지어내지 않는다.** 그건 운영자가 그림을 붙이면
  기계가 직접 읽는다.
- 주어진 답변에 있는 내용만 쓴다. 답이 "아니오/불가"면 제목에 결론을 박는다.
- 링크·전화번호·계좌번호는 넣지 않는다. 마크다운 강조(**)를 쓰지 않는다."""


# 규정이 아니라 그 사람만의 예외일 때 쓰는 말 (2026-09-20 시험에서 실제로 걸림:
# "○○님은 사정이 있으시니 이번만 절반 돌려드릴게요" 가 환불 규정 조각이 될 뻔했다)
_PERSONAL = re.compile(r"(이번만|특별히|예외로|○○님은|님만|원래는 안 되지만)")


def warnings_for(group: list[QA]) -> list[str]:
    """사람이 반드시 봐야 할 것만 짚는다. 고치지는 않는다."""
    notes = []
    answers = {p.answer.strip() for p in group}
    if len(answers) > 1:
        notes.append(f"답변이 {len(answers)}가지로 엇갈립니다 — 지금 규정만 남기세요")
    if any(_PERSONAL.search(p.answer) for p in group):
        notes.append("개별 사정으로 보이는 답이 섞여 있습니다 — 규정이 아니면 지우세요")
    if any("[" in p.answer for p in group):
        notes.append("연락처·링크를 지운 자리가 있습니다 — 필요하면 다시 쓰세요")
    return notes


def variants(group: list[QA], limit: int = 3) -> list[str]:
    """같은 주제를 묻는 서로 다른 표현. 표현마다 조각을 따로 두면 검색이 는다
    (docs/rag-tuning.md: "얼마에요" 0.519 → 제목으로 쓰면 0.592)."""
    # 짧은 표현이 대개 더 전형적이다 ("얼마에요" > "저 이번에 사정이 생겨서 …")
    seen: list[str] = []
    for p in sorted(group, key=lambda q: len(q.question)):
        q = p.question.strip().rstrip("?？")
        if q and not any(q in s or s in q for s in seen):
            seen.append(q)
    return seen[:limit]


def slug(heading: str, limit: int = 28) -> str:
    """제목 → 그림 파일 이름. 운영자가 어느 항목의 그림인지 알아볼 수 있어야 한다."""
    s = re.sub(r"^#+\s*", "", heading).strip()
    s = re.sub(r"[^\w가-힣]+", "-", s).strip("-")
    return (s[:limit].rstrip("-") or "화면").lower()


def with_image_slots(text: str) -> str:
    """조각마다 그림 자리를 하나씩 둔다.

    한 묶음에서 제목이 여러 개 나올 수 있는데(같은 답을 여러 표현으로), 검색
    단위는 제목마다 따로다. 한 군데만 붙이면 나머지는 화면 글자 없이 색인된다.
    같은 화면을 가리키게 두고, 다른 그림을 쓰고 싶으면 운영자가 경로만 바꾼다.
    """
    out: list[str] = []
    for line in text.splitlines():
        out.append(line)
        if line.startswith("## "):
            out.append("")
            out.append(f"![화면](images/{slug(line)}.png)")
    return "\n".join(out)


def evidence(group: list[QA], limit: int = 4) -> str:
    """검수자가 확인할 원문. 요약만 보면 검증이 아니라 재작성을 하게 된다."""
    lines = []
    for p in sorted(group, key=lambda q: q.at)[:limit]:
        lines.append(f"     [{p.at:%Y-%m-%d %H:%M}] 질문: {p.question[:90]}")
        lines.append(f"                       답변: {p.answer[:120]}")
    if len(group) > limit:
        lines.append(f"     … 외 {len(group) - limit}건")
    return "\n".join(lines)


def tidy_draft(text: str) -> str:
    """LLM 이 얹은 군더더기를 걷어낸다. 걷어낼 게 없으면 빈 글자.

    2026-09-20 실전에서 실제로 나온 오염:
      - "대량의 실제 Q&A 기록 정리 요청이네요. 체계적으로 진행하겠습니다." (머리말)
      - "안녕하세요. 시스템이 로드된 상태입니다." (인사)
      - "## SKIP" (건너뛰라는 뜻인데 제목 모양이라 그대로 조각이 됐다)
    형식을 어긴 답은 고쳐 쓰지 않고 버린다 — 규칙 기반 초안(draft_plain)이 낫다.
    """
    body = text.strip()
    if not body:
        return ""
    # 'SKIP' 만 있는 줄이 어디 있든 건너뛰기로 본다 ('## SKIP' 포함)
    for line in body.splitlines():
        if re.fullmatch(r"#*\s*SKIP\s*[.。]?", line.strip(), re.IGNORECASE):
            return "SKIP"
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        return ""  # 제목이 아예 없다 — 형식을 못 지켰다
    kept = "\n".join(lines[start:]).strip()
    # 제목만 있고 답이 없으면 쓸 수 없다
    return kept if len(kept.splitlines()) > 1 else ""


def draft_with_llm(group: list[QA], adapter, prompt: str = DRAFT_PROMPT) -> str:
    lines = [
        f"[{p.at:%Y-%m-%d}] 질문: {p.question}\n           답변: {p.answer}"
        for p in group[:12]
    ]
    answer = adapter.complete(prompt, "\n".join(lines))
    if not answer.ok:
        return ""
    return tidy_draft(answer.text)


def draft_plain(group: list[QA]) -> str:
    """LLM 없이: **가장 최근 답변 하나**를 정답으로 삼고, 표현마다 조각을 만든다.

    옛 답과 새 답이 표현별로 흩어지면 봇이 옛 금액을 말한다 (시험에서 39만원/49만원이
    갈렸다). 답은 하나로 모으고, 제목만 여러 개 둔다.
    """
    # 개별 사정 답변은 정답으로 쓰지 않는다. 그것뿐이면 어쩔 수 없이 쓰되 ⚠️ 가 붙는다.
    general = [p for p in group if not _PERSONAL.search(p.answer)] or group
    answer = max(general, key=lambda p: p.at).answer
    return "\n\n".join(f"## {q}\n{answer}" for q in variants(group))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", type=Path, help="카카오톡 대화 내보내기 .txt")
    ap.add_argument(
        "--operator", help="운영자(답변자) 이름. 빼면 발신자 목록만 보여준다"
    )
    ap.add_argument("--out", type=Path, default=ROOT / "data/knowledge/draft")
    ap.add_argument(
        "--no-llm", action="store_true", help="LLM 없이 최근 답변을 그대로 쓴다"
    )
    ap.add_argument(
        "--max-topics",
        type=int,
        default=30,
        help="초안으로 만들 주제 수 (많이 물어본 순)",
    )
    ap.add_argument(
        "--window-min",
        type=int,
        default=120,
        help="질문 뒤 이 시간 안의 답변만 짝짓는다",
    )
    ap.add_argument(
        "--min-answer", type=int, default=10, help="이보다 짧은 답변은 버린다"
    )
    ap.add_argument("--cluster-threshold", type=float, default=0.75)
    ap.add_argument(
        "--min-cluster",
        type=int,
        default=2,
        help="이만큼 반복된 주제만 초안으로 만든다 (1이면 전부)",
    )
    args = ap.parse_args()

    messages, unparsed = parse_export(args.export)
    print(
        f"메시지 {len(messages)}개 읽음"
        + (f" / 못 읽은 줄 {len(unparsed)}개" if unparsed else "")
    )
    if unparsed[:3]:
        for line in unparsed[:3]:
            print(f"   못 읽음: {line[:60]!r}")
    if not messages:
        print(
            "형식을 못 읽었습니다. 파일 앞부분 몇 줄을 보여주시면 맞추겠습니다.",
            file=sys.stderr,
        )
        return 1

    senders = Counter(m.who for m in messages)
    if not args.operator:
        print("\n발신자 (많은 순):")
        for who, n in senders.most_common(12):
            print(f"   {n:5}건  {who}")
        print('\n운영자를 골라 다시 실행하세요:  --operator "이름"')
        return 0
    if args.operator not in senders:
        print(
            f"'{args.operator}' 이름을 찾지 못했습니다. 위 목록에서 고르세요.",
            file=sys.stderr,
        )
        return 1

    messages = merge_bursts(messages)
    pairs = pair_qa(messages, args.operator, args.window_min, args.min_answer)
    screen_pairs = [p for p in pairs if needs_screen(p.question, p.has_photo)]
    kept = [p for p in pairs if p not in screen_pairs and self_contained(p.question)]
    print(
        f"질문-답변 짝 {len(pairs)}개 → 혼자 읽어도 통하는 것 {len(kept)}개,"
        f" 화면을 붙이면 살릴 수 있는 것 {len(screen_pairs)}개"
    )
    if not kept and not screen_pairs:
        print("짝을 찾지 못했습니다. --window-min 을 늘려 보세요.", file=sys.stderr)
        return 1

    names = [w for w in senders if w != args.operator]
    for p in kept + screen_pairs:
        p.question, p.answer = scrub(p.question, names), scrub(p.answer, names)

    cfg = config_mod.load(ROOT / "config/config.toml")
    model_name = cfg.get("rag", "embed_model", "BAAI/bge-m3")

    def topics(pairs: list[QA], label: str, least: int) -> list[list[QA]]:
        if not pairs:
            return []
        groups = cluster(pairs, model_name, args.cluster_threshold)
        rare = [g for g in groups if len(g) < least]
        groups = [g for g in groups if len(g) >= least]
        tail = (
            f" (한 번뿐인 {len(rare)}개는 뺍니다 — 대개 그날 그 사람 사정)"
            if rare
            else ""
        )
        print(
            f"{label}: 주제 {len(groups) + len(rare)}개 중 쓸 것 {len(groups)}개{tail}"
        )
        return groups

    groups = topics(kept, "글만으로 통하는 것", args.min_cluster)
    # 화면 쪽에서 **반복된** 주제만 초안으로 만든다. 1회성 질문은 답변만 봐서는
    # 무슨 화면이었는지 짐작할 수 없어 LLM 이 거의 다 건너뛴다 (2026-09-20: 8개 중 7개).
    # 대신 아래에서 목록으로 내놓는다 — 운영자가 그림 있는 것만 골라 쓰면 된다.
    screen_groups = topics(screen_pairs, "화면이 필요한 것", args.min_cluster)
    drafted = {id(p) for g in screen_groups for p in g}
    screen_singles = [p for p in screen_pairs if id(p) not in drafted]
    if not groups and not screen_groups:
        print(
            "반복된 주제가 없습니다. --min-cluster 1 로 모두 볼 수 있습니다.",
            file=sys.stderr,
        )
        return 1

    adapter = None
    if not args.no_llm:
        from kakaobot.llm import build_adapter

        adapter = build_adapter(cfg)
        print(
            f"초안 작성 중 — {cfg.mode} 로 주제마다 한 번씩 부릅니다. 개인정보는 이미 지웠습니다."
        )

    flagged: list[tuple[str, list[str]]] = []

    def draft_all(groups: list[list[QA]], prompt: str, label: str) -> tuple[list, int]:
        sections, skipped = [], 0
        total = min(len(groups), args.max_topics)
        for i, group in enumerate(groups[: args.max_topics], 1):
            text = (
                draft_with_llm(group, adapter, prompt)
                if adapter
                else draft_plain(group)
            )
            if not text:
                text = draft_plain(group)
            if text.strip().upper().startswith("SKIP"):
                skipped += 1
                print(f"  [{label}] {i}/{total} (일반 질문 아님 — 건너뜀)")
                continue
            notes = warnings_for(group)
            note_lines = "".join(f"\n     ⚠️ {n}" for n in notes)
            body = text
            if prompt is DRAFT_PROMPT_SCREEN:
                # 제목 바로 아래에 그림 자리를 만든다. 운영자가 파일을 넣으면
                # 그 화면의 글자가 검색어가 된다 (kakaobot.ocr).
                # 제목 **하나하나가** 따로 조각이 되므로 (rag.split_markdown)
                # 모든 제목에 붙여야 한다 — 첫 제목에만 붙이면 나머지 조각은
                # 화면 글자 없이 색인된다.
                body = with_image_slots(text)
            sections.append(
                f"{body}\n\n<!-- 근거 {len(group)}건{note_lines}\n{evidence(group)}\n-->"
            )
            head = text.splitlines()[0][:38]
            if notes:
                flagged.append((head, notes))
            print(f"  [{label}] {i}/{total} {'⚠️ ' if notes else '   '}{head}")
        return sections, skipped

    sections, skipped = draft_all(groups, DRAFT_PROMPT, "글")
    screen_sections, screen_skipped = draft_all(
        screen_groups, DRAFT_PROMPT_SCREEN, "화면"
    )

    args.out.mkdir(parents=True, exist_ok=True)
    written = []

    def write_draft(name: str, header: str, sections: list[str]) -> None:
        if not sections:
            return
        out = args.out / name
        out.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
        try:
            shown = out.relative_to(ROOT)
        except ValueError:
            shown = out
        written.append((len(sections), shown))

    def screen_shortlist(pairs: list[QA], limit: int = 60) -> str:
        """1회성 화면 질문을 고르기 쉬운 목록으로. LLM 을 부르지 않는다."""
        if not pairs:
            return ""
        rows = ["\n## 아직 정리하지 않은 화면 질문 (고르는 용도)\n"]
        rows.append(
            f"한 번씩만 나온 질문 {len(pairs)}개입니다. 답변만으로는 어떤 화면이었는지\n"
            f"알 수 없어 초안을 만들지 않았습니다. **지금도 자주 오는 화면**이 보이면,\n"
            f"위 형식대로 조각을 직접 쓰고 그림을 붙이세요.\n"
        )
        for p in sorted(pairs, key=lambda q: q.at, reverse=True)[:limit]:
            mark = "📷" if p.has_photo else "  "
            rows.append(
                f"- {mark} [{p.at:%m-%d}] {p.question[:60]}\n"
                f"       → {p.answer[:100]}"
            )
        if len(pairs) > limit:
            rows.append(
                f"\n… 외 {len(pairs) - limit}개 (최근 것부터 {limit}개만 보였습니다)"
            )
        return "\n".join(rows) + "\n"

    today = f"{datetime.now():%Y-%m-%d}"
    write_draft(
        f"{today}-초안.md",
        f"# 대화 기록에서 뽑은 초안 ({args.export.name})\n\n"
        f"> 검수 전입니다. ⚠️ 표시를 먼저 보고, 지금 규정이 아닌 답은 지우세요.\n"
        f"> 검수가 끝나면 이 파일을 data/knowledge/ 로 옮겨야 봇이 씁니다.\n\n",
        sections,
    )
    write_draft(
        f"{today}-화면필요.md",
        f"# 화면을 붙여야 살아나는 초안 ({args.export.name})\n\n"
        f'> 질문이 "이거 맞나요" 처럼 화면을 봐야 통하는 것들입니다. 버리지 않고 모았습니다.\n'
        f"> 각 항목의 `![화면](images/….png)` 자리에 **그때 올라온 스크린샷**을 넣으세요.\n"
        f"> 넣으면 화면 속 글자를 기계가 읽어 검색어로 씁니다 — 같은 화면을 본 다른 사람이\n"
        f"> 그 문구를 글로 쳐서 물었을 때 이 답이 걸립니다. 화면 속 글자는 검색에만 쓰고\n"
        f"> 답변 근거로는 안 쓰므로, 답이 틀리면 본문을 직접 고치세요.\n"
        f"> 그림을 못 구하면 그 항목은 지우세요 (제목만으로는 잘 안 걸립니다).\n\n",
        screen_sections
        + ([shortlist] if (shortlist := screen_shortlist(screen_singles)) else []),
    )

    print()
    for n, shown in written:
        print(f"초안 {n}개 → {shown}")
    dropped = skipped + screen_skipped
    if dropped:
        print(f"(일반 질문이 아니라 건너뛴 것 {dropped}개)")
    if flagged:
        print(f"\n사람이 봐야 할 것 {len(flagged)}개:")
        for head, notes in flagged:
            print(f"  {head}")
            for n in notes:
                print(f"     ⚠️ {n}")
    print(
        "검수 순서: ⚠️ 표시 확인 → 옛 정보 삭제 → 제목이 수강생 말투인지 확인 → data/knowledge/ 로 이동"
    )
    print("옮긴 뒤: .venv/bin/python scripts/sweep_threshold.py 로 임계값 재측정")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
