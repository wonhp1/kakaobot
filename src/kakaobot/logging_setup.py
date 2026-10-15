"""로깅 설정과 보존 기간 관리.

기록 종류별로 보존 기간이 다르다 (2026-09-18 결정):

    종류        내용                              보존
    audit      봇이 처리한 질문·답변·근거·점수       30일   D3 임계값 튜닝, 오답 추적
    filtered   봇이 답하지 않은 메시지 원문          7일    D2 트리거 튜닝 (놓친 질문 찾기)
    app log    사람이 읽는 운영 로그                30일

날짜별 파일로 나눠 쓴다 (`logs/audit/2026-09-18.jsonl`). 보존 기간이 지나면 **파일째 지운다**
— 큰 파일을 다시 쓰며 줄을 골라내지 않아도 된다.

미처리 메시지를 무기한 쌓으면 봇이 오픈채팅방 전체 대화 기록기가 된다. 익명 다수가
들어오는 방이므로 짧게 둔다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("kakaobot.logging")

KIND_AUDIT = "audit"
KIND_FILTERED = "filtered"


def setup(log_dir: str | Path, level: str = "INFO", keep_days: int = 30,
          console: bool = True) -> logging.Logger:
    d = Path(log_dir)
    d.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("kakaobot")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    # 크기가 아니라 날짜로 돌린다. 크기 기준이면 조용한 방에선 수개월치가 남는다.
    daily = logging.handlers.TimedRotatingFileHandler(
        d / "kakaobot.log",
        when="midnight",
        backupCount=keep_days,
        encoding="utf-8",
    )
    daily.setFormatter(fmt)
    root.addHandler(daily)

    return root


class AuditLog:
    """구조화 기록. 종류별 디렉터리에 날짜별 JSONL 로 쓴다."""

    def __init__(self, log_dir: str | Path, kind: str = KIND_AUDIT) -> None:
        self._dir = Path(log_dir) / kind
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, day: date | None = None) -> Path:
        return self._dir / f"{(day or date.today()).isoformat()}.jsonl"

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with self.path_for().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def purge_old(directory: str | Path, keep_days: int, today: date | None = None) -> list[Path]:
    """보존 기간이 지난 날짜 파일을 지운다. 지운 파일 목록을 돌려준다.

    오늘 포함 `keep_days` 일치를 남긴다. keep_days=30 이면 오늘~29일 전까지 남고
    30일 전 파일부터 지운다. 날짜 형식이 아닌 파일은 건드리지 않는다.
    """
    d = Path(directory)
    if keep_days <= 0 or not d.exists():
        return []
    today = today or date.today()
    removed = []
    for f in d.glob("*.jsonl"):
        try:
            day = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if (today - day).days >= keep_days:
            f.unlink()
            removed.append(f)
    if removed:
        log.info("보존 기간 경과 기록 삭제: %s %d개", d.name, len(removed))
    return removed
