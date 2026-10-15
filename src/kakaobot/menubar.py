"""메뉴바 제어판 (계획서 §6-4).

    .venv/bin/python -m kakaobot.menubar

봇(서비스)과 **별도 프로세스**다. 메뉴바를 꺼도 봇은 계속 돌고, 봇이 죽어도 메뉴바는 남는다.
둘은 파일로만 이야기한다 (control.py):

    긴급 정지 · 실제 전송 · 모드 · 답변 방식  →  config.toml 을 고친다
        서비스는 폴링마다, 그리고 **보내기 직전에** 설정을 다시 읽는다
    상태 표시                                  ←  data/run/status.json (서비스가 폴링마다 씀)
    실행 여부                                  ←  data/run/service.lock (잠금)

제목 표시:
    🤖⚪ 꺼짐   🤖🧪 연습 모드(보내지 않음)   🤖🟢 실제 전송   🤖⏸ 긴급 정지
    🤖💬 답변 만드는 중   🤖⏳ 응답 없음   🤖🔴 오류로 멈춤
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import rumps

from . import control
from .kakao import allowed_send_chats
from .secrets import has_secret

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "config.toml"

MODES = [
    ("claude-cli", "claude -p (구독)"),
    ("api", "Anthropic API"),
    ("local", "로컬 LLM"),
]
REPLY_MODES = [("thread", "댓글로 답하기"), ("plain", "일반 메시지로 답하기")]

REFRESH_SECS = 3
STALE_SECS = 45  # 이보다 오래 상태가 안 바뀌면 '응답 없음'
STOP_GRACE_SECS = 20  # 중지를 누르고 이만큼 지나도 살아 있으면 강제 종료를 권한다


def _ago(ts: float) -> str:
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}초 전"
    if secs < 3600:
        return f"{secs // 60}분 전"
    return f"{secs // 3600}시간 전"


class BotMenu(rumps.App):
    def __init__(self, root: Path = ROOT, config_path: Path = CONFIG) -> None:
        super().__init__("카카오봇", title="🤖…", quit_button=None)
        self.root = root
        self.config_path = config_path
        self.log_dir = root / "logs"
        self._proc: subprocess.Popen | None = None
        self._stop_requested_at: float | None = None
        self._checking = False
        self._last_error_full = ""

        self.i_state = rumps.MenuItem("상태: 확인 중…")
        self.i_room = rumps.MenuItem("방: —")
        self.i_today = rumps.MenuItem("오늘: —")
        self.i_error = rumps.MenuItem(
            "마지막 오류: 없음", callback=self.show_last_error
        )

        self.i_enabled = rumps.MenuItem("⏸ 긴급 정지", callback=self.toggle_enabled)
        self.i_dry = rumps.MenuItem("실제 전송 켜기…", callback=self.toggle_dry_run)

        self.i_mode = rumps.MenuItem("답변 엔진")
        self._mode_items = {}
        for key, label in MODES:
            item = rumps.MenuItem(label, callback=self._mode_setter(key))
            self._mode_items[key] = item
            self.i_mode.add(item)
        self.i_mode.add(rumps.separator)
        self.i_check = rumps.MenuItem(
            "엔진 점검 (시험 질문 3개)…", callback=self.check_llm
        )
        self.i_mode.add(self.i_check)

        self.i_reply = rumps.MenuItem("답변 방식")
        self._reply_items = {}
        for key, label in REPLY_MODES:
            item = rumps.MenuItem(label, callback=self._reply_setter(key))
            self._reply_items[key] = item
            self.i_reply.add(item)

        self.i_service = rumps.MenuItem("봇 시작", callback=self.toggle_service)

        self.i_open = rumps.MenuItem("열기")
        for label, fn in [
            ("오늘 처리 기록", self._open_today_audit),
            (
                "운영 로그",
                lambda _: self._open(self.log_dir / "kakaobot.log", text=True),
            ),
            (
                "봇 실행 오류 기록",
                lambda _: self._open(self.log_dir / "service.err", text=True),
            ),
            ("설정 파일", lambda _: self._open(self.config_path, text=True)),
            ("지식베이스 폴더", lambda _: self._open(self.root / "data" / "knowledge")),
            (
                "로컬 LLM 안내",
                lambda _: self._open(self.root / "docs" / "local-llm.md", text=True),
            ),
        ]:
            self.i_open.add(rumps.MenuItem(label, callback=fn))

        self.menu = [
            self.i_state,
            self.i_room,
            self.i_today,
            self.i_error,
            None,
            self.i_enabled,
            self.i_dry,
            self.i_mode,
            self.i_reply,
            None,
            self.i_service,
            self.i_open,
            None,
            rumps.MenuItem("메뉴바 닫기 (봇은 계속 돎)", callback=self.quit_menubar),
        ]

        self._counts_cache: tuple = ((), None)
        self.timer = rumps.Timer(self.refresh, REFRESH_SECS)
        self.timer.start()
        self.refresh()

    # --- 읽기 -----------------------------------------------------------
    def _config(self) -> tuple[dict, str]:
        try:
            return control.read_config(self.config_path), ""
        except Exception as exc:  # noqa: BLE001 — 깨진 설정도 화면에는 보여야 한다
            return {}, str(exc)

    def _counts(self):
        """오늘 파일 크기가 바뀔 때만 다시 센다."""
        day = date.today().isoformat()
        key = tuple(
            (p.stat().st_size if p.exists() else -1)
            for p in (
                self.log_dir / "audit" / f"{day}.jsonl",
                self.log_dir / "filtered" / f"{day}.jsonl",
            )
        ) + (day,)
        if self._counts_cache[0] != key:
            self._counts_cache = (key, control.today_counts(self.log_dir))
        return self._counts_cache[1]

    # --- 화면 갱신 -------------------------------------------------------
    def refresh(self, _=None) -> None:
        cfg, cfg_error = self._config()
        mode_cfg = cfg.get("mode", {})
        enabled = bool(mode_cfg.get("enabled", False))
        dry_run = bool(mode_cfg.get("dry_run", True))
        engine = str(mode_cfg.get("current", "?"))
        reply_mode = str(cfg.get("send", {}).get("reply_mode", "thread"))
        room = str(cfg.get("room", {}).get("name", "?"))

        pid = control.running_pid(self.root)
        st = control.read_status(self.root) or {}
        fresh = st.get("pid") == pid and pid is not None
        age = time.time() - float(st.get("updated_ts", 0)) if fresh else None
        state = st.get("state") if fresh else None

        self._check_started_process()

        # 제목과 상태 줄
        if pid is None:
            last = st.get("state")
            if last == "tripped":
                title, text = (
                    "🤖🔴",
                    "꺼짐 — 서킷 브레이커로 멈췄습니다 (오류 확인 후 다시 시작)",
                )
            elif last == "error":
                title, text = "🤖🔴", "꺼짐 — 오류로 멈췄습니다"
            else:
                title, text = "🤖⚪", "꺼짐"
        elif cfg_error or (
            fresh and st.get("paused_reason", "").startswith("설정 파일 오류")
        ):
            title, text = "🤖⏸", "설정 파일 오류 — 고칠 때까지 답하지 않음"
        elif not enabled:
            title, text = "🤖⏸", "긴급 정지 중 — 답하지 않음"
        elif state == "answering" and age is not None and age < 180:
            title, text = "🤖💬", "답변 만드는 중…"
        elif not fresh or age is None or age > STALE_SECS:
            title, text = "🤖⏳", "응답 없음" + (
                f" (마지막 신호 {_ago(st['updated_ts'])})" if fresh else " (시작 중?)"
            )
        elif state == "away":
            title, text = "🤖💤", "방 창이 화면에 없음 (닫힘·최소화·다른 데스크탑) — 돌아오면 그사이 질문에 답함"
        elif dry_run:
            title, text = "🤖🧪", "실행 중 · 연습 모드 (보내지 않음)"
        else:
            title, text = "🤖🟢", "실행 중 · 실제 전송"
        if fresh and st.get("canary_failed"):
            title += "⚠️"
            text += " · 자가진단 실패"
        self.title = title
        self.i_state.title = f"상태: {text}"

        allowed = room in allowed_send_chats()
        self.i_room.title = (
            f"방: {room}"
            + ("" if allowed else "  ⚠️ 전송 허용 목록에 없음")
            + f" · {dict(MODES).get(engine, engine)}"
        )

        c = self._counts()
        self.i_today.title = (
            f"오늘: 답변 {c['answer']} · 안내 {c['escalate'] + c['llm_error']}"
            f" · 오류 {c['error'] + c['llm_error']} · 거름 {c['filtered']}"
            + (f" · 제한 {c['rate_limited']}" if c["rate_limited"] else "")
        )

        stats = st.get("stats", {}) if st else {}
        err = cfg_error or stats.get("last_error", "")
        if err:
            when = stats.get("last_error_at", "")[11:16] if not cfg_error else "지금"
            self._last_error_full = f"[{when}] {err}"
            short = err if len(err) <= 38 else err[:37] + "…"
            self.i_error.title = f"마지막 오류 ({when}): {short}"
        else:
            self._last_error_full = ""
            self.i_error.title = "마지막 오류: 없음"

        # 토글 표시
        self.i_enabled.title = "▶︎ 답변 재개" if not enabled else "⏸ 긴급 정지"
        self.i_dry.title = (
            "실제 전송 켜기…" if dry_run else "연습 모드로 돌리기 (보내지 않음)"
        )
        for key, item in self._mode_items.items():
            item.state = int(key == engine)
        local = cfg.get("local", {})
        self._mode_items["local"].title = "로컬 LLM" + (
            "" if local.get("endpoint") and local.get("model") else " (미설정)"
        )
        for key, item in self._reply_items.items():
            item.state = int(key == reply_mode)
        self.i_check.title = (
            "엔진 점검 중…" if self._checking else "엔진 점검 (시험 질문 3개)…"
        )

        if pid is None:
            self.i_service.title = "봇 시작"
            self._stop_requested_at = None
        elif (
            self._stop_requested_at
            and time.time() - self._stop_requested_at > STOP_GRACE_SECS
        ):
            self.i_service.title = "봇 강제 종료 (응답 없음)"
        elif self._stop_requested_at:
            self.i_service.title = "봇 중지 중…"
        else:
            self.i_service.title = f"봇 중지 (pid {pid})"

    def _check_started_process(self) -> None:
        """메뉴바가 띄운 봇이 곧바로 죽었으면 이유를 보여준다 (예: 방 창이 안 열려 있음)."""
        if self._proc is None or self._proc.poll() is None:
            return
        code, self._proc = self._proc.returncode, None
        if code in (0, None) or self._stop_requested_at:
            return
        tail = self._tail(self.log_dir / "service.err", 6)
        hints = {
            1: "카카오톡 오류 — 테스트방이 별도 창으로 열려 있는지 확인하세요.",
            2: "설정 오류 — 설정 파일을 확인하세요.",
            3: "이미 다른 봇이 실행 중입니다.",
        }
        self._alert(
            "봇이 시작 직후 멈췄습니다",
            f"{hints.get(code, f'종료 코드 {code}')}\n\n{tail}",
        )

    @staticmethod
    def _tail(path: Path, n: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

    # --- 동작 -----------------------------------------------------------
    def _set(self, updates: dict) -> bool:
        try:
            control.set_config_values(self.config_path, updates)
        except Exception as exc:  # noqa: BLE001
            self._alert("설정을 바꾸지 못했습니다", str(exc))
            return False
        self.refresh()
        return True

    def toggle_enabled(self, _) -> None:
        cfg, err = self._config()
        if err:
            self._alert("설정 파일이 깨져 있습니다", f"먼저 고쳐 주세요.\n\n{err}")
            return
        enabled = bool(cfg.get("mode", {}).get("enabled", False))
        # 긴급 정지는 확인 없이 바로. 재개도 바로 (재개해도 옛 질문에는 답하지 않는다)
        self._set({("mode", "enabled"): not enabled})

    def toggle_dry_run(self, _) -> None:
        cfg, err = self._config()
        if err:
            self._alert("설정 파일이 깨져 있습니다", err)
            return
        dry_run = bool(cfg.get("mode", {}).get("dry_run", True))
        if dry_run:
            room = cfg.get("room", {}).get("name", "?")
            if room not in allowed_send_chats():
                self._alert(
                    "보낼 수 없는 방입니다",
                    f"'{room}' 은 ~/.config/openkakao/config.toml 의 전송 허용 목록에 없습니다.",
                )
                return
            engine = dict(MODES).get(cfg.get("mode", {}).get("current"), "?")
            if not self._alert(
                "실제 전송을 켤까요?",
                f"'{room}' 방에 봇 답변이 실제로 올라갑니다.\n"
                f"답변 엔진: {engine}\n\n언제든 '긴급 정지'로 멈출 수 있습니다.",
                ok="실제 전송 켜기",
                cancel="취소",
            ):
                return
        self._set({("mode", "dry_run"): not dry_run})

    def _mode_setter(self, key: str):
        def set_mode(_):
            cfg, err = self._config()
            if err:
                self._alert("설정 파일이 깨져 있습니다", err)
                return
            if key == "local":
                local = cfg.get("local", {})
                if not (local.get("endpoint") and local.get("model")):
                    self._alert(
                        "로컬 LLM 이 아직 설정되지 않았습니다",
                        "실행 도구는 새 Mac 을 산 뒤 정하기로 했습니다.\n"
                        "config.toml 의 [local] endpoint·model 을 채운 뒤 바꾸세요.\n"
                        "(열기 → 로컬 LLM 안내)",
                    )
                    return
            if key == "api" and not has_secret("ANTHROPIC_API_KEY"):
                self._alert(
                    "API 키가 없습니다",
                    "터미널에서 scripts/setup-secrets.sh 를 실행해 키체인에 저장한 뒤 바꾸세요.",
                )
                return
            self._set({("mode", "current"): key})

        return set_mode

    def _reply_setter(self, key: str):
        def set_reply(_):
            self._set({("send", "reply_mode"): key})

        return set_reply

    def toggle_service(self, _) -> None:
        pid = control.running_pid(self.root)
        if pid is None:
            self._start_service()
            return
        if pid < 0:
            self._alert(
                "봇 pid 를 알 수 없습니다", "data/run/service.lock 을 확인하세요."
            )
            return
        if (
            self._stop_requested_at
            and time.time() - self._stop_requested_at > STOP_GRACE_SECS
        ):
            if self._alert(
                "봇을 강제 종료할까요?",
                "답변을 보내는 도중이었다면 그 답변은 나가지 않았거나, "
                "나갔는지 확인되지 않습니다.",
                ok="강제 종료",
                cancel="취소",
            ):
                os.kill(pid, signal.SIGKILL)
            return
        os.kill(pid, signal.SIGTERM)
        self._stop_requested_at = time.time()
        self.refresh()

    def _start_service(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        err = (self.log_dir / "service.err").open("a", encoding="utf-8")
        err.write(
            f"\n--- {datetime.now().isoformat(timespec='seconds')} 메뉴바에서 시작 ---\n"
        )
        err.flush()
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kakaobot",
                "--config",
                str(self.config_path),
                "--no-console",
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=err,
            stderr=err,
            start_new_session=True,  # 메뉴바가 닫혀도 봇은 계속 돈다
        )
        err.close()
        self._stop_requested_at = None
        self.refresh()

    def check_llm(self, _) -> None:
        if self._checking:
            return
        self._checking = True
        self.refresh()

        def work():
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "kakaobot",
                        "--config",
                        str(self.config_path),
                        "--check-llm",
                    ],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                out = (proc.stdout + proc.stderr).strip()
                ok = proc.returncode == 0
            except Exception as exc:  # noqa: BLE001
                out, ok = str(exc), False
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self._check_done, ok, out)

        threading.Thread(target=work, daemon=True).start()

    def _check_done(self, ok: bool, out: str) -> None:
        self._checking = False
        self.refresh()
        self._alert("엔진 점검 " + ("통과 ✅" if ok else "실패 ❌"), out[-1800:])

    def show_last_error(self, _) -> None:
        if self._last_error_full:
            self._alert("마지막 오류", self._last_error_full)

    def _open_today_audit(self, _) -> None:
        path = self.log_dir / "audit" / f"{date.today().isoformat()}.jsonl"
        self._open(path if path.exists() else path.parent, text=path.exists())

    def _open(self, path: Path, text: bool = False) -> None:
        if not path.exists():
            self._alert("아직 없습니다", str(path))
            return
        subprocess.run(
            ["open", "-t", str(path)] if text else ["open", str(path)], check=False
        )

    def quit_menubar(self, _) -> None:
        rumps.quit_application()

    # --- 알림창 ---------------------------------------------------------
    def _alert(
        self, title: str, message: str, ok: str = "확인", cancel: str | None = None
    ) -> bool:
        from AppKit import NSApplication

        # 사용자가 메뉴를 눌러서 뜨는 창이다 — 앞으로 가져와도 된다
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return rumps.alert(title=title, message=message, ok=ok, cancel=cancel) == 1


def _single_instance(root: Path):
    """메뉴바가 두 개 뜨지 않게 한다. 잠금 파일 핸들을 돌려준다 (프로세스 동안 쥐고 있는다)."""
    fh = (control.run_dir(root) / "menubar.lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("메뉴바가 이미 떠 있습니다.", file=sys.stderr)
        raise SystemExit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> None:
    lock = _single_instance(
        ROOT
    )  # noqa: F841 — 프로세스가 끝날 때까지 쥐고 있어야 한다
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    # Dock 아이콘 없이 메뉴바에만 뜬다
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
    BotMenu().run()


if __name__ == "__main__":
    main()
