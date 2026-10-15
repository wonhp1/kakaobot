"""카카오톡 I/O 어댑터.

계획서 §3 원칙: I/O 계층은 직접 만들지 않고 openkakao-cli(패치 001~003 적용본)를
감싼다. 카카오톡 UI가 바뀌면 CLI/패치만 손보면 되고 답변 로직은 건드리지 않는다.

Phase 0에서 실증된 제약 (개발 초기 실측):
  - 대상 방이 별도 창으로 열려 있어야 한다
  - 창이 최소화되어 있으면 안 된다 (화면 밖 배치는 허용)
  - 전송 직전에 **AXRaise → 입력창 AXFocused=true** 순서를 지켜야 한다.
    AXRaise 없이 포커스만 주면 설정은 성공하지만 반영되지 않는다(focused 가
    계속 false). 둘 다 앱을 포그라운드로 가져오지 않아 사용자 포커스는 유지된다.
  - ax-watch 이벤트의 message/author/logId 는 신뢰할 수 없다 → 신호로만 쓰고
    본문은 ax-read 로 다시 읽는다
"""

from __future__ import annotations

import json
import logging
import re
import random
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kakaobot.kakao")

# openkakao-cli 의 전송 허용 목록. 일반 메시지는 CLI 가 이 목록으로 막는다.
# 댓글은 AX 를 직접 다뤄 CLI 를 거치지 않으므로 같은 목록을 여기서 다시 확인한다
# — 허용 목록이 두 벌이 되지 않게 한 곳(이 파일)만 본다.
OPENKAKAO_CONFIG = Path.home() / ".config" / "openkakao" / "config.toml"

# 시스템 공지. 화면 구조로는 연속 메시지와 구분되지 않는다 (2026-09-20 실측:
# 둘 다 '본문 하나'뿐이고 이름·시각 라벨이 없다). 그래서 문구로 가른다.
_SYSTEM_NOTICE = re.compile(
    r"님이 (들어왔|나갔)습니다"
    r"|님을 (초대했|내보냈)습니다"
    r"|님이 방장이 되었습니다"
    r"|관리자가 되었습니다"
    r"|공지가 (등록|수정)되었습니다"
    r"|오픈채팅방입니다"
    r"|^여기까지 읽었습니다\.?$"  # 안읽음 구분선 (메시지가 아니라 UI 줄)
)


def allowed_send_chats(path: Path = OPENKAKAO_CONFIG) -> list[str]:
    """보내도 되는 방 이름 목록 (완전 일치). 못 읽으면 빈 목록 — 아무 데도 보내지 않는다."""
    try:
        with Path(path).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [str(c) for c in data.get("safety", {}).get("allowed_send_chats", [])]


# 전송 성공 조건 3번을 충족시키는 스크립트. 앱을 활성화하지 않으므로
# 사용자 포커스를 뺏지 않는다 (Phase 0 V5 통과 근거).
_FOCUS_COMPOSER = """
tell application "System Events" to tell process "KakaoTalk"
  set w to first window whose name is "{room}"
  if (value of attribute "AXMinimized" of w) is true then
    return "MINIMIZED"
  end if
  -- 1단계: 대상 창을 카카오톡의 메인 창으로 올린다.
  -- AXRaise 는 앱을 포그라운드로 가져오지 않으므로 사용자 포커스를 뺏지 않는다.
  try
    perform action "AXRaise" of w
  end try
  delay 0.3
  -- 2단계: 입력창에 포커스를 준다. 1단계 없이 하면 설정은 되지만 반영되지 않는다.
  repeat with sa in (scroll areas of w)
    try
      if (count of text areas of sa) > 0 then
        set ta to first text area of sa
        set value of attribute "AXFocused" of ta to true
        delay 0.2
        if (value of attribute "AXFocused" of ta) is true then return "OK"
      end if
    end try
  end repeat
  return "NO_COMPOSER"
end tell
"""

_BOT_PREFIX = "🤖 [Sent via openkakao]"


class KakaoError(Exception):
    pass


class RoomNotOpenError(KakaoError):
    """대상 방이 별도 창으로 열려 있지 않다."""


class RoomMinimizedError(KakaoError):
    """대상 방 창이 최소화되어 전송할 수 없다."""


