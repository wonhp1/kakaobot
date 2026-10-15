"""보존 기간 — 기간이 지난 기록은 파일째 지워진다."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.logging_setup import KIND_AUDIT, KIND_FILTERED, AuditLog, purge_old

TODAY = date(2026, 9, 18)


def make_days(d: Path, days_ago: list[int]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for n in days_ago:
        (d / f"{(TODAY - timedelta(days=n)).isoformat()}.jsonl").write_text("{}\n")


def remaining(d: Path) -> list[int]:
    """남은 날짜 파일이 오늘로부터 며칠 전인지. 날짜가 아닌 파일은 세지 않는다."""
    out = []
    for f in d.glob("*.jsonl"):
        try:
            out.append((TODAY - date.fromisoformat(f.stem)).days)
        except ValueError:
            continue
    return sorted(out)


def test_keeps_exactly_keep_days(tmp_path):
    """keep_days=30 → 오늘~29일 전은 남고 30일 전부터 지운다."""
    make_days(tmp_path, [0, 1, 29, 30, 31, 100])
    purge_old(tmp_path, keep_days=30, today=TODAY)
    assert remaining(tmp_path) == [0, 1, 29]


def test_filtered_uses_shorter_retention(tmp_path):
    make_days(tmp_path, [0, 6, 7, 8])
    purge_old(tmp_path, keep_days=7, today=TODAY)
    assert remaining(tmp_path) == [0, 6]


def test_today_is_never_deleted(tmp_path):
    make_days(tmp_path, [0])
    purge_old(tmp_path, keep_days=1, today=TODAY)
    assert remaining(tmp_path) == [0]


def test_non_date_files_untouched(tmp_path):
    """날짜 형식이 아닌 파일은 건드리지 않는다 — 잘못 지우는 것보다 남기는 게 낫다."""
    make_days(tmp_path, [100])
    (tmp_path / "notes.jsonl").write_text("keep me")
    purge_old(tmp_path, keep_days=30, today=TODAY)
    assert (tmp_path / "notes.jsonl").exists()
    assert remaining(tmp_path) == []


def test_zero_or_negative_keep_disables_purge(tmp_path):
    make_days(tmp_path, [100])
    assert purge_old(tmp_path, keep_days=0, today=TODAY) == []
    assert remaining(tmp_path) == [100]


def test_missing_directory_is_fine(tmp_path):
    assert purge_old(tmp_path / "없음", keep_days=30, today=TODAY) == []


def test_audit_and_filtered_written_to_separate_dirs(tmp_path):
    """미처리 메시지는 보존 기간이 달라 별도 디렉터리에 쓴다."""
    AuditLog(tmp_path, KIND_AUDIT).write("answer", question="q")
    AuditLog(tmp_path, KIND_FILTERED).write("filtered", text="잡담")
    assert list((tmp_path / KIND_AUDIT).glob("*.jsonl"))
    assert list((tmp_path / KIND_FILTERED).glob("*.jsonl"))
    audit_text = next((tmp_path / KIND_AUDIT).glob("*.jsonl")).read_text(encoding="utf-8")
    assert "잡담" not in audit_text
