"""RAG 신뢰도 임계값(D3) 스윕. docs/rag-tuning.md 참조.

실제 자료로 교체한 뒤 CASES 를 실제 질문으로 바꾸고 실행한다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.disable(logging.WARNING)

from kakaobot.rag import KnowledgeBase  # noqa: E402

# (질문, 근거가 있어야 하는가)
CASES: list[tuple[str, bool]] = [
    ("수강료가 얼마인가요?", True),
    ("환불 언제까지 되나요?", True),
    ("녹화본 볼 수 있어요?", True),
    ("수업 몇시에 해요?", True),
    ("노트북 꼭 있어야 하나요?", True),
    ("코딩 몰라도 되나요?", True),
    ("할인 되나요?", True),
    ("분납 가능한가요?", True),
    ("결석하면 보강해주나요?", True),
    ("주차장 있나요?", False),
    ("강사님 나이가 어떻게 되세요?", False),
    ("오늘 점심 뭐 먹을까요?", False),
    ("비트코인 전망 어때요?", False),
    ("내일 날씨 어때요?", False),
    ("숙소 추천해주세요", False),
]

GRID = [0.35, 0.40, 0.45, 0.50, 0.525, 0.55, 0.575, 0.60, 0.625, 0.65, 0.70]


def main() -> int:
    kb = KnowledgeBase("data/knowledge", threshold=0.0, top_k=3)
    n = kb.build()
    print(f"색인 {n}개 조각 / 평가 질문 {len(CASES)}개\n")

    scored = [(q, ok, kb.ground(q).best_score) for q, ok in CASES]

    print(f"{'질문':<30s} {'점수':>7s}  근거")
    print("-" * 50)
    for q, ok, s in sorted(scored, key=lambda x: -x[2]):
        print(f"{q:<30s} {s:>7.3f}  {'있음' if ok else '없음'}")

    pos = sorted(s for _, ok, s in scored if ok)
    neg = sorted(s for _, ok, s in scored if not ok)
    print()
    if pos and neg:
        print(f"근거 있음: {pos[0]:.3f} ~ {pos[-1]:.3f}")
        print(f"근거 없음: {neg[0]:.3f} ~ {neg[-1]:.3f}")
        gap = pos[0] - neg[-1]
        print(f"분리 구간: {neg[-1]:.3f} ~ {pos[0]:.3f} (폭 {gap:+.3f})")
        if gap <= 0:
            print("⚠️  구간이 겹친다. 완전한 분리가 불가능하니 오탐 0 을 우선하라.")

    print()
    print(f"{'임계값':>7s} {'정답률':>7s} {'미탐':>6s} {'오탐':>6s}")
    print("-" * 30)
    best: list[float] = []
    for th in GRID:
        fn = sum(1 for _, ok, s in scored if ok and s < th)
        fp = sum(1 for _, ok, s in scored if not ok and s >= th)
        acc = (len(scored) - fn - fp) / len(scored)
        print(f"{th:>7.3f} {acc:>7.1%} {fn:>6d} {fp:>6d}")
        if fn == 0 and fp == 0:
            best.append(th)

    if best:
        # 안전 구간의 위쪽 3분의 1 — 침묵이 오답보다 낫다 (계획서 §3)
        pick = best[0] + (best[-1] - best[0]) * 2 / 3
        print(f"\n권장: {pick:.2f}  (안전 구간 {best[0]:.3f}~{best[-1]:.3f} 의 위쪽 1/3)")
    else:
        print("\n⚠️  미탐·오탐 모두 0 인 임계값이 없다. 지식베이스를 보강하라.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