@dataclass(frozen=True)
class Message:
    text: str
    sender: str | None
    outgoing: bool
    time: str | None
    # 그 줄에 **실제로 찍힌** 이름표. sender 는 앞 줄에서 물려받은 이름일 수 있지만
    # label 은 물려받지 않는다 — 이름표가 없는 줄은 "". 안 주면(테스트 등) sender 로 본다.
    label: str | None = None

    def __post_init__(self) -> None:
        if self.label is None:
            object.__setattr__(self, "label", self.sender or "")

    @property
    def is_system_notice(self) -> bool:
        """ "…님이 들어왔습니다." 같은 시스템 공지. **문구로만** 가른다.

        2026-09-20 테스트방 실측으로 앞선 판별자를 버렸다. 그때까지는 "시각 라벨이
        없으면 공지"로 봤는데, 카카오톡은 시각을 **연속 발화 묶음의 마지막 메시지에만**
        붙인다. 그래서 묶음 가운데 메시지가 전부 공지로 오판됐고, 한 번에 올라온 질문
        9개 중 7개가 발신자 불명으로 떨어졌다.

            공지         AXTextArea 하나
            묶음 첫 줄    프로필 + 이름 라벨 + AXTextArea
            묶음 가운데   AXTextArea 하나          ← 공지와 구조가 같다
            묶음 마지막   시각 라벨 + AXTextArea

        구조가 같으므로 화면으로는 구분할 수 없다. 문구는 카카오톡이 정한 정형문이다.
        """
        return bool(_SYSTEM_NOTICE.search(self.text))

    @property
    def is_own(self) -> bool:
        """봇/운영자 자신의 메시지. 무한 루프 방지의 1차 관문 (계획서 §6-1 [1]).

        말풍선 위치(outgoing)로 판단한다. 접두어 검사는 접두어를 떼기 전
        (2026-09-19 이전)에 보낸 메시지를 거르기 위해 남겨 둔다.
        두 번째 관문은 서비스의 SentLog — 봇이 최근 보낸 문장과 대조한다.
        """
        return self.outgoing or self.text.startswith(_BOT_PREFIX)

    def signature(self) -> str:
        """같은 메시지인지 비교할 때 쓰는 값.

        `time` 은 날짜 단위(`2026. 9. 17.`)라 같은 사람이 같은 날 같은 질문을
        반복하면 구분되지 않는다. 그래서 이 값만으로 중복을 판정하지 않고,
        폴링 간 **목록 정렬 비교**(KakaoClient.diff_new)로 새 메시지를 찾는다.
        계획서 §5 0-3 의 "화면 위치 인덱스"에 해당한다.

        **물려받은 sender 가 아니라 그 줄의 이름표(label)** 를 쓴다. 물려받은 이름은
        창에 따라 바뀐다 — 묶음의 이름표 줄이 창 위로 밀려나면 아래 줄들이 이름을
        잃는다. 그러면 같은 메시지의 서명이 달라져 겹침을 못 찾고, 화면 전체를 새
        메시지로 다시 처리했다 (2026-09-21 실전: 어제 메시지 20여 개를 다시 판정.
        재현: 9개 창과 12개 창에서 같은 메시지 3개의 서명이 달랐다).
        """
        return f"{self.label or ''}|{self.outgoing}|{self.text}"


@dataclass(frozen=True)
class SendResult:
    """전송 결과.

    via:
      thread     댓글로 달았고 스레드 창에서 확인함
      plain      일반 메시지로 보냈고 방에서 확인함 (댓글 실패 시 대체 경로 포함)
      dry-run    보내지 않음 (연습 모드)
      unverified 전송 버튼/Return 은 눌렀지만 확인하지 못함 — **다시 보내면 안 된다**
      failed     보내지 못함 (보내기 전 단계에서 멈춤)
      cancelled  보내기 직전에 취소함 (긴급 정지 등, 서비스가 채운다)
    """

    ok: bool
    via: str
    detail: str = ""

    @property
    def maybe_posted(self) -> bool:
        """방에 올라갔을 수 있는가. SentLog 에 넣을지 판단할 때 쓴다."""
        return self.via in ("thread", "plain", "unverified")


