"""audit 기록에서 파인튜닝 후보를 뽑는다 (말투 튜닝용).

    # 1) 후보 뽑기 — 사람이 검수할 파일
    .venv/bin/python scripts/export_finetune.py
    # 2) data/finetune/candidates.jsonl 을 열어 나쁜 줄은 지우고, assistant 답을 원하는 말투로 고친다
    # 3) 학습용으로 확정 — meta 를 떼고 train/valid 로 나눈다
    .venv/bin/python scripts/export_finetune.py --finalize

원칙 (docs/local-llm.md §파인튜닝):
  - **사실이 아니라 말투를 가르친다.** 사실은 RAG 근거에서 온다. 그래서 예제는 추론 때와
    똑같이 <근거>·<최근_대화>·<user_message> 를 담은 형식으로 만든다.
  - **거절도 가르친다.** 모델이 스스로 '답변불가'라고 한 기록을 넣어야 튜닝 뒤에도 근거
    없을 때 지어내지 않는다.
  - 근거가 약해 LLM 을 부르지도 않은 에스컬레이션(stage=rag)과 출력 검증에 걸린 답은 뺀다.

개인정보: 질문자 닉네임은 '질문자'로, 전화번호·이메일은 자리표시로 바꾼다.
audit 는 30일 뒤 지워지지만 여기서 만든 파일은 지워지지 않는다 — 보관 여부는 운영 결정.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kakaobot import config as config_mod  # noqa: E402
from kakaobot.answering import NO_ANSWER  # noqa: E402
from kakaobot.llm import (
    build_system_prompt,
    build_user_prompt,
    prompt_sha,
)  # noqa: E402

OUT_DIR = ROOT / "data" / "finetune"
CANDIDATES = OUT_DIR / "candidates.jsonl"

_PHONE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(text: str, sender: str | None) -> str:
    if sender:
        text = text.replace(f"{sender}:", "질문자:")
    text = _PHONE.sub("[전화번호]", text)
    return _EMAIL.sub("[이메일]", text)


def iter_audit(log_dir: Path):
    for path in sorted((log_dir / "audit").glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield path.stem, json.loads(line)
                except json.JSONDecodeError:
                    continue


def to_example(rec: dict, system: str) -> tuple[dict | None, str]:
    """audit 한 줄 → 학습 예제. 못 쓰면 (None, 사유)."""
    event, stage = rec.get("event"), rec.get("stage")
    if event == "answer" and stage == "ok":
        target = rec.get("reply", "")
    elif event == "escalate" and stage == "llm":
        target = NO_ANSWER  # 근거 부족 판단을 그대로 가르친다
    else:
        return None, f"대상 아님({event}/{stage})"
    if not rec.get("context"):
        return None, "근거 기록 없음(이전 형식)"
    if not target:
        return None, "답 없음"

    sender = rec.get("sender")
    user = build_user_prompt(
        # llm_question: 멘션을 뗀 실제 입력. 추론 때와 같은 형식이어야 한다
        scrub(rec.get("llm_question") or rec.get("question", ""), sender),
        rec["context"],
        scrub(rec.get("history", ""), sender),
    )
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": scrub(target, sender)},
        ],
        # 검수용 정보. --finalize 때 뗀다
        "meta": {
            "kind": "refusal" if target == NO_ANSWER else "answer",
            "sources": rec.get("sources", []),
            "score": rec.get("score"),
            "model": rec.get("model"),
            "mode": rec.get("mode"),
            "same_prompt": rec.get("system_sha") == prompt_sha(system),
            "ts": rec.get("ts"),
        },
    }, ""


def export(since: str | None) -> int:
    cfg = config_mod.load(ROOT / "config" / "config.toml")
    system = build_system_prompt(cfg.get("style", "guide", ""))
    log_dir = ROOT / cfg.get("logging", "dir", "logs")

    seen: set[tuple[str, str]] = set()
    examples, skipped = [], Counter()
    for day, rec in iter_audit(log_dir):
        if since and day < since:
            continue
        ex, why = to_example(rec, system)
        if ex is None:
            skipped[why] += 1
            continue
        key = (ex["messages"][1]["content"], ex["messages"][2]["content"])
        if key in seen:
            skipped["중복"] += 1
            continue
        seen.add(key)
        examples.append(ex)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with CANDIDATES.open("w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(ex, ensure_ascii=False) + "\n")

    kinds = Counter(ex["meta"]["kind"] for ex in examples)
    other_prompt = sum(not ex["meta"]["same_prompt"] for ex in examples)
    print(f"후보 {len(examples)}개 → {CANDIDATES.relative_to(ROOT)}")
    print(f"  답변 {kinds['answer']} / 거절(답변불가) {kinds['refusal']}")
    if other_prompt:
        print(
            f"  ⚠️ 다른 시스템 프롬프트로 만든 기록 {other_prompt}개 — 현재 프롬프트로 다시 조립했습니다"
        )
    for why, n in skipped.most_common():
        print(f"  제외 {n}개: {why}")
    if examples and kinds["refusal"] / len(examples) < 0.1:
        print(
            "  ⚠️ 거절 예제가 10% 미만입니다. 튜닝 뒤 근거 없이 지어낼 위험이 커집니다."
        )
    print(
        "다음: 파일을 열어 나쁜 줄을 지우고 assistant 답을 원하는 말투로 고친 뒤 --finalize"
    )
    return 0


def finalize(valid_ratio: float, seed: int) -> int:
    if not CANDIDATES.exists():
        print("후보 파일이 없습니다. 먼저 인자 없이 실행하세요.", file=sys.stderr)
        return 1
    rows = []
    for n, line in enumerate(CANDIDATES.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ex = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"{n}번째 줄이 JSON 이 아닙니다 (검수 중 깨짐?): {exc}", file=sys.stderr
            )
            return 1
        roles = [m.get("role") for m in ex.get("messages", [])]
        if (
            roles != ["system", "user", "assistant"]
            or not ex["messages"][2]["content"].strip()
        ):
            print(
                f"{n}번째 줄의 messages 형식이 맞지 않습니다: {roles}", file=sys.stderr
            )
            return 1
        rows.append({"messages": ex["messages"]})

    random.Random(seed).shuffle(rows)
    n_valid = max(1, int(len(rows) * valid_ratio)) if len(rows) >= 10 else 0
    splits = {"valid.jsonl": rows[:n_valid], "train.jsonl": rows[n_valid:]}
    for name, part in splits.items():
        with (OUT_DIR / name).open("w", encoding="utf-8") as fh:
            for r in part:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(
        f"train {len(splits['train.jsonl'])} / valid {len(splits['valid.jsonl'])} → {OUT_DIR.relative_to(ROOT)}"
    )
    if len(rows) < 100:
        print(
            "  ⚠️ 말투 튜닝에는 보통 수백 개 이상이 필요합니다. 지금은 형식 확인용으로만 쓰세요."
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="이 날짜(YYYY-MM-DD) 이후 기록만")
    ap.add_argument(
        "--finalize", action="store_true", help="검수한 후보를 train/valid 로 확정"
    )
    ap.add_argument("--valid-ratio", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if args.finalize:
        return finalize(args.valid_ratio, args.seed)
    return export(args.since)


if __name__ == "__main__":
    raise SystemExit(main())
