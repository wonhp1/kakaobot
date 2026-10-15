"""서비스 ↔ 메뉴바 사이의 약속.

둘은 별도 프로세스이고 파일로만 이야기한다.

    config/config.toml      메뉴바가 쓰고, 서비스가 폴링마다 다시 읽는다 (핫리로드)
    data/run/status.json    서비스가 폴링마다 쓰고, 메뉴바가 읽는다 (하트비트)
    data/run/service.lock   서비스가 떠 있는 동안 잡는 잠금 — 두 개가 동시에 돌면
                            같은 질문에 두 번 답한다

메뉴바가 죽어도 서비스는 계속 돈다. 서비스를 멈추는 길은 셋이다:
메뉴바의 긴급 정지(enabled=false), 서비스 중지(SIGTERM), 서킷 브레이커.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import tomllib
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

RUN_DIR = "data/run"


class AlreadyRunning(Exception):
    pass


def run_dir(root: str | Path) -> Path:
    d = Path(root) / RUN_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def status_path(root: str | Path) -> Path:
    return run_dir(root) / "status.json"


def lock_path(root: str | Path) -> Path:
    return run_dir(root) / "service.lock"


# --- 파일 쓰기 --------------------------------------------------------------


def atomic_write_text(path: str | Path, text: str) -> None:
    """임시 파일에 쓴 뒤 바꿔 끼운다. 읽는 쪽이 반쯤 쓴 파일을 보지 않는다."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- 잠금 -------------------------------------------------------------------


class ServiceLock:
    """중복 실행 방지. 프로세스가 죽으면 OS 가 잠금을 풀어 주므로 낡은 잠금이 남지 않는다."""

    def __init__(self, root: str | Path) -> None:
        self._path = lock_path(root)
        self._fd: int | None = None

    def acquire(self, retries: int = 5, wait: float = 0.3) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        for attempt in range(retries):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # 메뉴바가 상태를 확인하느라 잠깐 잡았을 수 있다
                if attempt == retries - 1:
                    os.close(fd)
                    pid = _read_pid(self._path)
                    raise AlreadyRunning(
                        f"봇이 이미 실행 중입니다 (pid {pid or '?'})"
                    ) from None
                time.sleep(wait)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
        except OSError:
            pass
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None


def running_pid(root: str | Path) -> int | None:
    """서비스가 떠 있으면 pid, 아니면 None. 잠금으로 판단하므로 pid 재사용에 속지 않는다."""
    path = lock_path(root)
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_pid(path) or -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


# --- 상태 -------------------------------------------------------------------


def write_status(root: str | Path, data: dict[str, Any]) -> None:
    atomic_write_text(status_path(root), json.dumps(data, ensure_ascii=False, indent=1))


def read_status(root: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(status_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- 설정 쓰기 ---------------------------------------------------------------


def set_config_values(
    config_path: str | Path, updates: dict[tuple[str, str], Any]
) -> None:
    """설정 파일의 값을 바꾼다. 주석과 순서는 그대로 둔다.

    다 쓴 결과를 한 번 파싱해 보고 나서 바꿔 끼운다 — 서비스가 깨진 설정을 읽지 않게.
    """
    import tomlkit

    path = Path(config_path)
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    for (section, key), value in updates.items():
        if section not in doc:
            doc[section] = tomlkit.table()
        doc[section][key] = value
    text = tomlkit.dumps(doc)
    tomllib.loads(text)
    atomic_write_text(path, text)


def read_config(config_path: str | Path) -> dict[str, Any]:
    with Path(config_path).open("rb") as fh:
        return tomllib.load(fh)


# --- 오늘 집계 ---------------------------------------------------------------


def today_counts(log_dir: str | Path, day: date | None = None) -> Counter:
    """오늘 audit 이벤트별 건수 + 답하지 않은 메시지 수(filtered)."""
    day = day or date.today()
    counts: Counter = Counter()
    for kind in ("audit", "filtered"):
        p = Path(log_dir) / kind / f"{day.isoformat()}.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line).get("event", "?")
                except json.JSONDecodeError:
                    continue
                counts["filtered" if kind == "filtered" else event] += 1
    return counts