# openkakao-cli 가 Return 을 누른 뒤 확인에 실패했을 때 내는 문구 (ax_send.rs).
# 이 경우 메시지가 나갔을 수 있으므로 절대 다시 보내지 않는다.
_UNCERTAIN_MARKERS = ("sent the message but", "after commit began")


class KakaoClient:
    def __init__(
        self,
        cli_path: Path,
        room: str,
        dry_run: bool = True,
        allowlist_path: Path = OPENKAKAO_CONFIG,
    ) -> None:
        self._cli = Path(cli_path)
        self._room = room
        self._dry_run = dry_run
        self._allowlist_path = Path(allowlist_path)
        if not self._cli.exists():
            raise KakaoError(f"openkakao-cli 가 없습니다: {self._cli}")

    @property
    def room(self) -> str:
        return self._room

    # --- 내부 -----------------------------------------------------------
    def _run(self, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self._cli), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @staticmethod
    def _osascript(script: str, timeout: int = 15) -> str:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise KakaoError(f"osascript 실패: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # --- 공개 API -------------------------------------------------------
    def window_is_open(self) -> bool:
        """대상 방이 별도 창으로 열려 있는지 확인한다."""
        script = (
            'tell application "System Events" to tell process "KakaoTalk" '
            "to return name of every window"
        )
        try:
            names = self._osascript(script)
        except KakaoError:
            return False
        return self._room in [n.strip() for n in names.split(",")]

    def windows_visible(self) -> bool | None:
        """카카오톡 창이 **지금 보이는 데스크탑(Space)** 에 하나라도 있는가.

        접근성 API 는 지금 데스크탑의 창만 돌려준다. 사람이 다른 데스크탑이나 전체 화면
        앱으로 가면 창은 그대로 있어도 목록이 빈다 (2026-09-24 00:54 실측 — 카카오톡 창
        13개가 모두 화면 밖이었고, 읽기가 3번 실패해 봇이 멈췄다).
        카카오톡이 안 떠 있거나 확인하지 못하면 None — 그때는 평소처럼 읽기 실패로 센다.
        """
        try:
            from .thread import _AX

            return bool(_AX().windows())
        except Exception:  # noqa: BLE001 — 이 점검 때문에 봇이 멈추면 안 된다
            return None

    def room_visible(self) -> bool | None:
        """대상 방 창이 지금 데스크탑에 **최소화되지 않은 채** 보이는가.

        봇이 실제로 읽는 것은 이 창 하나다. 메인 채팅 목록 창은 없어도 된다 — openkakao 는
        방 창을 못 찾을 때만 메인 창으로 방을 열려 하고, 그래서 방 창이 안 보이면 "메인 창을
        찾을 수 없다"는 오류가 났다 (2026-09-26 확인: 메인 창을 닫아도 방 창이 있으면 읽힌다).
        카카오톡이 안 떠 있거나 확인하지 못하면 None.
        """
        try:
            from .thread import _AX

            ax = _AX()
            return any(
                ax.attr(w, "AXTitle") == self._room and not ax.attr(w, "AXMinimized")
                for w in ax.windows()
            )
        except Exception:  # noqa: BLE001 — 이 점검 때문에 봇이 멈추면 안 된다
            return None

    def focus_composer(self) -> None:
        """전송 직전 입력창에 포커스를 준다. 앱은 활성화하지 않는다."""
        result = self._osascript(_FOCUS_COMPOSER.format(room=self._room))
        if result == "MINIMIZED":
            raise RoomMinimizedError(
                f"'{self._room}' 창이 최소화되어 있습니다. "
                "최소화를 풀어주세요 (화면 밖 배치는 괜찮습니다)."
            )
        if result != "OK":
            raise KakaoError(f"입력창 포커스 실패: {result}")

    def scrolled_up(self) -> bool:
        """대상 방 창이 맨 아래가 아니다 (사람이 위로 올려 옛 메시지를 보는 중).

        그때 화면에 보이는 것은 '최근 메시지'가 아니다. 서비스는 그 폴링을 건너뛴다.
        확인하지 못하면 False — 이 점검 때문에 봇이 멈추면 안 된다 (겹침 검사가 한 번 더
        막는다). AX 로 스크롤바 값만 읽는다: 실측 54ms, 창을 건드리지 않는다.
        """
        try:
            from .thread import _AX, _message_area

            ax = _AX()
            win = next(
                (w for w in ax.windows() if ax.attr(w, "AXTitle") == self._room), None
            )
            if win is None:
                return False
            _, _, bar = _message_area(ax, win)
            value = ax.attr(bar, "AXValue") if bar is not None else None
            return value is not None and float(value) < 0.999
        except Exception:  # noqa: BLE001
            return False

    def thread_comment_rows(self, count: int) -> list[tuple[str, bool]]:
        """방 창 맨 아래 글 줄 `count` 개의 (본문, 스레드에서 '채팅방에 함께 보내기'로 올린
        댓글인가). 읽기만 한다 (thread.thread_comment_rows, 실측 3줄 0.03초 · 6줄 0.25초)."""
        from .thread import thread_comment_rows

        return thread_comment_rows(self._room, count)

    def read(self, count: int = 20) -> list[Message]:
        """방의 최근 메시지를 읽는다 (패치 003으로 sender/outgoing 포함)."""
        proc = self._run(["ax-read", self._room, "-n", str(count), "--json"])
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            if "not found in visible/loaded chat list" in err:
                raise RoomNotOpenError(f"'{self._room}' 방을 찾을 수 없습니다: {err}")
            raise KakaoError(f"ax-read 실패: {err}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise KakaoError(f"ax-read JSON 파싱 실패: {exc}") from exc
        messages = [
            Message(
                text=m.get("text", ""),
                sender=m.get("sender"),
                outgoing=bool(m.get("outgoing", False)),
                time=m.get("time"),
                label=m.get("sender") or "",
            )
            for m in payload.get("messages", [])
        ]
        return _inherit_senders(messages)

    def reply(
        self,
        target: "Message",
        text: str,
        mode: str = "thread",
        mirror_to_room: bool = True,
        nth: int = 0,
        expect: str | None = None,
    ) -> SendResult:
        """`target` 메시지에 답한다. 기본은 댓글(스레드) 답장이다 (D10 결정).

        스레드 안의 추가 질문에 답할 때는 `target` 이 원글이고, `nth`·`expect` 로
        같은 본문의 원글 중 어느 스레드인지 가린다 (thread.reply_in_thread).

        댓글을 **보내기 전에** 막히면(메시지를 못 찾음, 메뉴가 안 뜸 등) 일반 메시지로
        대신 보낸다. 전송 버튼을 누른 뒤 확인만 못 한 경우에는 대신 보내지 않는다 —
        이미 나갔을 수 있어 중복이 된다.

        지연은 호출하는 쪽(서비스)이 넣는다. 지연 뒤에 긴급 정지를 다시 확인하려고.
        """
        if self._dry_run:
            log.info("[dry-run] %s 답장하지 않음 → %r", mode, text)
            return SendResult(True, "dry-run")

        if self._room not in allowed_send_chats(self._allowlist_path):
            return SendResult(
                False,
                "failed",
                f"'{self._room}' 은 전송 허용 목록에 없습니다 "
                f"({self._allowlist_path} [safety] allowed_send_chats)",
            )

        fallback_reason = ""
        if mode == "thread":
            from .thread import ThreadError, reply_in_thread

            try:
                r = reply_in_thread(
                    self._room,
                    target.text,
                    text,
                    mirror_to_room=mirror_to_room,
                    target_sender=target.sender,
                    nth=nth,
                    expect=expect,
                )
            except ThreadError as exc:
                # 전송 버튼을 누르기 전 단계의 실패만 ThreadError 로 온다
                fallback_reason = f"댓글 불가({exc})"
                log.warning("댓글을 달 수 없어 일반 메시지로 보냅니다: %s", exc)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — 어디서 멈췄는지 모르면 다시 보내지 않는다
                log.exception("댓글 전송 중 예상하지 못한 오류")
                return SendResult(False, "unverified", f"댓글 전송 중 오류: {exc!r}")
            else:
                if r.ok:
                    return SendResult(True, "thread")
                log.error(
                    "댓글 전송을 확인하지 못했습니다 — 중복을 피하려고 다시 보내지 않습니다: %s",
                    r.detail,
                )
                return SendResult(False, "unverified", f"댓글: {r.detail}")

        result = self._send_plain(text)
        if fallback_reason:
            detail = f"{fallback_reason} → 일반 메시지"
            if result.detail:
                detail += f": {result.detail}"
            return SendResult(result.ok, result.via, detail)
        return result

    def send(self, text: str, delay_range: tuple[float, float] | None = None) -> bool:
        """일반 메시지를 보낸다. dry_run 이면 실제로 보내지 않는다. 성공하면 True.

        `delay_range` 가 주어지면 그 범위에서 무작위로 쉬었다가 보낸다
        (계획서 §6-1 [10]). 즉답이 연속되면 자동화 티가 나고, 이는 계획서 §9 의
        계정 리스크와 직결된다.
        """
        if self._dry_run:
            log.info("[dry-run] 전송하지 않음 → %r", text)
            return True

        if delay_range:
            lo, hi = delay_range
            if hi > 0:
                pause = random.uniform(max(0.0, lo), hi)
                log.debug("전송 전 %.1f초 대기", pause)
                time.sleep(pause)
        return self._send_plain(text).ok

    def _send_plain(self, text: str) -> SendResult:
        """local-send 로 일반 메시지를 보낸다. 창 상태와 입력창 포커스를 먼저 맞춘다.

        openkakao-cli 가 보장하는 것 (ax_send.rs):
          - 입력창에 작성 중인 글이 있으면 덮어쓰지 않고 거부한다
          - 클립보드를 쓰지 않는다 (AXValue 로 넣는다)
          - 보낸 뒤 방에 새 말풍선이 생겼는지 확인한다
        """
        if self._dry_run:
            return SendResult(True, "dry-run")
        if not self.window_is_open():
            raise RoomNotOpenError(
                f"'{self._room}' 방이 별도 창으로 열려 있지 않습니다."
            )
        self.focus_composer()

        # --no-prefix: 방에 '🤖 [Sent via openkakao]' 가 붙지 않게 한다 (2026-09-19 결정).
        # 자기 메시지 판별은 말풍선 위치(outgoing)와 SentLog 대조로 한다.
        proc = self._run(["local-send", self._room, text, "-y", "--no-prefix"])
        if proc.returncode == 0 and "Message sent!" in proc.stdout:
            return SendResult(True, "plain")

        err = (proc.stderr.strip() or proc.stdout.strip())[-500:]
        log.error("전송 실패: rc=%s %s", proc.returncode, err)
        if any(k in err for k in _UNCERTAIN_MARKERS):
            return SendResult(False, "unverified", err)
        return SendResult(False, "failed", err)


def _inherit_senders(messages: list[Message]) -> list[Message]:
    """연속 메시지에 앞 발화자의 이름을 물려준다.

    카카오톡은 같은 사람이 연달아 말하면 두 번째부터 이름 라벨을 생략한다.
    그대로 두면 발신자 불명으로 전부 탈락해(fail-closed) 답을 못 한다.
    이름 라벨이 없는 받은 메시지가 대상이다. 시각 라벨은 묶음의 마지막 줄에만 붙으므로
    판단에 쓰지 않는다 (2026-09-20 실측).

    내 메시지나 공지를 만나면 물려주기를 끊는다 — 다음 사람이 말하면 이름 라벨이 다시 나온다.
    창 위쪽이 묶음 가운데에서 잘렸으면 물려줄 이름이 없어 발신자 불명으로 남는다 (fail-closed).
    """
    out: list[Message] = []
    last_sender: str | None = None
    for m in messages:
        if m.outgoing:
            last_sender = None  # 내 메시지 다음엔 상대 이름이 다시 나온다
            out.append(m)
            continue
        if m.is_system_notice:
            last_sender = None
            out.append(m)
            continue
        if m.sender:
            last_sender = m.sender
            out.append(m)
            continue
        # 이름 라벨이 없다 → 직전 발화자가 이어서 보낸 메시지
        out.append(
            Message(
                text=m.text,
                sender=last_sender,
                outgoing=m.outgoing,
                time=m.time,
                label=m.label,  # 이름표는 물려받지 않는다 — 서명이 창에 따라 흔들리지 않게
            )
        )
    return out


# 사진·파일 말풍선 자리에 ax-read 가 넣는 글 (photo.MEDIA 와 같다)
_MEDIA_ROWS = ("[사진]", "[파일]")


def last_seen_index(previous: list[Message], current: list[Message]) -> int | None:
    """직전 화면의 **마지막 글 메시지**가 지금 화면 몇 번째에 있는가. 안 보이면 None.

    직전 화면 끝에서부터 거꾸로 맞춰 가며 **가장 길게 맞는 자리**를 고른다. 같은 문구
    ("확인 후 답변드릴게요.")가 여러 번 있어도 앞뒤 맥락으로 가린다. 길이가 같으면
    앞쪽 자리 — 같은 질문을 반복하면 뒤의 것을 새 메시지로 본다.

    **사진·파일 줄은 맞추는 데 쓰지 않는다** (2026-09-26). 사진 줄은 미리보기를 다시
    그리는 3초가량 목록에서 빠지고, 올라가는 중에는 자리를 옮기기도 한다. 실전에서 직전
    화면 끝이 사진이었는데 그 줄이 뒤로 옮겨졌다가 사라져, 두 번 연속 자리를 못 찾고
    기준선을 다시 잡으면서 그사이 질문 세 개에 답하지 않았다. 글 메시지로만 자리를 찾고,
    그 뒤의 사진·파일 줄은 새것으로 본다 (사진은 질문 쪽에서 photo_hint 로 쓴다).
    """
    prev_rows = [m for m in previous if m.text not in _MEDIA_ROWS]
    cur_rows = [(i, m) for i, m in enumerate(current) if m.text not in _MEDIA_ROWS]
    if not prev_rows:
        # 직전 화면이 사진뿐이었다 — 예전처럼 전부로 맞춘다
        prev_rows = list(previous)
        cur_rows = list(enumerate(current))
    prev = [m.signature() for m in prev_rows]
    cur = [m.signature() for _, m in cur_rows]
    best_k, best_j = 0, None
    for j in range(len(cur)):
        k = 0
        while k < len(prev) and k <= j and cur[j - k] == prev[-1 - k]:
            k += 1
        if k > best_k:
            best_k, best_j = k, j
    return None if best_j is None else cur_rows[best_j][0]


def diff_new(previous: list[Message], current: list[Message]) -> list[Message]:
    """직전 폴링 목록과 비교해 새로 추가된 메시지만 고른다.

    ax-read 는 화면에 보이는 마지막 N개를 시간순으로 준다. 새 메시지는 직전 화면의
    마지막 메시지 **뒤**에 붙는다. 그 자리를 찾아 뒤쪽만 새것으로 본다.

    직전 화면의 마지막 메시지가 **안 보이면 아무것도 새것으로 보지 않는다** (fail-closed).
    예전에는 겹침을 못 찾으면 화면 전체를 새것으로 봤다. 창이 위로 밀린 것만으로 옛
    질문에 다시 답했다 (2026-09-21 15:38 — 같은 질문에 두 번 답하고, 세 번째는 쿨다운에
    걸렸다). 빠뜨리는 쪽이 같은 말을 두 번 하는 쪽보다 낫다.
    """
    if not previous:
        return list(current)
    i = last_seen_index(previous, current)
    return [] if i is None else current[i + 1 :]


def canary_check(client: "KakaoClient") -> tuple[bool, str]:
    """전송 가능 상태인지 조용히 점검한다 (계획서 §6-5 Canary 자가진단).

    실제로 메시지를 보내지 않고 **전송 조건 3가지만** 확인한다. 계획서는 하루 1회
    "나와의 채팅"에 테스트 전송을 제안했지만, Phase 0 에서 "나와의 채팅"이 AX 목록에
    잡히지 않는 것이 확인됐고(개발 초기 실측), 매일 테스트 메시지를
    방에 남기는 것도 바람직하지 않다.

    대신 실패의 실제 원인 3가지를 직접 본다. 조용한 먹통을 잡는다는 목적은 같다.
    """
    if not client.window_is_open():
        return False, f"대상 방 '{client.room}' 이 별도 창으로 열려 있지 않습니다."
    try:
        client.focus_composer()
    except RoomMinimizedError as exc:
        return False, str(exc)
    except KakaoError as exc:
        return False, f"입력창을 사용할 수 없습니다: {exc}"
    try:
        client.read(count=1)
    except KakaoError as exc:
        return False, f"메시지를 읽을 수 없습니다: {exc}"
    return True, "정상"
