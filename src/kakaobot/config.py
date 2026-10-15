"""설정 로딩. 변경 감지로 핫리로드를 지원한다 (계획서 §6-4)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: Path
    mtime: float

    # --- 편의 접근자 ---------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.raw.get(section, {}).get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self.get("mode", "enabled", False))

    @property
    def dry_run(self) -> bool:
        return bool(self.get("mode", "dry_run", True))

    @property
    def mode(self) -> str:
        return str(self.get("mode", "current", "api"))

    @property
    def room_name(self) -> str:
        name = self.get("room", "name")
        if not name:
            raise ConfigError("[room] name 이 비어 있습니다")
        return str(name)

    @property
    def openkakao_path(self) -> Path:
        raw = self.get("cli", "openkakao_path")
        if not raw:
            raise ConfigError("[cli] openkakao_path 가 비어 있습니다")
        p = Path(raw)
        if not p.is_absolute():
            p = self.path.parent.parent / p
        return p


def load(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"설정 파일이 없습니다: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(raw=raw, path=p, mtime=p.stat().st_mtime)


MODES = ("claude-cli", "local", "api")


def validate(cfg: Config) -> None:
    """돌기 전에 확인할 값. 틀리면 ConfigError — 서비스는 고칠 때까지 답하지 않는다."""
    _ = cfg.room_name, cfg.openkakao_path
    if cfg.mode not in MODES:
        raise ConfigError(f"[mode] current 는 {' | '.join(MODES)} 중 하나여야 합니다: {cfg.mode!r}")
    used = {cfg.mode}
    if cfg.get("fallback", "enabled", False):
        to = cfg.get("fallback", "to", "")
        if to and to not in MODES:
            raise ConfigError(f"[fallback] to 는 {' | '.join(MODES)} 중 하나여야 합니다: {to!r}")
        if to:
            used.add(to)
    if "local" in used:
        if not cfg.get("local", "endpoint"):
            raise ConfigError("[local] endpoint 가 비어 있습니다 — docs/local-llm.md 의 도구별 주소 참조")
        if not cfg.get("local", "model"):
            raise ConfigError("[local] model 이 비어 있습니다 — 서버에 올린 모델 id 를 적어 주세요")
    mode = cfg.get("send", "reply_mode", "thread")
    if mode not in ("thread", "plain"):
        raise ConfigError(f"[send] reply_mode 는 thread | plain 이어야 합니다: {mode!r}")


def reload_if_changed(cfg: Config) -> tuple[Config, bool]:
    """파일이 바뀌었으면 다시 읽는다. (새 설정, 변경여부)를 돌려준다."""
    try:
        mtime = cfg.path.stat().st_mtime
    except OSError:
        return cfg, False
    if mtime == cfg.mtime:
        return cfg, False
    return load(cfg.path), True


def api_key() -> str:
    """API 키는 설정 파일이 아니라 환경변수에서만 읽는다 (계획서 §6-4)."""
    from .secrets import get_secret

    key = get_secret("ANTHROPIC_API_KEY")
    if not key:
        raise ConfigError(
            "ANTHROPIC_API_KEY 가 환경변수에도 키체인에도 없습니다 "
            "(scripts/setup-secrets.sh). 설정 파일에 키를 쓰지 마세요."
        )
    return key
