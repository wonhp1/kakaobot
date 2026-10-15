"""서비스 루프 (계획서 §6-1).

수신 → 필터 → 큐 → RAG → LLM → 출력검증 → 전송 → 로깅 전 구간을 잇는다.

Phase 0 §5-3에 따라 ax-watch 이벤트는 "새 메시지 있음" 신호로만 쓰고
본문은 반드시 ax-read 로 다시 읽는다.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import time
import tomllib
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from . import control
from .kakao import (
    KakaoClient,
    canary_check,
    KakaoError,
    Message,
    RoomMinimizedError,
    RoomNotOpenError,
    SendResult,
    last_seen_index,
)
from .answering import Answerer
from .guard import OutputGuard
from .llm import build_adapter, build_system_prompt
from .logging_setup import KIND_AUDIT, KIND_FILTERED, AuditLog, purge_old
from .memory import ConversationMemory
from .notify import build_notifier, format_canary_failure, format_escalation
from .rag import KnowledgeBase
from .pipeline import CircuitBreaker, RateLimiter, TriggerJudge, Verdict
from .photo import MEDIA, nearby_photo, read_and_discard, save_photo
from .sentlog import SentLog
from .followup import (
    Followup,
    Notice,
    NoticeTracker,
    matches,
    parent_candidates,
    pick_followup,
    read_notices,
)
from .thread import ThreadError, read_thread

log = logging.getLogger("kakaobot.service")

# 직전 화면의 마지막 메시지가 이만큼 연속으로 안 보이면 기준선을 다시 잡는다 (3초 × 10 = 30초).
# 한꺼번에 20개 넘게 올라와 따라잡을 수 없을 때를 위한 탈출구다.
MISALIGNED_LIMIT = 10
# 위 한도는 3초 폴링 기준(30초)이다. 폴링 간격이 길면 같은 시간(최소 2번)으로 줄인다
MISALIGNED_SECS = 30

# 줄 앞의 번호·글머리 ("9-1)", "1.", "-", "·") — 한 줄씩 따로 찾을 때 뗀다
_LIST_MARK = re.compile(r"^\s*(?:\d+(?:-\d+)*[.)]|[-•·*])\s*")

# 폴링 간격이 이 이상이면(2026-09-26 운영자 결정: 1분) 매번 넓게 읽는다 — 1분 사이에
# 20개 넘게 올라와도 놓치지 않게. ax-read 는 창에 올라온 것을 다 읽는 것이라 비용은 거의 같다
LONG_POLL_SECS = 10
# 오래 쉬는 동안에도 이 간격으로 상태를 적는다 — 메뉴바는 45초 넘게 안 바뀌면 '응답 없음'
HEARTBEAT_SECS = 10

# 스레드 추가 질문을 '방에도 함께' 올리면 같은 글이 방과 스레드 양쪽에 보인다.
# 한쪽에서 답한 (질문자, 글)은 이 시간 동안 다른 쪽에서 답하지 않는다.
DEDUPE_SECS = 600
# 스레드 하나에 봇이 답하는 횟수를 세는 창 (max_answers_per_thread 번까지)
THREAD_WINDOW_SECS = 1800
# 추가 질문에 답한 뒤 이 시간 안에 그 스레드 알림이 오면(최신 댓글 = 봇의 답) 한 번 더 연다 —
# 봇이 답을 만드는 사이에 달린 댓글은 봇의 답 **앞**에 끼어 알림만으로는 안 보인다
RECHECK_SECS = 180
# 봇이 단 답을 기억하는 기간. 감사 기록 보존 기간(30일)과 맞춘다
BOT_REPLY_DAYS = 30

# 카카오톡 창이 다른 데스크탑에 있는 동안은 멈추지 않고 기다린다 (2026-09-24 결정).
# 돌아오면 그사이 올라온 질문에 **늦게라도 답한다** — 첫 읽기는 평소(20개)보다 넓게 본다.
CATCH_UP_COUNT = 60  # ax-read 로 창에 올라와 있는 메시지를 거의 다 (실측 약 48개)
# '댓글 알림'도 기준선을 잡을 때와 돌아왔을 때는 더 아래까지 본다. 평소엔 화면에 보이는
# 5줄만 (0.01초) — 그 아래는 카카오톡이 느리게 답한다 (10줄 0.2초, 20줄 1~2초, 실측)
CATCH_UP_ROWS = 20
# 이만큼 오래 창이 안 보이면 운영자에게 한 번 알린다
AWAY_NOTIFY_SECS = 600


def _thread_comment_flags(
    messages: list[Message], rows: list[tuple[str, bool]]
) -> list[bool]:
    """ax-read 로 읽은 메시지마다, 방 창의 같은 줄이 '함께 보낸 스레드 댓글'인지.

    둘 다 최근 것이 끝이고 글 줄끼리 1:1 이다 (사진·파일 자리표시는 창에서 글 줄이 아니라
    건너뛴다). 읽는 사이 새 글이 붙었을 수 있어 창 쪽 끝 몇 줄은 건너뛰고 맞춘다. 본문이
    어긋나면 거기서 멈추고 나머지는 False — 잘못 맞춰 방 질문을 빼느니 방에서 답한다.
    """

    def norm(t: str) -> str:
        return " ".join(t.split())

    flags = [False] * len(messages)
    idx = [i for i, m in enumerate(messages) if m.text not in MEDIA]
    if not idx or not rows:
        return flags
    last = norm(messages[idx[-1]].text)
    j = next(
        (
            k
            for k in range(len(rows) - 1, max(-1, len(rows) - 5), -1)
            if norm(rows[k][0]) == last
        ),
        None,
    )
    if j is None:
        return flags
    for i in reversed(idx):
        if j < 0 or norm(rows[j][0]) != norm(messages[i].text):
            break
        flags[i] = rows[j][1]
        j -= 1
    return flags


@dataclass
class Stats:
    seen: int = 0
    triggered: int = 0
    answered: int = 0
    escalated: int = 0
    errors: int = 0
    cancelled: int = 0
    silent: int = 0
    last_error: str = ""
    last_error_at: str = ""


class Service:
    def __init__(self, config_path: str | Path) -> None:
        self._cfg = config_mod.load(config_path)
        config_mod.validate(self._cfg)
        self._root = Path(config_path).resolve().parent.parent
        self._stats = Stats()
        self._stop = False
        self._started_at = datetime.now().isoformat(timespec="seconds")
        self._init_state()
        self._apply_config()

    def _init_state(self) -> None:
        """실행 중에 쌓이는 상태. **한 번만** 만든다.

        설정이 바뀌었다고 이걸 다시 만들면 안 된다. 예전에는 설정 변경 때마다 통째로
        다시 만들었는데, 그러면 긴급 정지(enabled=false→true)를 풀자마자
          - 직전 목록(_last_seen)이 비어 화면의 옛 메시지 20개를 전부 새것으로 보고
          - 레이트 리밋 이력이 사라져 옛 질문 여러 개에 한꺼번에 답했다.
        """
        # 직전 폴링의 화면. None 이면 기준선이 없다 — 다음 폴링은 기준선만 잡고 답하지 않는다.
        self._last_seen: list[Message] | None = None
        # 직전 화면의 마지막 메시지가 안 보인 연속 폴링 수. 너무 길어지면 기준선을 다시 잡는다
        self._misaligned = 0
        self._scroll_skip_logged = False
        self._rate = RateLimiter()
        # 서킷 브레이커를 둘로 나눈다. 하나로 두면 폴링(읽기)이 성공할 때마다 연속
        # 오류 횟수가 0 으로 돌아가, LLM 이 계속 실패해도 영영 작동하지 않았다.
        self._breaker = CircuitBreaker()  # 읽기(ax-read) 연속 실패
        self._answer_breaker = CircuitBreaker()  # 답변·전송 연속 실패
        self._memory = ConversationMemory()
        self._sent = SentLog()  # 봇이 보낸 문구 — 자기 메시지 판별 2차 관문
        # 스레드 추가 질문 (2026-09-24, kakaobot.followup)
        # 봇이 단 답 전부. 스레드의 마지막 운영자 댓글이 봇의 것인지 가린다 — 재시작해도
        # 알아야 하므로 기동할 때 감사 기록에서 채운다 (_load_bot_replies)
        self._bot_replies = SentLog(capacity=5000, max_age_secs=BOT_REPLY_DAYS * 86400)
        self._notices = NoticeTracker()
        self._followup_tick = 0
        self._notice_missing_logged = False
        self._followup_error = ""
        self._thread_answers: dict[str, deque[float]] = {}
        # (질문자, 글) → (답한 쪽 'room'|'thread', 시각)
        self._answered: dict[tuple[str, str], tuple[str, float]] = {}
        # 카카오톡 창이 다른 데스크탑에 있는 동안 (그 시작 시각). None 이면 보인다
        self._away_since: float | None = None
        self._away_notified = False
        self._catch_up = False  # 돌아온 뒤 첫 방 읽기는 넓게
        self._followup_catch_up = False  # 돌아온 뒤 첫 알림 읽기도 넓게
        self._kb: KnowledgeBase | None = None  # 임베딩 모델(1GB)을 품고 있어 재사용한다
        self._last_purge_day = None
        self._last_canary = 0.0
        self._canary_failed = False
        self._state = "starting"
        self._config_error = ""
        self._bad_config_mtime: float | None = None

    def _apply_config(self) -> None:
        """설정에서 오는 값만 반영한다. 쌓인 상태는 건드리지 않는다."""
        cfg = self._cfg
        log_dir = self._root / cfg.get("logging", "dir", "logs")
        self._audit = AuditLog(log_dir, KIND_AUDIT)
        self._filtered_log = AuditLog(log_dir, KIND_FILTERED)
        self._keep_audit = int(cfg.get("logging", "retention_days_audit", 30))
        self._keep_filtered = int(cfg.get("logging", "retention_days_filtered", 7))
        self._kakao = KakaoClient(
            cfg.openkakao_path, cfg.room_name, dry_run=cfg.dry_run
        )
        self._judge = TriggerJudge(
            on_question_mark=cfg.get("trigger", "on_question_mark", True),
            on_mention=cfg.get("trigger", "on_mention", True),
            mention_names=cfg.get("trigger", "mention_names", []),
            min_length=cfg.get("trigger", "min_length", 4),
            on_question_ending=cfg.get("trigger", "on_question_ending", True),
        )
        # D2 보강: 물음표도 멘션도 없는 말은 지식베이스와 가까울 때만 답한다
        self._on_similarity = bool(cfg.get("trigger", "on_similarity", True))
        self._weak_threshold = float(cfg.get("trigger", "weak_threshold", 0.70))
        self._rag_threshold = float(cfg.get("rag", "confidence_threshold", 0.58))
        # 한도는 제자리에서 바꾼다 — 이력은 유지
        self._rate.user_cooldown_secs = cfg.get("rate_limit", "user_cooldown_secs", 60)
        self._rate.room_per_minute = cfg.get("rate_limit", "room_per_minute", 5)
        max_errors = cfg.get("circuit_breaker", "max_consecutive_errors", 3)
        self._breaker.max_consecutive_errors = max_errors
        self._answer_breaker.max_consecutive_errors = max_errors
        self._memory.configure(
            turns=cfg.get("context", "window_turns", 5),
            max_age_secs=float(cfg.get("context", "max_age_minutes", 30)) * 60,
        )
        self._notifier = build_notifier(cfg)
        self._send_delay = (
            float(cfg.get("send", "delay_min_secs", 1.0)),
            float(cfg.get("send", "delay_max_secs", 3.0)),
        )
        # D10: 질문 메시지에 댓글로 답하고 방에도 함께 올린다
        self._reply_mode = str(cfg.get("send", "reply_mode", "thread"))
        self._mirror_to_room = bool(cfg.get("send", "mirror_to_room", True))
        self._canary_every = int(cfg.get("canary", "interval_secs", 3600))
        # 폴링 간격. 명령줄 --interval 이 있으면 그것이 먼저다 (_run)
        self._poll_interval = max(1, int(cfg.get("poll", "interval_secs", 3)))
        self._interval = self._poll_interval
        # 질문과 함께 올라온 사진의 글자를 읽는다 (2026-09-21 결정, docs/screenshots.md).
        # 설정에 없으면 끈다 — 우클릭 메뉴를 여는 동작이라 명시적으로 켜야 한다.
        self._photo_enabled = bool(cfg.get("photo", "enabled", False))
        self._photo_dir = Path(
            str(cfg.get("photo", "save_dir", "~/Downloads"))
        ).expanduser()
        # 봇이 단 댓글 아래의 추가 질문에 같은 스레드로 답한다 (2026-09-24).
        # 카카오톡 '댓글 알림' 방을 창으로 열어 두어야 한다. 설정에 없으면 끈다.
        was_enabled = getattr(self, "_followup_enabled", None)
        self._followup_enabled = bool(cfg.get("followup", "enabled", False))
        if self._followup_enabled and was_enabled is False:
            # 꺼져 있던 동안 달린 추가 질문에 늦게 답하지 않게 — 다음 화면을 기준선으로
            self._notices.reset()
        self._followup_top = max(1, int(cfg.get("followup", "top", 5)))
        self._followup_every = max(1, int(cfg.get("followup", "every_polls", 2)))
        self._followup_cap = max(
            1, int(cfg.get("followup", "max_answers_per_thread", 3))
        )
        # 임계값·모델이 바뀌었을 수 있으니 답변 계층은 다음 질문 때 새로 만든다
        self._answerer: Answerer | None = None

    def _ensure_answerer(self) -> Answerer:
        """임베딩 모델 로딩이 무거우므로 첫 트리거 때까지 미룬다."""
        if self._answerer is not None:
            return self._answerer
        cfg = self._cfg
        kb = self._ensure_kb()
        self._answerer = Answerer(
            kb=kb,
            llm=build_adapter(cfg),
            guard=OutputGuard(
                max_chars=cfg.get("output", "max_chars", 500),
                block_links=cfg.get("output", "block_links", True),
            ),
            escalation_message=cfg.get(
                "output", "escalation_message", "확인 후 답변드릴게요."
            ),
            system_prompt=build_system_prompt(cfg.get("style", "guide", "")),
            partial_message=cfg.get(
                "output", "partial_message", "나머지는 확인 후 답변드릴게요."
            ),
        )
        return self._answerer

    def _ensure_kb(self) -> KnowledgeBase:
        """지식베이스는 설정이 바뀌어도 다시 만들지 않는다.

        임베딩 모델 로딩에 12초·1GB 가 든다. 메뉴바에서 긴급 정지를 누를 때마다
        (= 설정 파일이 바뀔 때마다) 다시 읽으면 안 된다. 모델이나 폴더가 바뀔 때만
        새로 만들고, 문서가 바뀌면 이미 올라온 모델로 다시 색인만 한다.
        """
        cfg = self._cfg
        directory = self._root / cfg.get("rag", "knowledge_dir", "data/knowledge")
        model = cfg.get("rag", "embed_model", "BAAI/bge-m3")
        threshold = cfg.get("rag", "confidence_threshold", 0.35)
        top_k = cfg.get("rag", "top_k", 4)

        if (
            self._kb is None
            or self._kb.directory != directory
            or self._kb.model_name != model
        ):
            kb = KnowledgeBase(
                directory, threshold=threshold, top_k=top_k, model_name=model
            )
            n = kb.build()
            log.info("지식베이스 색인 완료: %d개 조각", n)
            self._kb = kb
            return kb

        self._kb.configure(threshold=threshold, top_k=top_k)
        try:
            n = self._kb.refresh_if_changed()
            if n is not None:
                log.info("지식베이스 문서가 바뀌어 다시 색인했습니다: %d개 조각", n)
        except (OSError, ValueError) as exc:
            # 문서를 고치다 잠깐 비었거나 깨진 경우. 직전 색인으로 계속 답한다.
            log.error("지식베이스를 다시 읽지 못해 직전 색인을 씁니다: %s", exc)
        return self._kb

    # --- 수명주기 -------------------------------------------------------
    def request_stop(self, *_: object) -> None:
        self._stop = True

    def _sleep(self, secs: float) -> None:
        """폴링 사이를 쉰다. 1초씩 끊어 자서 멈춤 신호에 곧바로 답하고(1분을 다 채우지
        않는다), 오래 쉴 때는 HEARTBEAT_SECS 마다 상태를 적어 메뉴바가 '응답 없음'으로
        보지 않게 한다. 긴급 정지는 기다릴 필요가 없다 — 쉬는 동안에는 아무것도 보내지
        않고, 깨면 설정부터 다시 읽는다.
        """
        end = time.monotonic() + secs
        last_beat = time.monotonic()
        while True:
            time.sleep(max(0.0, min(1.0, end - time.monotonic())))
            now = time.monotonic()
            if self._stop or now >= end:
                return
            if now - last_beat >= HEARTBEAT_SECS:
                self._write_status()
                last_beat = now

    @property
    def _paused_reason(self) -> str:
        """지금 답하면 안 되는 이유. 없으면 빈 문자열."""
        if self._config_error:
            return self._config_error
        if not self._cfg.enabled:
            return "긴급 정지 중 (enabled=false)"
        return ""

    def _reload_config(self) -> None:
        try:
            mtime = self._cfg.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime == self._bad_config_mtime:
            return  # 이미 알린 깨진 파일 — 고쳐질 때까지 기다린다
        old_cfg = self._cfg
        try:
            cfg, changed = config_mod.reload_if_changed(self._cfg)
            if not changed:
                return
            config_mod.validate(cfg)
            self._cfg = cfg
            self._apply_config()
        except (
            tomllib.TOMLDecodeError,
            config_mod.ConfigError,
            KakaoError,
            OSError,
        ) as exc:
            # 깨진 설정으로는 돌지 않는다 (fail-closed). 운영자가 enabled=false 를
            # 쓰다가 오타를 냈을 수도 있다 — 그럴 때 계속 답하면 안 된다.
            self._cfg = old_cfg
            self._apply_config()
            self._bad_config_mtime = mtime
            self._config_error = f"설정 파일 오류 — 고칠 때까지 답하지 않습니다: {exc}"
            log.error(self._config_error)
            self._notifier.send(f"⚠️ {self._config_error}")
            return
        if self._config_error:
            log.info("설정 파일 오류가 풀렸습니다.")
        self._config_error = ""
        self._bad_config_mtime = None
        old_room = old_cfg.room_name
        log.info("설정 파일이 바뀌어 다시 읽었습니다.")
        if cfg.room_name != old_room:
            # 방이 바뀌면 새 방의 현재 화면을 기준선으로 잡는다. 비워 두면
            # 새 방의 옛 메시지를 전부 새것으로 보고 답한다.
            try:
                self._last_seen = self._kakao.read(count=20)
            except KakaoError as exc:
                # 빈 목록([])으로 두면 다음 폴링이 화면 전체를 새 메시지로 본다
                log.error("새 방 %r 기준선을 잡지 못했습니다: %s", cfg.room_name, exc)
                self._last_seen = None
            self._memory.clear()
            self._notices.reset()
            log.info("대상 방 변경: %r → %r", old_room, cfg.room_name)

    # --- 처리 -----------------------------------------------------------
    def _handle(
        self,
        m: Message,
        photo_hint: bool = False,
        followup: Followup | None = None,
        parts: tuple[Message, ...] | None = None,
    ) -> bool:
        """메시지 하나(또는 한 사람이 이어 보낸 묶음)를 판정하고 답한다.
        답을 올렸으면(올렸을 수 있으면) True.

        photo_hint: 같은 사람이 이어 보낸 메시지 중에 사진이 있다 (_poll_once 가 본다).
        followup: 스레드 안의 추가 질문이다 (_followup 이 준다). 그러면
          - 맥락은 방의 대화 기억 대신 **그 스레드의 대화**를 쓰고
          - 답은 방의 질문이 아니라 **그 스레드**에 단다 (방에도 함께 — send.mirror_to_room).
        parts: 한 번 확인할 때 같은 사람이 이어 보낸 메시지들 (2026-09-26 운영자 결정).
          **하나의 질문**으로 보고 한 번에 답한다 — 둘째 질문을 버리지 않는다. 답은 m 에 단다
          (그중 마지막 질문 메시지, _poll_once 가 고른다).
        """
        self._stats.seen += 1
        origin = "thread" if followup else "room"
        parts = tuple(parts) if parts else (m,)

        # 자기 메시지 2차 관문. 말풍선 위치(outgoing)로 못 알아본 봇 메시지를 거른다.
        # 예) 접두어를 뗀 뒤로는 문구만으로 봇 메시지를 알아볼 수 없다.
        kept = []
        for p in parts:
            if not p.is_own and self._sent.contains(p.text):
                self._filtered_log.write(
                    "filtered",
                    stage="self_filter",
                    reason="봇이 보낸 문구와 같음(SentLog)",
                    sender=p.sender,
                    text=p.text,
                )
            else:
                kept.append(p)
        if not kept:
            return False

        # 사진·파일 자리표시는 질문이 아니다. 사진은 **질문과 함께 올 때만** 읽는다
        # (2026-09-21 결정) — 그 판단은 질문 쪽에서 photo_hint 로 한다.
        if len(parts) == 1 and m.text in MEDIA:
            self._filtered_log.write(
                "filtered",
                stage="media",
                reason="사진·파일만 올라옴",
                sender=m.sender,
                text=m.text,
            )
            return False

        # 스레드 추가 질문을 '방에도 함께' 올리면 방에도 같은 글이 뜬다. 스레드에서 이미 답했다
        if followup is None:
            for p in [
                p for p in kept if self._answered_elsewhere(p.sender, p.text, origin)
            ]:
                self._filtered_log.write(
                    "filtered",
                    stage="duplicate",
                    reason="스레드에서 이미 답한 추가 질문 (방에 함께 올라옴)",
                    sender=p.sender,
                    text=p.text,
                )
                kept.remove(p)
            if not kept:
                return False
        parts = tuple(kept)
        if m not in parts:
            m = parts[-1]
        # 묶음이면 한 질문으로 합친다. 판정·기록·모델에 이 글을 쓰고, 답은 m 에 단다
        q = m if len(parts) == 1 else replace(m, text="\n".join(p.text for p in parts))

        # 맥락은 트리거 여부와 무관하게 쌓는다. "저 질문이 있는데요" 처럼 물음표 없는
        # 앞 발화가 뒤 질문의 맥락이다. 이력은 이번 발화를 넣기 **전에** 뽑는다.
        # 스레드 추가 질문은 그 스레드의 대화(원글 → 봇의 답 → …)가 맥락이다.
        history = followup.history if followup else ""
        if m.sender and not m.is_own and not m.is_system_notice:
            if followup is None:
                history = self._memory.history(m.sender)
            for p in parts:
                self._memory.add_user(m.sender, p.text)

        screen, photo_note = "", ""
        use_photo = photo_hint and self._photo_enabled
        verdict = self._judge_parts(parts)
        if not verdict.passed and verdict.stage == "no_trigger":
            verdict = self._guess_question(q, parts=parts)
            if (
                not verdict.passed
                and verdict.stage == "weak_no_grounding"
                and use_photo
                and any(self._judge.looks_like_question(p.text) for p in parts)
            ):
                # "이거 왜 이래요" — 질문 어미는 있는데 글만으로는 무엇을 묻는지 모른다.
                # 함께 올린 화면의 글자로 한 번 더 본다. 어미 단서가 없는 말("완성했어요")은
                # 사진이 있어도 읽지 않는다 — 질문과 함께 온 사진만 읽기로 했다.
                screen, photo_note = self._read_photo(m)
                if screen:
                    verdict = self._guess_question(q, screen, parts=parts)

        if not verdict.passed:
            log.debug("탈락 [%s] %s — %r", verdict.stage, verdict.reason, q.text[:40])
            self._filtered_log.write(
                "filtered",
                stage=verdict.stage,
                reason=verdict.reason,
                sender=m.sender,
                text=q.text,
                origin=origin,
            )
            return False

        weak = verdict.is_weak
        # 1인 쿨다운은 2026-09-26 부터 끈다 ([rate_limit] user_cooldown_secs = 0 — 이어 보낸
        # 메시지를 묶어 한 번에 답하므로 필요 없다). 스레드 추가 질문은 원래 걸지 않았다.
        rate = self._rate.check_room() if followup else self._rate.check(m.sender)
        if not rate.passed:
            log.info("레이트 리밋 — %s", rate.reason)
            self._audit.write(
                "rate_limited",
                reason=rate.reason,
                sender=m.sender,
                text=q.text,
                origin=origin,
            )
            return False

        # 여기서부터 답한다 — 같은 글이 다른 쪽(방/스레드)에 보여도 다시 답하지 않게 적어 둔다
        self._mark_answered(
            m.sender,
            followup.lines if followup else tuple(p.text for p in parts),
            origin,
        )
        self._stats.triggered += 1
        log.info(
            "[트리거:%s%s] @%s: %s",
            verdict.reason,
            (
                " · 스레드 추가 질문"
                if followup
                else (
                    f" · 이어 보낸 {len(parts)}개를 한 질문으로"
                    if len(parts) > 1
                    else ""
                )
            ),
            m.sender,
            q.text,
        )
        # 모델 로딩·LLM 호출로 수십 초 걸릴 수 있다. 메뉴바가 '멈춤'으로 오해하지 않게 알린다.
        self._state = "answering"
        self._write_status()

        # [5]~[9] 질문 재구성 → RAG → LLM → 출력 검증
        # 부르는 말(@운영자)은 검색어에서 뺀다 — 이름이 검색 점수를 떨어뜨린다
        question = self._judge.strip_mentions(q.text)
        if use_photo and not photo_note:
            screen, photo_note = self._read_photo(m)
        # 여러 가지를 물었으면 한 줄씩 따로도 찾는다 — 묶음이든, 한 메시지에 여러 줄이든.
        # 합친 글만으로는 주제가 섞여 점수가 떨어지고, 한 주제 근거가 자리를 다 차지한다
        # (2026-09-26 실전: 여섯 줄 질문에서 '난이도' 근거가 빠짐)
        pieces = self._question_pieces(
            [
                _LIST_MARK.sub("", line).strip()
                for p in parts
                for line in p.text.splitlines()
            ]
        )
        extra_q = (
            tuple(self._judge.strip_mentions(x) for x in pieces)
            if len(pieces) > 1
            else ()
        )
        outcome = self._ensure_answerer().answer(
            question,
            history,
            screen_text=screen,
            **({"extra_queries": extra_q} if extra_q else {}),
        )

        llm_failed = outcome.action == "skip"
        if llm_failed:
            # LLM 이 답을 못 냈다. 질문자에게는 안내 문구를 달고(D11 '항상 안내 문구')
            # 오류로 센다 — 연속되면 서킷 브레이커가 멈춘다.
            self._record_error(outcome.reason)
            log.error("LLM 오류 — 안내 문구로 대신합니다: %s", outcome.reason)
            self._answer_breaker.record_error()

        # [10] 전송 — 답변이든 안내 문구든 질문에 댓글로 단다 (D10·D11)
        if outcome.action not in ("answer", "partial") and weak:
            # 추측으로 잡은 말이다. 답할 근거가 없으면 안내 문구도 보내지 않는다 —
            # 잡담에 "확인 후 답변드릴게요"가 뿌려지지 않게 (D2 보강).
            self._stats.silent += 1
            log.info(
                "추측 트리거였으나 근거 부족 — 조용히 넘어갑니다: %s", outcome.reason
            )
            self._audit.write(
                "silent",
                sender=m.sender,
                question=q.text,
                trigger=verdict.reason,
                stage=outcome.stage,
                reason=outcome.reason,
                score=round(outcome.score, 4),
                photo=photo_note or None,
                screen_text=screen[:500] or None,
                origin=origin,
                parent=followup.parent if followup else None,
            )
            return False
        if outcome.reply:
            result = self._deliver(m, outcome.reply, followup)
        else:
            result = SendResult(False, "none", "보낼 문구가 없습니다")
        sent = result.ok

        if result.via == "cancelled":
            self._stats.cancelled += 1
            log.warning("전송 취소 — %s", result.detail)
        elif result.via == "failed":
            self._record_error(f"전송 실패: {result.detail}")
            self._answer_breaker.record_error()
        elif result.via == "unverified":
            # 나갔을 수도 있다. 다시 보내지 않고 사람에게 확인을 맡긴다.
            self._record_error(f"전송 확인 실패: {result.detail}")
            self._answer_breaker.record_error()
            self._notifier.send(
                "⚠️ 답변 전송을 확인하지 못했습니다 — 방을 직접 확인해 주세요.\n"
                f"질문: {q.text[:200]}\n답변: {outcome.reply[:200]}\n사유: {result.detail[:300]}"
            )
        elif not llm_failed:
            self._answer_breaker.record_success()

        if outcome.action in ("escalate", "skip", "partial"):
            # 일부 답변도 나머지는 운영자가 확인해야 한다 — 같은 알림을 보낸다
            self._stats.escalated += 1
            log.info("에스컬레이션 — %s", outcome.reason)
            # 운영자가 알아야 §6-3 의 지식베이스 성장 루프가 돈다
            self._notifier.send(
                format_escalation(
                    sender=m.sender,
                    question=(
                        f"[스레드 추가 질문] {q.text}\n(원글: {followup.parent[:80]})"
                        if followup
                        else q.text
                    ),
                    reason=outcome.reason,
                    score=outcome.score,
                )
            )
        if outcome.action in ("answer", "partial"):
            self._stats.answered += 1
            log.info("답변(%s) → %s", result.via, outcome.reply)
            if sent:
                self._memory.add_bot(m.sender, outcome.reply)

        # [11] 로깅 — 질문·근거·답변·모드·지연시간 (§6-1)
        # context·history·system_sha 는 파인튜닝 예제를 추론 때와 같은 형식으로 되살리는 데 쓴다
        self._audit.write(
            "llm_error" if llm_failed else outcome.action,
            origin=origin,  # room: 방의 질문 / thread: 스레드 안의 추가 질문
            parent=followup.parent if followup else None,
            sender=m.sender,
            question=q.text,
            parts=len(parts) if len(parts) > 1 else None,  # 이어 보낸 메시지를 묶은 수
            anchor=m.text if len(parts) > 1 else None,  # 그중 답을 단 메시지
            llm_question=question,  # 멘션을 뗀 뒤 실제로 모델에 준 질문
            search_query=outcome.search_query,
            photo=photo_note or None,  # 사진을 읽었는지, 왜 못 읽었는지
            screen_text=screen[:500] or None,  # 검색에 따로 넣은 화면 글자
            had_context=bool(history),
            history=history,
            context=outcome.context,
            system_sha=outcome.system_sha,
            reply=outcome.reply,
            sources=outcome.sources,
            score=round(outcome.score, 4),
            stage=outcome.stage,
            reason=outcome.reason,
            model=outcome.model,
            mode=outcome.mode,
            latency_ms=outcome.latency_ms,
            sent=sent,
            via=result.via,
            send_detail=result.detail,
            dry_run=self._cfg.dry_run,
            raw_reply=outcome.raw_reply,
        )
        if result.via != "cancelled":
            self._rate.commit(m.sender)
        return result.maybe_posted

    def _judge_parts(self, parts: tuple[Message, ...]) -> Verdict:
        """묶음의 판정. 한 조각이라도 확실한 질문(물음표·멘션)이면 통과한다. 아니면 추측해
        볼 만한지(no_trigger)를 본다. 메시지 하나면 TriggerJudge.judge 와 같다."""
        verdicts = [self._judge.judge(p) for p in parts]
        return (
            next((v for v in verdicts if v.passed), None)
            or next((v for v in verdicts if v.stage == "no_trigger"), None)
            or verdicts[-1]
        )

    def _question_pieces(self, lines: list[str]) -> list[str]:
        """여러 줄을 물음 단위로. 끝나지 않은 짧은 줄("환불은", "수강료가")은 다음 줄에 붙인다.

        줄마다 따로 찾으면 "환불은" / "언제까지 되나요?" 가 각각 엉뚱한 근거를 끌어온다
        (2026-09-26 실전: '환불 신청 방법'이 들어가고 '환불 기한'이 빠짐).
        """
        out: list[str] = []
        carry = ""
        for line in (x for x in lines if x):
            text = f"{carry} {line}".strip() if carry else line
            unfinished = (
                len(text) <= 12
                and not text.endswith(("?", "？"))
                and not self._judge.looks_like_question(text)
            )
            if unfinished:
                carry = text
                continue
            out.append(text)
            carry = ""
        if carry:
            out.append(carry)
        return out

    def _anchor(self, parts: list[Message]) -> Message:
        """묶음에서 답을 달 메시지 — 마지막 질문 같은 메시지, 없으면 마지막 메시지."""
        for p in reversed(parts):
            if self._judge.judge(p).passed or self._judge.looks_like_question(p.text):
                return p
        return parts[-1]

    def _guess_question(
        self, m: Message, screen: str = "", parts: tuple[Message, ...] | None = None
    ) -> Verdict:
        """물음표도 멘션도 없는 말이 질문인지 추측한다 (D2 보강, 2026-09-20).

        두 가지를 본다 — 한국어 의문 어미, 그리고 지식베이스와의 유사도.
        **근거가 충분할 때만** 통과시킨다. 추측이라 조용히 지나가는 쪽이 낫기 때문이다.
        LLM 은 부르지 않는다 (임베딩 1회, 로컬 0.4초).
        묶음(parts)이면 조각 중 하나라도 묻는 말투면 단서로 보고, 조각마다 따로도 찾는다.
        """
        texts = [p.text for p in parts] if parts and len(parts) > 1 else [m.text]
        hint = next((h for h in map(self._judge.looks_like_question, texts) if h), "")
        if not hint and not self._on_similarity:
            return Verdict(False, "no_trigger", "트리거 조건 불일치")
        try:
            extra = ((screen,) if screen else ()) + (
                tuple(texts) if len(texts) > 1 else ()
            )
            score = self._ensure_kb().ground(m.text, extra_queries=extra).best_score
        except Exception as exc:  # noqa: BLE001 — 추측 단계 때문에 봇이 멈추면 안 된다
            log.warning("추측 트리거 검색 실패: %s", exc)
            return Verdict(False, "no_trigger", "트리거 조건 불일치")

        # 어미 단서가 있으면 평소 임계값, 단서 없이 유사도만이면 더 높은 문턱.
        # 2026-09-20 측정: 자료에 답이 있는 질문 0.607~0.778, 잡담 최고 0.581,
        # 어미에 걸리는 잡담("다음 주는 뭐 하지")은 0.47 이하.
        need = self._rag_threshold if hint else self._weak_threshold
        if score >= need:
            return Verdict(
                True, "trigger", f"{hint or '유사도'} {score:.3f}", strength="weak"
            )
        return Verdict(
            False,
            "weak_no_grounding",
            f"질문 같지만({hint or '유사도'}) 근거 부족 {score:.3f} < {need}",
        )

    def _read_photo(self, m: Message) -> tuple[str, str]:
        """질문과 같은 묶음의 사진을 받아 글자를 읽는다. (화면 글자, 기록용 메모).

        어떤 실패도 질문 처리를 멈추지 않는다 — 글만으로 계속 답하면 된다.
        화면을 **다시 읽어** 사진이 질문에서 몇 줄 떨어졌는지 정하고, 질문 말풍선을
        기준으로 그 사진을 찾는다 (photo.save_photo). 사진 줄은 저절로 깜빡이므로
        '뒤에서 몇 번째 사진'으로 세면 남의 사진을 받을 수 있다.
        """
        if not self._photo_dir.is_dir():
            return "", f"저장 폴더가 없음: {self._photo_dir}"
        try:
            messages = self._kakao.read(count=20)
        except KakaoError as exc:
            return "", f"화면 다시 읽기 실패: {exc}"
        index = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if messages[i].signature() == m.signature()
            ),
            None,
        )
        if index is None:
            return "", "질문이 화면에서 사라짐"
        offset = nearby_photo(messages, index)
        if offset is None:
            return "", "같은 묶음에 사진 없음"
        try:
            path = save_photo(self._kakao.room, m.text, offset, self._photo_dir)
        except Exception as exc:  # noqa: BLE001 — 사진 때문에 질문이 막히면 안 된다
            log.warning("사진 저장 실패 — 글만으로 답합니다: %s", exc)
            return "", f"사진 저장 실패: {exc}"
        if path is None:
            return "", "사진을 받지 못함 (이미 저장했거나 작은 그림)"
        text = read_and_discard(path)
        log.info("함께 올린 사진에서 글자 %d자를 읽었습니다", len(text))
        return text, f"사진 글자 {len(text)}자"

    def _deliver(
        self, m: Message, text: str, followup: Followup | None = None
    ) -> SendResult:
        """[10] 무작위 지연 → 긴급 정지 재확인 → 댓글 답장(막히면 일반 메시지).

        스레드 추가 질문이면 질문 댓글이 아니라 **원글**의 스레드에 단다.
        """
        room = self._kakao.room
        target, where = m, {}
        if followup is not None:
            target = Message(
                text=followup.parent, sender=None, outgoing=False, time=None
            )
            where = {"nth": followup.nth, "expect": followup.lines[-1]}
        if not self._cfg.dry_run:
            lo, hi = self._send_delay
            if hi > 0:
                time.sleep(random.uniform(max(0.0, lo), hi))

        # LLM 호출(최대 2분)과 지연 사이에 운영자가 멈췄을 수 있다. 보내기 직전에 다시 본다.
        self._reload_config()
        if self._paused_reason:
            return SendResult(False, "cancelled", self._paused_reason)
        if self._kakao.room != room:
            return SendResult(
                False, "cancelled", f"대상 방이 바뀜 ({room} → {self._kakao.room})"
            )

        try:
            result = self._kakao.reply(
                target,
                text,
                mode=self._reply_mode,
                mirror_to_room=self._mirror_to_room,
                **where,
            )
        except KakaoError as exc:
            result = SendResult(False, "failed", str(exc))
        if result.maybe_posted:
            # 다음 폴링에서 이 문구를 질문으로 읽지 않도록. 확인 못 한 것도 넣는다.
            self._sent.add(text)
            self._bot_replies.add(text)
        return result

    def _record_error(self, message: str) -> None:
        self._stats.errors += 1
        self._stats.last_error = message
        self._stats.last_error_at = datetime.now().isoformat(timespec="seconds")

    def _poll_once(self) -> None:
        # 사람이 창을 위로 올려 옛 메시지를 보는 중이면 화면이 '최근 20개'가 아니다
        if self._kakao.scrolled_up():
            if not self._scroll_skip_logged:
                log.info(
                    "방 창이 위로 스크롤돼 있어 읽지 않습니다 — 맨 아래로 내리면 이어서 봅니다."
                )
                self._scroll_skip_logged = True
            return
        self._scroll_skip_logged = False

        wide = self._catch_up or self._interval >= LONG_POLL_SECS
        messages = self._kakao.read(count=CATCH_UP_COUNT if wide else 20)
        if self._last_seen is None:
            self._last_seen = messages
            self._catch_up = False
            log.info(
                "기준선을 다시 잡았습니다 (%d개) — 이번 화면에는 답하지 않습니다.",
                len(messages),
            )
            return
        if self._last_seen:
            index = last_seen_index(self._last_seen, messages)
            if index is None:
                # 직전 화면의 마지막 메시지가 안 보인다. 창이 밀렸거나 한꺼번에 20개 넘게 왔다.
                # 아무것도 새것으로 보지 않고 **기준선을 지킨다** — 바꿔 버리면 창이 돌아왔을 때
                # 이미 처리한 메시지를 또 새것으로 본다 (2026-09-21 15:38·15:39 두 번 재처리).
                self._misaligned += 1
                limit = max(2, round(MISALIGNED_SECS / max(self._interval, 1)))
                log.warning(
                    "직전 화면의 마지막 메시지가 보이지 않습니다 (%d/%d) — 이번 폴링은 답하지 않습니다.",
                    self._misaligned,
                    limit,
                )
                if self._misaligned >= limit:
                    self._last_seen = messages
                    self._misaligned = 0
                    self._catch_up = False
                    log.warning(
                        "기준선을 다시 잡았습니다 — 그 사이 메시지에는 답하지 않습니다."
                    )
                return
            fresh = messages[index + 1 :]
        else:
            fresh = list(messages)  # 빈 방이었다 — 전부 새것
        if self._catch_up and fresh:
            log.info("자리를 비운 사이 올라온 메시지 %d개를 이어서 봅니다.", len(fresh))
        self._catch_up = False
        self._misaligned = 0
        self._last_seen = messages
        room = self._kakao.room
        offset = len(messages) - len(fresh)  # fresh 는 항상 messages 의 꼬리다
        # 스레드 댓글을 '채팅방에 함께 보내기'로 방에도 올린 글은 여기서 답하지 않는다 —
        # '댓글 알림'으로 그 스레드를 열어 거기서 답한다 (_poll_followups, 2026-09-26 운영자 결정)
        items = self._drop_thread_comments(
            [(offset + j, m) for j, m in enumerate(fresh)]
        )
        # 같은 사람이 이어 보낸 메시지는 **한 질문**으로 묶는다 (2026-09-26 운영자 결정).
        # 1분마다 보면 "수강료 얼마예요?" "환불은요?" 가 한꺼번에 잡힌다 — 따로 답하면 둘째가
        # 1인 쿨다운에 걸려 버려졌다. 사진·파일 말고 글이 하나뿐인 묶음은 예전처럼 하나씩.
        groups = self._bursts(items)
        done = 0
        for group in groups:
            # 한 묶음을 처리하는 동안(질문마다 LLM 호출) 멈추거나 방을 바꿀 수 있다
            if self._paused_reason or self._kakao.room != room:
                log.info("처리 중단 — 남은 %d개는 답하지 않습니다", len(items) - done)
                break
            done += len(group)
            texts = [(i, m) for i, m in group if m.text not in MEDIA]
            try:
                if len(texts) < 2:
                    for i, m in group:
                        hint = (
                            self._photo_enabled
                            and nearby_photo(messages, i) is not None
                        )
                        self._handle(m, photo_hint=hint)
                else:
                    parts = tuple(m for _, m in texts)
                    anchor = self._anchor(list(parts))
                    at = next(i for i, m in texts if m is anchor)
                    hint = self._photo_enabled and (
                        len(texts) < len(group)
                        or nearby_photo(messages, at) is not None
                    )
                    self._handle(anchor, photo_hint=hint, parts=parts)
            except KakaoError:
                raise
            except (
                Exception
            ) as exc:  # noqa: BLE001 — 한 메시지 때문에 봇 전체가 죽지 않게
                log.exception("메시지 처리 중 예상하지 못한 오류")
                self._record_error(f"처리 오류: {exc!r}")
                last = group[-1][1]
                self._audit.write(
                    "error", sender=last.sender, text=last.text, error=repr(exc)
                )
                self._answer_breaker.record_error()

    def _drop_thread_comments(
        self, items: list[tuple[int, Message]]
    ) -> list[tuple[int, Message]]:
        """스레드 댓글을 '채팅방에 함께 보내기'로 방에도 올린 글을 뺀다.

        그 글은 '댓글 알림'으로 스레드를 열어 **그 스레드에서** 답한다 — 같은 스레드에 여러
        번 물었어도 스레드를 열면 다 보인다. 방에서 답하면 함께 보낸 글에는 댓글을 달 수 없어
        일반 메시지로 나갔고, 스레드 쪽은 방에서 답했다며 스레드를 열지 않아 함께 보내기를 끈
        앞 댓글까지 놓쳤다 (2026-09-26 실전 12·13번).

        '댓글 알림' 창이 없거나 추가 질문 기능이 꺼져 있으면 빼지 않는다 — 방에서라도 답한다.
        가려내지 못해도(창 읽기 실패) 빼지 않는다.
        """
        if not self._followup_enabled or self._notice_missing_logged:
            return items
        if not any(not m.is_own and m.text not in MEDIA for _, m in items):
            return items
        try:
            rows = self._kakao.thread_comment_rows(count=len(items) + 3)
        except Exception as exc:  # noqa: BLE001 — 못 가리면 예전처럼 방에서 답한다
            log.warning(
                "스레드 댓글인지 확인하지 못했습니다 — 방 메시지로 봅니다: %s", exc
            )
            return items
        kept = []
        for (i, m), flag in zip(
            items, _thread_comment_flags([m for _, m in items], rows)
        ):
            if flag and not m.is_own:
                log.info(
                    "[스레드 댓글] @%s: %s — 방에 함께 올라온 댓글이라 스레드에서 답합니다",
                    m.sender,
                    m.text,
                )
                self._filtered_log.write(
                    "filtered",
                    stage="thread_comment",
                    reason="스레드 댓글을 방에도 함께 보냄 — 스레드('댓글 알림')에서 답함",
                    sender=m.sender,
                    text=m.text,
                )
            else:
                kept.append((i, m))
        return kept

    @staticmethod
    def _bursts(items: list[tuple[int, Message]]) -> list[list[tuple[int, Message]]]:
        """(위치, 메시지) 목록을 '같은 사람이 이어 보낸 묶음'으로 나눈다.

        다른 사람의 말, 내 메시지, 입퇴장 공지, 보낸 사람을 모르는 메시지는 묶음을 끊고
        혼자 한 묶음이 된다.
        """
        groups: list[list[tuple[int, Message]]] = []
        open_sender: str | None = None
        for i, m in items:
            joinable = bool(m.sender) and not m.is_own and not m.is_system_notice
            if joinable and groups and open_sender == m.sender:
                groups[-1].append((i, m))
            else:
                groups.append([(i, m)])
            open_sender = m.sender if joinable else None
        return groups

    # --- 스레드 안의 추가 질문 (2026-09-24, kakaobot.followup) -------------
    def _load_bot_replies(self) -> None:
        """재시작 전에 봇이 단 답을 감사 기록에서 되살린다.

        스레드의 마지막 운영자 댓글이 봇의 답인지 알아야 추가 질문에 답한다. 메모리에만
        두면 재시작할 때마다 잊어, 그 전에 답한 스레드의 추가 질문을 모두 흘려보낸다.
        """
        n = 0
        for path in sorted(self._audit.directory.glob("*.jsonl"))[-BOT_REPLY_DAYS:]:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("reply") and rec.get("via") in (
                    "thread",
                    "plain",
                    "unverified",
                ):
                    self._bot_replies.add(rec["reply"])
                    n += 1
        log.info(
            "감사 기록에서 봇이 단 답 %d개를 불러왔습니다 (스레드 추가 질문 판별용).", n
        )

    def _mark_answered(self, sender: str | None, texts, origin: str) -> None:
        now = time.monotonic()
        for t in texts:
            self._answered[(sender or "", " ".join(t.split()))] = (origin, now)
        for key in [
            k for k, (_, at) in self._answered.items() if now - at > DEDUPE_SECS
        ]:
            del self._answered[key]

    def _answered_elsewhere(self, sender: str | None, text: str, origin: str) -> bool:
        """이 (질문자, 글)을 **다른 쪽**(방 ↔ 스레드)에서 최근에 답했는가."""
        hit = self._answered.get((sender or "", " ".join(text.split())))
        return (
            hit is not None
            and hit[0] != origin
            and time.monotonic() - hit[1] <= DEDUPE_SECS
        )

    def _was_answered(self, sender: str | None, text: str) -> bool:
        """이 (질문자, 글)에 방이든 스레드든 최근에 답했는가."""
        hit = self._answered.get((sender or "", " ".join(text.split())))
        return hit is not None and time.monotonic() - hit[1] <= DEDUPE_SECS

    def _followed_up_recently(self, clipped_parent: str) -> bool:
        """이 스레드의 추가 질문에 방금 답했는가 (RECHECK_SECS 안)."""
        now = time.monotonic()
        return any(
            hits and now - hits[-1] <= RECHECK_SECS and matches(clipped_parent, parent)
            for parent, hits in self._thread_answers.items()
        )

    def _thread_allowed(self, parent: str) -> bool:
        now = time.monotonic()
        hits = self._thread_answers.setdefault(parent, deque())
        while hits and now - hits[0] > THREAD_WINDOW_SECS:
            hits.popleft()
        return len(hits) < self._followup_cap

    def _read_notices(self, catch_up: bool = False) -> list[Notice] | None:
        """'댓글 알림' 창 맨 위 몇 줄. 창이 없으면 한 번만 알리고 None.

        기준선을 잡을 때와 자리에서 돌아왔을 때는 CATCH_UP_ROWS 줄까지 본다. 알림 줄은
        새 댓글이 달리면 맨 위로 올라가므로, 기준선을 넓게 잡아 두면 돌아와서 넓게 읽을 때
        처음 보는 줄은 **그사이 새 댓글이 달린 줄**뿐이다 (옛 댓글에 뒤늦게 답하지 않는다).
        """
        top = self._followup_top
        if catch_up or not self._notices.has_baseline:
            top = max(top, CATCH_UP_ROWS)
        notices = read_notices(self._kakao.room, top=top)
        if notices is None:
            if not self._notice_missing_logged:
                log.warning(
                    "'댓글 알림' 방이 창으로 열려 있지 않아 스레드 안의 추가 질문은 보지 않습니다 "
                    "— 카카오톡 채팅 목록에서 '댓글 알림'을 더블클릭해 창으로 열어 두세요."
                )
                self._notice_missing_logged = True
            self._notices.reset()  # 다시 열리면 그때 화면을 기준선으로
            return None
        if self._notice_missing_logged:
            log.info(
                "'댓글 알림' 창을 찾았습니다 — 지금 알림은 기준선으로 두고 새 알림부터 봅니다."
            )
            self._notice_missing_logged = False
        return notices

    def _baseline_notices(self) -> None:
        """기동 때 알림 화면을 기준선으로 잡는다 — 재시작 전 추가 질문에 뒤늦게 답하지 않게."""
        if not self._followup_enabled:
            return
        try:
            notices = self._read_notices()
        except Exception as exc:  # noqa: BLE001 — 추가 질문 때문에 봇이 못 뜨면 안 된다
            log.warning("'댓글 알림'을 읽지 못했습니다: %s", exc)
            return
        if notices is not None:
            self._notices.fresh(notices)
            log.info(
                "'댓글 알림' 맨 위 %d줄 중 이 방 알림 %d개를 기준선으로 잡았습니다.",
                max(self._followup_top, CATCH_UP_ROWS),
                len(notices),
            )

    def _poll_followups(self) -> None:
        """폴링 몇 번에 한 번 '댓글 알림'을 보고, 새 알림마다 스레드를 확인한다.

        여기서 난 오류는 방 폴링(읽기 서킷 브레이커)에 섞지 않는다. 추가 질문을 못 보는
        것뿐이고 방의 첫 질문은 계속 답해야 한다.
        """
        if not self._followup_enabled:
            return
        self._followup_tick += 1
        catch_up = self._followup_catch_up
        if self._followup_tick % self._followup_every and not catch_up:
            return
        # 사람이 방 창을 올려 보는 중이면 스레드를 열 때 스크롤을 움직이게 된다. 기다린다
        if self._kakao.scrolled_up():
            return
        try:
            notices = self._read_notices(catch_up=catch_up)
            if notices is None:
                return
            self._followup_catch_up = False
            room = self._kakao.room
            for n in self._notices.fresh(notices):
                if self._paused_reason or self._kakao.room != room:
                    break
                self._followup(n)
            self._followup_error = ""
        except KakaoError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if msg != self._followup_error:  # 같은 오류를 폴링마다 쏟지 않는다
                log.warning("스레드 추가 질문 확인 실패 — %s", msg)
                self._followup_error = msg

    def _followup(self, n: Notice) -> None:
        """새 알림 하나: 원글 찾기 → 스레드 읽기 → 봇의 답 뒤 새 댓글이면 같은 스레드에 답한다."""
        room = self._kakao.room
        recheck = self._bot_replies.matches_clipped(n.latest)
        if recheck and not self._followed_up_recently(n.parent):
            return  # 최신 댓글이 봇의 답 — 봇이 방금 답한 스레드다
        # recheck: 추가 질문에 방금 답한 스레드. 답하는 사이 달린 댓글이 있는지 한 번 더 본다
        # 알림의 최신 댓글을 방에서 이미 답했어도 스레드는 연다 — 알림에는 최신 댓글 하나만
        # 보여, 그 앞에 달린 다른 추가 질문을 놓친다 (2026-09-26 실전 12번). 이미 답한 줄은
        # 스레드를 연 뒤 줄마다 뺀다.

        # 방 화면에서 원글 찾기. 기동 뒤 방 폴링과 같은 ax-read 를 쓴다 (접근성 직접 탐색보다 빠르다)
        texts = [m.text for m in self._kakao.read(count=100)]
        candidates = parent_candidates(texts, n.parent)
        if not candidates:
            self._audit.write(
                "followup_skipped",
                reason="원글이 방 화면에 없음 (너무 위로 밀렸을 수 있음)",
                parent=n.parent,
                latest=n.latest,
            )
            log.info(
                "스레드 알림의 원글을 방 화면에서 찾지 못했습니다: %r", n.parent[:30]
            )
            return

        found, why = None, "알림의 최신 댓글이 있는 스레드를 찾지 못함"
        for parent, nth in candidates:
            try:
                messages = read_thread(room, parent, nth=nth)
            except ThreadError as exc:
                why = f"스레드를 열지 못함: {exc}"
                continue
            if any(matches(n.latest, m.text) for m in messages[1:]):
                found = (parent, nth, messages)
                break
        if found is None:
            self._audit.write(
                "followup_skipped", reason=why, parent=n.parent, latest=n.latest
            )
            log.info("스레드 추가 질문을 확인하지 못했습니다 — %s", why)
            return

        parent, nth, messages = found
        fu, why = pick_followup(
            parent, nth, messages, self._bot_replies.contains, self._was_answered
        )
        if fu is None and recheck:
            log.debug("답하는 사이 달린 댓글 없음 — %s", why)
            return
        if fu is None:
            log.info("스레드에 봇이 답할 추가 질문이 없습니다 — %s", why)
            self._filtered_log.write(
                "filtered",
                stage="followup",
                reason=why,
                parent=parent,
                text=n.latest,
            )
            return
        lines = tuple(
            t for t in fu.lines if not self._answered_elsewhere(fu.asker, t, "thread")
        )
        if not lines:
            log.info(
                "방에서 이미 답한 추가 질문입니다 — 스레드에는 다시 답하지 않습니다."
            )
            return
        fu = replace(fu, lines=lines)

        if not self._thread_allowed(parent):
            reason = (
                f"스레드 하나에 {THREAD_WINDOW_SECS // 60}분 동안 "
                f"{self._followup_cap}번까지만 답합니다"
            )
            log.info("레이트 리밋 — %s (원글 %r)", reason, parent[:30])
            self._audit.write(
                "rate_limited",
                reason=reason,
                origin="thread",
                parent=parent,
                sender=fu.asker,
                text=fu.question,
            )
            return
        m = Message(text=fu.question, sender=fu.asker, outgoing=False, time=None)
        try:
            posted = self._handle(m, followup=fu)
        except KakaoError:
            raise
        except Exception as exc:  # noqa: BLE001 — 방 폴링의 메시지 처리와 같게 센다
            log.exception("스레드 추가 질문 처리 중 예상하지 못한 오류")
            self._record_error(f"처리 오류: {exc!r}")
            self._audit.write(
                "error", origin="thread", sender=m.sender, text=m.text, error=repr(exc)
            )
            self._answer_breaker.record_error()
            return
        if posted:
            self._thread_answers.setdefault(parent, deque()).append(time.monotonic())

    def _check_away(self) -> bool:
        """대상 방 창이 지금 화면에 없으면 True — 이번 폴링은 쉬고 기다린다.

        방 창이 닫혔든, 최소화됐든, 다른 데스크탑에 있든 같다 (2026-09-26 운영자 결정 —
        예전에는 이때 읽기가 3번 실패해 서킷 브레이커가 봇을 멈췄고, 9/24 12:12 부터 이틀을
        멈춰 있었다). 읽기 실패로 세지 않는다. 돌아오면 그사이 올라온 질문에 **늦게라도
        답한다** (2026-09-24 운영자 결정) — 방은 넓게 다시 읽고, '댓글 알림' 기준선은 그대로
        둔 채 넓게 본다.

        카카오톡이 안 떠 있으면(None) 기다리지 않는다 — 평소처럼 읽기 실패로 세어 멈춘다.
        """
        visible = self._kakao.room_visible()
        now = time.monotonic()
        room = self._cfg.room_name
        if visible is False:
            if self._away_since is None:
                self._away_since = now
                log.warning(
                    "'%s' 방 창이 지금 화면에 없어 기다립니다 (닫혔거나, 최소화됐거나, 다른 "
                    "데스크탑에 있음) — 돌아오면 그사이 올라온 질문부터 이어서 답합니다.",
                    room,
                )
            elif not self._away_notified and now - self._away_since >= AWAY_NOTIFY_SECS:
                self._away_notified = True
                msg = (
                    f"'{room}' 방 창이 {AWAY_NOTIFY_SECS // 60}분째 화면에 없습니다 — 봇은 "
                    "기다리는 중입니다. 방 창을 다시 열거나(최소화했다면 되살리기), 방 창이 있는 "
                    "데스크탑으로 돌아와 주세요."
                )
                log.warning(msg)
                self._notifier.send(f"⏸ {msg}")
            return True
        if self._away_since is not None:
            log.info(
                "'%s' 방 창이 다시 보입니다 (%d초 만) — 그사이 올라온 메시지부터 이어서 봅니다.",
                room,
                int(now - self._away_since),
            )
            self._away_since = None
            self._away_notified = False
            self._catch_up = True
            self._followup_catch_up = True
        return False

    def _purge_if_due(self) -> None:
        """하루 한 번 보존 기간이 지난 기록을 지운다."""
        from datetime import date

        today = date.today()
        if self._last_purge_day == today:
            return
        self._last_purge_day = today
        purge_old(self._audit.directory, self._keep_audit)
        purge_old(self._filtered_log.directory, self._keep_filtered)

    def _run_canary_if_due(self) -> None:
        """주기적 자가진단. 조용한 먹통을 잡는다 (계획서 §6-5).

        실패가 이어질 때 알림을 반복해 보내지 않는다 — 상태가 바뀔 때만 알린다.
        """
        if self._canary_every <= 0 or self._away_since is not None:
            return  # 창이 다른 데스크탑에 있으면 당연히 실패한다
        now = time.monotonic()
        if now - self._last_canary < self._canary_every:
            return
        self._last_canary = now

        ok, detail = canary_check(self._kakao)
        if not ok and not self._canary_failed:
            log.error("자가진단 실패 — %s", detail)
            self._notifier.send(format_canary_failure(detail))
            self._audit.write("canary_failed", detail=detail)
            self._canary_failed = True
        elif ok and self._canary_failed:
            log.info("자가진단 복구됨")
            self._notifier.send("✅ 봇 자가진단 복구 — 정상 동작 중입니다.")
            self._audit.write("canary_recovered")
            self._canary_failed = False

    def _write_status(self) -> None:
        """메뉴바가 읽는 하트비트. 실패해도 봇은 계속 돈다."""
        try:
            control.write_status(
                self._root,
                {
                    "pid": os.getpid(),
                    "state": self._state,
                    "paused_reason": self._paused_reason,
                    "started_at": self._started_at,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "updated_ts": time.time(),
                    "room": self._cfg.room_name,
                    "mode": self._cfg.mode,
                    "enabled": self._cfg.enabled,
                    "dry_run": self._cfg.dry_run,
                    "reply_mode": self._reply_mode,
                    "stats": asdict(self._stats),
                    "breaker": {
                        "read_tripped": self._breaker.tripped,
                        "answer_tripped": self._answer_breaker.tripped,
                    },
                    "canary_failed": self._canary_failed,
                },
            )
        except OSError as exc:
            log.debug("상태 파일을 쓰지 못했습니다: %s", exc)

    # --- 메인 루프 ------------------------------------------------------
    def run(
        self, interval: int | None = None, max_iterations: int | None = None
    ) -> Stats:
        """interval 을 주지 않으면 설정 [poll] interval_secs 를 쓴다 (바꾸면 다음 폴링부터)."""
        lock = control.ServiceLock(self._root)
        lock.acquire()
        try:
            return self._run(interval, max_iterations)
        finally:
            if self._state not in ("tripped", "error"):
                self._state = "stopped"
            self._write_status()
            lock.release()

    def _run(self, interval: int | None, max_iterations: int | None) -> Stats:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self._interval = interval if interval is not None else self._poll_interval
        log.info(
            "시작 — 방=%r 모드=%s dry_run=%s enabled=%s 폴링 %d초",
            self._cfg.room_name,
            self._cfg.mode,
            self._cfg.dry_run,
            self._cfg.enabled,
            self._interval,
        )

        # 카카오톡 창이 다른 데스크탑에 있으면 돌아올 때까지 기다린다. 메뉴바는 모든
        # 데스크탑에 보이므로 다른 데스크탑에서 '봇 시작'을 누를 수 있다.
        while not self._stop and self._kakao.windows_visible() is False:
            if self._state != "away":
                log.warning(
                    "카카오톡 창이 지금 화면(데스크탑)에 없어 기다립니다 — 보이면 시작합니다."
                )
                self._state = "away"
            self._write_status()
            self._sleep(self._interval)
        if self._stop:
            return self._stats

        if not self._kakao.window_is_open():
            self._state = "error"
            self._record_error(f"'{self._cfg.room_name}' 방 창이 열려 있지 않음")
            raise RoomNotOpenError(
                f"'{self._cfg.room_name}' 방이 별도 창으로 열려 있지 않습니다. "
                "카카오톡에서 방을 더블클릭해 창으로 띄워주세요 (계획서 §7)."
            )

        # 기동 시 화면에 있던 메시지는 처리 대상에서 뺀다.
        # 없으면 재시작할 때마다 과거 질문에 답한다.
        self._last_seen = self._kakao.read(count=20)
        log.info(
            "기동 시 기존 메시지 %d개를 기준선으로 잡았습니다.", len(self._last_seen)
        )
        if self._followup_enabled:
            self._load_bot_replies()
            self._baseline_notices()
        self._state = "running"

        iterations = 0
        was_paused = False
        while not self._stop:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            self._reload_config()
            # 설정에서 간격을 바꾸면 다음 폴링부터 (명령줄 --interval 이 있으면 그대로)
            self._interval = interval if interval is not None else self._poll_interval

            if self._breaker.tripped or self._answer_breaker.tripped:
                which = "읽기" if self._breaker.tripped else "답변·전송"
                log.error("서킷 브레이커 작동(%s 연속 실패) — 정지합니다.", which)
                self._notifier.send(
                    f"🚨 서킷 브레이커 작동({which} 연속 실패) — 봇이 정지했습니다.\n"
                    f"마지막 오류: {self._stats.last_error or '(없음)'}"
                )
                self._audit.write(
                    "breaker_tripped", which=which, last_error=self._stats.last_error
                )
                self._state = "tripped"
                break
            if self._paused_reason:
                log.debug("쉬는 중 — %s", self._paused_reason)
                self._state = "paused"
                self._write_status()
                was_paused = True
                self._sleep(self._interval)
                continue
            if was_paused:
                # 정지 중에는 방을 읽지 않는다. 그대로 재개하면 그사이 올라온 질문을 새 메시지로
                # 보고 한꺼번에(늦게) 답한다. 재개 후 첫 화면은 기준선만 잡는다.
                log.info("재개 — 정지 중에 올라온 메시지에는 답하지 않습니다.")
                self._last_seen = None
                self._notices.reset()  # 정지 중에 달린 추가 질문도 마찬가지
                was_paused = False
            self._state = "running"

            self._purge_if_due()
            self._run_canary_if_due()

            try:
                if self._check_away():
                    self._state = "away"
                else:
                    self._poll_once()
                    self._breaker.record_success()
                    self._poll_followups()
            except (RoomNotOpenError, RoomMinimizedError) as exc:
                # 운영 조건 위반은 사용자가 고쳐야 한다. 조용히 넘기지 않는다.
                self._record_error(str(exc))
                log.error("운영 조건 위반: %s", exc)
                self._breaker.record_error()
            except KakaoError as exc:
                self._record_error(str(exc))
                log.error("카카오 I/O 오류: %s", exc)
                self._breaker.record_error()

            if self._state == "answering":
                self._state = "running"
            self._write_status()
            self._sleep(self._interval)

        log.info(
            "종료 — 확인 %d / 트리거 %d / 답변 %d / 에스컬 %d / 오류 %d",
            self._stats.seen,
            self._stats.triggered,
            self._stats.answered,
            self._stats.escalated,
            self._stats.errors,
        )
        return self._stats
