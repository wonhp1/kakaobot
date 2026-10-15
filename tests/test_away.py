"""방 창이 화면에 없을 때 (2026-09-24, 2026-09-26).

접근성 API 는 지금 보이는 데스크탑의 창만 돌려준다. 예전에는 사람이 다른 데스크탑으로
가거나 방 창을 닫으면 읽기가 3번 실패해 봇이 멈췄다 (2026-09-24 00:54, 12:12). 지금은
기다렸다가, 돌아오면 그사이 올라온 질문에 **늦게라도 답한다** (운영자 결정).
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kakaobot import service as service_mod
from kakaobot.followup import Notice
from kakaobot.kakao import Message
from kakaobot.service import AWAY_NOTIFY_SECS, CATCH_UP_COUNT, CATCH_UP_ROWS

# 스레드 추가 질문 시험의 가짜 카카오톡·설정을 그대로 쓴다
from test_followup import BOT, PARENT, ROOM, FakeKakao, post, svc, tm  # noqa: F401


class AwayKakao(FakeKakao):
    """창이 보이는지를 차례로 돌려주고, 몇 개씩 읽었는지 적어 둔다.

    visible: 기동할 때 카카오톡 창이 하나라도 보이는지 (windows_visible)
    room:    돌면서 방 창이 보이는지 (room_visible)
    """

    def __init__(self, visible=(), room=(), screen=()):
        super().__init__()
        self.visible_seq = list(visible)
        self.room_seq = list(room)
        self.screen = list(screen)
        self.reads = []

    def windows_visible(self):
        return self.visible_seq.pop(0) if self.visible_seq else True

    def room_visible(self):
        return self.room_seq.pop(0) if self.room_seq else True

    def window_is_open(self):
        return True

    def read(self, count=20):
        self.reads.append(count)
        return list(self.screen)


def q(text, sender):
    return Message(text, sender, False, None)


def use(svc, fake, monkeypatch):
    svc._kakao = fake
    monkeypatch.setattr("kakaobot.service.KakaoClient", lambda *a, **k: fake)
    answerer = svc._answerer
    svc._ensure_answerer = lambda: answerer
    return fake


def test_away_waits_without_tripping_and_answers_late_on_return(
    svc, monkeypatch, caplog
):
    base = q("안녕하세요", "a")
    late = [q("수강료가 얼마예요?", "Kim"), q("환불은 언제까지 되나요?", "Lee")]
    # 기동 때 보임 → 두 번 폴링 동안 방 창이 안 보임(닫힘·다른 데스크탑) → 돌아옴
    fake = use(svc, AwayKakao(room=[False, False], screen=[base]), monkeypatch)
    steps = []

    def fake_sleep(_):
        steps.append(1)
        if len(steps) == 1:
            fake.screen = [base, *late]  # 자리를 비운 사이 질문 두 개

    monkeypatch.setattr("kakaobot.service.time.sleep", fake_sleep)
    with caplog.at_level("INFO", logger="kakaobot.service"):
        svc.run(interval=0, max_iterations=5)

    assert not svc._breaker.tripped and svc._stats.errors == 0
    # 기동 20개 → (기다리는 동안 읽지 않음) → 돌아와서 넓게 60개 → 평소 20개
    assert fake.reads == [20, CATCH_UP_COUNT, 20, 20]
    assert [r["target"] for r in fake.replies] == [m.text for m in late]
    logs = [r.getMessage() for r in caplog.records]
    assert sum("방 창이 지금 화면에 없어" in m for m in logs) == 1
    assert any("다시 보입니다" in m for m in logs)


def test_startup_waits_until_the_windows_are_visible(svc, monkeypatch, caplog):
    """메뉴바는 모든 데스크탑에 보인다 — 다른 데스크탑에서 '봇 시작'을 누를 수 있다."""
    fake = use(
        svc,
        AwayKakao(visible=[False, False, True], screen=[q("안녕하세요", "a")]),
        monkeypatch,
    )
    monkeypatch.setattr("kakaobot.service.time.sleep", lambda _: None)
    with caplog.at_level("WARNING", logger="kakaobot.service"):
        svc.run(interval=0, max_iterations=1)
    assert fake.reads[0] == 20  # 보이고 나서 기준선
    assert sum("보이면 시작합니다" in r.getMessage() for r in caplog.records) == 1


def test_kakao_not_running_is_a_read_failure_not_away(svc):
    """카카오톡 자체가 꺼졌으면 기다리지 않는다 — 평소처럼 읽기 실패로 세어 멈춘다."""
    svc._kakao.room_visible = lambda: None
    assert svc._check_away() is False
    assert svc._away_since is None


def test_long_away_is_reported_once(svc):
    sent = []
    svc._notifier.send = sent.append
    svc._kakao.visible = False
    assert svc._check_away()
    svc._away_since = time.monotonic() - AWAY_NOTIFY_SECS - 1
    assert svc._check_away() and svc._check_away()
    assert len(sent) == 1 and "방 창" in sent[0]
    svc._kakao.visible = True
    assert svc._check_away() is False
    assert svc._catch_up and svc._followup_catch_up


# --- 스레드 추가 질문 ---------------------------------------------------------


def notice_feed(svc, monkeypatch):
    """알림 목록을 top 줄까지만 돌려주고, 몇 줄씩 읽었는지 적어 둔다."""
    tops = []

    def fake_read_notices(room, top=5):
        tops.append(top)
        return svc.fake["notices"][:top]

    monkeypatch.setattr(service_mod, "read_notices", fake_read_notices)
    return tops


def test_followup_posted_while_away_is_answered_on_return(svc, monkeypatch):
    tops = notice_feed(svc, monkeypatch)
    svc.fake["notices"] = [Notice(ROOM, PARENT, "화면에 보이는 명령어를…", False)]
    svc._poll_followups()  # 기준선 — 넓게 읽는다
    assert tops == [CATCH_UP_ROWS]

    svc._kakao.visible = False
    assert svc._check_away()  # 다른 데스크탑으로
    post(
        svc,
        "그럼 윈도우는요?",
        [tm(PARENT), tm(BOT, outgoing=True), tm("그럼 윈도우는요?")],
    )
    svc._kakao.visible = True
    assert not svc._check_away()  # 돌아옴

    svc._followup_every, svc._followup_tick = 2, 0  # 평소라면 이번 폴링은 건너뛸 차례
    svc._poll_followups()
    assert tops[-1] == CATCH_UP_ROWS  # 돌아온 뒤 첫 읽기는 넓게, 곧바로
    assert [r["expect"] for r in svc._kakao.replies] == ["그럼 윈도우는요?"]
    svc._poll_followups()
    svc._poll_followups()
    assert tops[-1] == svc._followup_top  # 그다음은 평소대로


def test_wide_read_after_return_does_not_answer_old_comments(svc, monkeypatch):
    """평소엔 맨 위 5줄만 본다. 돌아와서 20줄을 볼 때, 6~20번째 줄이 처음 보는 줄이면
    옛 댓글에 뒤늦게 답하게 된다 — 기준선을 넓게 잡아 두어 막는다."""
    notice_feed(svc, monkeypatch)
    old = [Notice(ROOM, f"옛 원글 {k}", f"옛 댓글 {k}", False) for k in range(12)]
    svc.fake["notices"] = old
    svc._poll_followups()  # 기준선 (20줄)
    svc._kakao.visible = False
    svc._check_away()
    svc._kakao.visible = True
    svc._check_away()
    svc._poll_followups()  # 돌아와서 20줄 — 새 알림 없음
    assert svc.fake["opened"] == []
    assert svc._kakao.replies == []


# --- 방 창이 보이는가 (실제 KakaoClient) ----------------------------------------


class WindowsAX:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return self._windows

    def attr(self, e, name):
        return e.get(name)


def test_room_visible_checks_only_the_room_window(monkeypatch):
    """메인 창은 없어도 된다 — 방 창이 최소화되지 않고 보이면 된다 (2026-09-26 확인)."""
    from kakaobot import thread as thread_mod
    from kakaobot.kakao import KakaoClient

    c = KakaoClient(Path(sys.executable), ROOM, dry_run=True)
    cases = [
        ([{"AXTitle": ROOM}], True),                                    # 메인 창 없이 방 창만
        ([{"AXTitle": "댓글 알림"}, {"AXIdentifier": "Main Window"}], False),   # 방 창 닫힘
        ([{"AXTitle": ROOM, "AXMinimized": True}], False),             # 최소화
        ([], False),                                                    # 다른 데스크탑
    ]
    for windows, want in cases:
        monkeypatch.setattr(thread_mod, "_AX", lambda w=windows: WindowsAX(w))
        assert c.room_visible() is want, windows

    def not_running():
        raise thread_mod.ThreadError("카카오톡이 실행 중이 아닙니다")

    monkeypatch.setattr(thread_mod, "_AX", not_running)
    assert c.room_visible() is None



# --- 1분 간격 (2026-09-26 운영자 결정) --------------------------------------------


def test_long_interval_reads_wide_and_rebaselines_after_two_misses(svc):
    """1분 사이에 20개 넘게 올라와도 놓치지 않게 매번 넓게 읽는다. 직전 화면을 못 찾으면
    3초 간격일 때처럼 30초(10번)가 아니라 두 번(2분) 만에 기준선을 다시 잡는다."""
    base = q("안녕하세요", "a")
    fake = AwayKakao(screen=[base])
    svc._kakao = fake
    svc._interval = 60
    svc._poll_once()  # 기준선
    assert fake.reads == [CATCH_UP_COUNT]
    fake.screen = [q("전혀 다른 화면", "b")]
    svc._poll_once()
    assert svc._last_seen == [base]  # 한 번은 기준선을 지킨다
    svc._poll_once()
    assert svc._last_seen == fake.screen


def test_sleep_wakes_up_on_stop_and_keeps_the_heartbeat(svc, monkeypatch):
    """1분을 쉬는 동안에도 멈춤 신호에 곧바로 서고, 10초마다 상태를 적는다."""
    clock = {"t": 0.0}
    beats = []

    def fake_sleep(dt):
        clock["t"] += dt
        if clock["t"] >= 25:
            svc._stop = True

    monkeypatch.setattr(service_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(service_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(svc, "_write_status", lambda: beats.append(clock["t"]))
    svc._sleep(60)
    assert 25 <= clock["t"] < 27
    assert beats == [10, 20]


def test_interval_comes_from_config(svc):
    import os

    text = svc.cfg_path.read_text(encoding="utf-8") + "\n[poll]\ninterval_secs = 60\n"
    svc.cfg_path.write_text(text, encoding="utf-8")
    st = svc.cfg_path.stat()
    os.utime(svc.cfg_path, (st.st_atime, st.st_mtime + 5))
    svc._reload_config()
    assert svc._poll_interval == 60
