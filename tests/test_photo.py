"""질문과 함께 올라온 사진을 읽는 경로 (kakaobot.photo, 2026-09-21).

지켜야 할 것:
  - **질문과 함께 온 사진만** 읽는다. 사진만 올린 메시지, 질문이 아닌 말의 사진은 안 읽는다.
  - 남의 사진을 이 질문에 붙이지 않는다 (같은 사람이 이어 보낸 묶음 안에서만).
  - 어떤 경로로 끝나든 카카오톡 메뉴를 열어 둔 채 끝나지 않는다.
  - 받은 사진 파일은 글자를 읽은 뒤 지운다.
  - 사진 쪽이 실패해도 질문은 글만으로 계속 처리한다.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kakaobot import photo as photo_mod
from kakaobot.kakao import Message
from kakaobot.photo import (
    PHOTO,
    SAVE_ITEM,
    _is_photo,
    _rows,
    nearby_photo,
    read_and_discard,
    save_photo,
    wait_for_new_file,
)
from test_delivery import audit_events, q, svc  # noqa: F401 — svc 는 fixture


def msg(text, sender="Mina", outgoing=False):
    return Message(text, sender, outgoing, None)


# --- 어느 사진인가 ----------------------------------------------------------


class TestNearbyPhoto:
    def test_photo_right_before_the_question(self):
        ms = [msg("안녕하세요", "Kim"), msg(PHOTO), msg("이거 왜 이래요?")]
        assert nearby_photo(ms, 2) == -1  # 바로 앞 줄

    def test_later_photos_do_not_change_the_distance(self):
        """뒤에 남의 사진이 더 올라와도 질문과의 거리는 그대로다 (예전엔 순번이 밀렸다)."""
        ms = [msg(PHOTO), msg("이거 왜 이래요?"), msg(PHOTO, "Kim")]
        assert nearby_photo(ms, 1) == -1

    def test_photo_right_after_the_question(self):
        ms = [msg("이거 왜 이래요?"), msg(PHOTO)]
        assert nearby_photo(ms, 0) == 1  # 바로 뒤 줄

    def test_prefers_the_photo_before(self):
        ms = [msg(PHOTO), msg("이거 왜 이래요?"), msg(PHOTO)]
        assert nearby_photo(ms, 1) == -1  # 앞 사진

    def test_someone_elses_photo_is_never_borrowed(self):
        ms = [msg(PHOTO, "Kim"), msg("이거 왜 이래요?")]
        assert nearby_photo(ms, 1) is None

    def test_burst_ends_at_another_speaker(self):
        ms = [msg(PHOTO), msg("잠깐만요", "Kim"), msg("이거 왜 이래요?")]
        assert nearby_photo(ms, 2) is None

    def test_burst_ends_at_my_own_message(self):
        ms = [
            msg(PHOTO),
            msg("확인해 볼게요", None, outgoing=True),
            msg("이거 왜 이래요?"),
        ]
        assert nearby_photo(ms, 2) is None

    def test_burst_ends_at_a_notice(self):
        ms = [msg(PHOTO), msg("Kim님이 들어왔습니다.", None), msg("이거 왜 이래요?")]
        assert nearby_photo(ms, 2) is None

    def test_unknown_sender_gets_nothing(self):
        ms = [msg(PHOTO, None), msg("이거 왜 이래요?", None)]
        assert nearby_photo(ms, 1) is None

    def test_too_far_back_is_not_the_same_question(self):
        ms = (
            [msg(PHOTO)] + [msg(f"말 {i}") for i in range(4)] + [msg("이거 왜 이래요?")]
        )
        assert nearby_photo(ms, 5) is None


# --- 파일이 다 쓰일 때까지 -----------------------------------------------------


class Clock:
    """가짜 시계. sleep 할 때 예약해 둔 일(파일 생성·커짐)을 일으킨다."""

    def __init__(self, events=()):
        self.t = 0.0
        self.events = sorted(events, key=lambda e: e[0])

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        while self.events and self.events[0][0] <= self.t:
            self.events.pop(0)[1]()


def test_waits_until_the_file_stops_growing(tmp_path):
    f = tmp_path / "KakaoTalk_Photo_2026-09-21-14-48-09.png"
    clock = Clock(
        [
            (0.3, lambda: f.write_bytes(b"x" * 10)),
            (0.5, lambda: f.write_bytes(b"x" * 20)),
        ]
    )
    got = wait_for_new_file(
        tmp_path, set(), timeout=5, clock=clock.now, sleep=clock.sleep
    )
    assert got == f and f.stat().st_size == 20  # 다 쓰인 뒤에만 돌려준다


def test_old_files_and_non_images_are_not_ours(tmp_path):
    old = tmp_path / "KakaoTalk_Photo_2026-07-09-18-22-15.png"
    old.write_bytes(b"old")
    zipped = tmp_path / "KakaoTalk_Photo_묶음.zip"
    clock = Clock([(0.2, lambda: zipped.write_bytes(b"zip"))])
    got = wait_for_new_file(
        tmp_path, {old}, timeout=1, clock=clock.now, sleep=clock.sleep
    )
    assert got is None


# --- 접근성 트리 흉내 --------------------------------------------------------


def pos(y):
    return f"<AXValue> {{value = x:100.000000 y:{y:.6f} type = kAXValueCGPointType}}"


def size(w, h):
    return f"<AXValue> {{value = w:{w:.6f} h:{h:.6f} type = kAXValueCGSizeType}}"


def el(role, children=(), y=None, w=200.0, h=40.0, **extra):
    e = {"AXRole": role, "AXChildren": list(children)}
    if y is not None:
        e["AXPosition"], e["AXSize"] = pos(y), size(w, h)
    e.update(extra)
    return e


def text_cell(y, text, profile=False):
    kids = [el("AXTextArea", AXValue=text)]
    if profile:  # 묶음 첫 줄: 프로필 사진(34×34) + 이름 — 그래도 글 칸이다
        kids = [
            el("AXImage", y=y, w=34, h=34),
            el("AXStaticText", AXValue="Mina"),
        ] + kids
    return el("AXCell", kids, y=y, h=40, id=f"text@{y}")


def photo_cell(y, w=212.0, h=102.0):
    return el(
        "AXCell",
        [
            el("AXImage", y=y, w=w, h=h),
            el("AXStaticText", AXHelp="2026. 9. 21.", AXValue="14:48"),
        ],
        y=y,
        h=h + 10,
        id=f"photo@{y}",
    )


class FakeAX:
    """photo.save_photo 와 thread 의 도우미가 쓰는 만큼만."""

    def __init__(self, cells, menu_items=(SAVE_ITEM, "댓글"), on_save=None):
        rows = [el("AXRow", [c]) for c in cells]
        self.table = el("AXTable", rows, y=450, h=432)
        bar = el("AXScrollBar", AXOrientation="AXVerticalOrientation", AXValue=1.0)
        area = el("AXScrollArea", [self.table, bar], y=450, h=432)
        self.win = el("AXWindow", [area], y=362, h=640, AXTitle="연습방")
        self.menu_items = menu_items
        self.on_save = on_save
        self.actions = []

    def windows(self):
        return [self.win]

    def attr(self, e, name):
        return e.get(name) if isinstance(e, dict) else None

    def walk(self, e, depth=0, max_depth=14):
        yield e
        if depth < max_depth:
            for child in e.get("AXChildren", []):
                yield from self.walk(child, depth + 1, max_depth)

    def set_value(self, e, value):
        e["AXValue"] = value
        return 0

    def open_menu(self):
        return next(
            (c for c in self.win["AXChildren"] if c["AXRole"] == "AXMenu"), None
        )

    def _close_menu(self):
        self.win["AXChildren"] = [
            c for c in self.win["AXChildren"] if c["AXRole"] != "AXMenu"
        ]

    def perform(self, e, action):
        self.actions.append((e.get("id") or e.get("AXTitle") or e["AXRole"], action))
        if action == "AXShowMenu":
            items = [el("AXMenuItem", AXTitle=t) for t in self.menu_items]
            self.win["AXChildren"].append(el("AXMenu", items))
        elif action == "AXPick":
            self._close_menu()
            if e.get("AXTitle") == SAVE_ITEM and self.on_save:
                self.on_save()
        elif action == "AXCancel":
            self._close_menu()
        return 0


def saver(tmp_path, name="KakaoTalk_Photo_2026-09-21-14-48-09.png"):
    def on_save():
        (tmp_path / name).write_bytes(b"\x89PNG" + b"x" * 100)

    return on_save


def test_photo_cell_rules():
    """글 칸은 프로필 사진이 있어도 사진 칸이 아니다. 작은 그림(이모티콘)도 아니다."""
    ax = FakeAX([])
    assert not _is_photo(ax, text_cell(460, "안녕", profile=True))
    assert _is_photo(ax, photo_cell(510))
    assert not _is_photo(ax, photo_cell(510, w=40, h=40))


def test_saves_the_photo_next_to_the_question_and_leaves_no_menu(tmp_path):
    ax = FakeAX(
        [photo_cell(460), text_cell(580, "이거 왜 이래요?"), photo_cell(630)],
        on_save=saver(tmp_path),
    )
    got = save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=2, ax=ax)
    assert got is not None and got.name.startswith("KakaoTalk_Photo_")
    assert ("photo@460", "AXShowMenu") in ax.actions  # 질문 바로 앞 줄
    assert ("photo@630", "AXShowMenu") not in ax.actions
    assert ax.open_menu() is None


def test_a_flickering_photo_elsewhere_does_not_change_the_choice(tmp_path):
    """2026-09-21 15:49 실측: 사진 줄이 저절로 3초간 비었다. '뒤에서 몇 번째'로 세면
    그 순간 남의 사진을 고른다. 질문 기준으로 찾으면 흔들리지 않는다."""
    flickering = photo_cell(700)
    flickering["AXChildren"] = [el("AXStaticText", AXValue="")]  # 그림을 다시 그리는 중
    ax = FakeAX(
        [photo_cell(460), text_cell(580, "이거 왜 이래요?"), flickering],
        on_save=saver(tmp_path),
    )
    assert save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=2, ax=ax)
    assert ("photo@460", "AXShowMenu") in ax.actions


def test_waits_for_the_target_photo_to_come_back(tmp_path):
    """고를 사진 자체가 깜빡이는 중이면 잠깐 기다렸다 다시 본다."""
    target = photo_cell(460)
    image = target["AXChildren"]
    target["AXChildren"] = []  # 처음엔 비어 있다
    ax = FakeAX([target, text_cell(580, "이거 왜 이래요?")], on_save=saver(tmp_path))
    real_sleep = photo_mod.time.sleep

    def sleep(dt):
        target["AXChildren"] = image  # 기다리는 사이 돌아온다
        real_sleep(0)

    photo_mod.time.sleep, restore = sleep, photo_mod.time.sleep
    try:
        got = save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=2, ax=ax, wait=0)
    finally:
        photo_mod.time.sleep = restore
    assert got is not None


def test_already_saved_photo_is_skipped_and_the_menu_closed(tmp_path):
    """한 번 저장한 사진은 '저장하기'가 '폴더열기'로 바뀐다 (2026-09-21 실측)."""
    ax = FakeAX(
        [photo_cell(460), text_cell(580, "이거 왜 이래요?")],
        menu_items=("폴더열기", "댓글"),
        on_save=saver(tmp_path),
    )
    assert save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=1, ax=ax) is None
    assert ax.open_menu() is None  # 2026-09-21 사고: 이 경로에서 메뉴가 1분 열려 있었다
    assert list(tmp_path.iterdir()) == []


def test_small_pictures_are_not_opened(tmp_path):
    """이모티콘처럼 작은 그림은 메뉴도 열지 않는다."""
    ax = FakeAX(
        [photo_cell(460, w=40, h=40), text_cell(580, "이거 왜 이래요?")],
        on_save=saver(tmp_path),
    )
    assert save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=1, ax=ax, wait=0) is None
    assert all(action != "AXShowMenu" for _, action in ax.actions)


def test_missing_question_on_screen_gives_up_quietly(tmp_path):
    ax = FakeAX([photo_cell(460), text_cell(580, "다른 말")])
    assert save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=1, ax=ax, wait=0) is None
    assert ax.actions == []


def test_no_file_in_time_is_none_and_no_menu_left(tmp_path):
    ax = FakeAX([photo_cell(460), text_cell(580, "이거 왜 이래요?")], on_save=None)
    assert save_photo("연습방", "이거 왜 이래요?", -1, tmp_path, timeout=0.5, ax=ax) is None
    assert ax.open_menu() is None


def test_closed_room_window_is_an_error(tmp_path):
    ax = FakeAX([photo_cell(460)])
    with pytest.raises(photo_mod.PhotoError):
        save_photo("다른방", "이거 왜 이래요?", -1, tmp_path, timeout=0.5, ax=ax)


# --- 읽고 지운다 --------------------------------------------------------------


def test_file_is_deleted_after_reading(tmp_path, monkeypatch):
    import kakaobot.ocr as ocr

    f = tmp_path / "KakaoTalk_Photo_x.png"
    f.write_bytes(b"png")
    monkeypatch.setattr(
        ocr,
        "read_image",
        lambda p: ocr.OcrResult(text="가" * 1500, lines=1, confidence=0.9),
    )
    text = read_and_discard(f, max_chars=1000)
    assert len(text) == 1000
    assert not f.exists()  # 수강생 화면이 운영자 맥에 남지 않는다


def test_file_is_deleted_even_when_reading_fails(tmp_path, monkeypatch):
    import kakaobot.ocr as ocr

    f = tmp_path / "KakaoTalk_Photo_x.png"
    f.write_bytes(b"png")

    def boom(p):
        raise RuntimeError("Vision 실패")

    monkeypatch.setattr(ocr, "read_image", boom)
    assert read_and_discard(f) == ""
    assert not f.exists()


# --- 검색: 질문과 화면을 따로 찾고 더 잘 맞은 쪽 --------------------------------


def test_search_many_takes_the_best_query_per_chunk():
    from test_ocr import retriever_over

    from kakaobot.rag import Chunk

    a = Chunk(doc="d", heading="A", text="홈브루 설치")
    b = Chunk(doc="d", heading="B", text="수강료")
    table = {
        a.views[0]: [1.0, 0.0],
        b.views[0]: [0.0, 1.0],
        "이거 왜 이래요?": [0.1, 0.2],  # 글만으로는 어느 쪽도 아니다
        "Install Homebrew": [0.9, 0.0],  # 화면 글자는 A 를 가리킨다
    }
    r = retriever_over([a, b], table)
    alone = r.search("이거 왜 이래요?", top_k=1)[0]
    both = r.search_many(["이거 왜 이래요?", "Install Homebrew"], top_k=2)
    assert alone.score == pytest.approx(0.2)
    assert [h.chunk.heading for h in both] == ["A", "B"]
    assert both[0].score == pytest.approx(0.9)


def test_screen_text_goes_to_search_and_to_the_model_but_not_as_grounds():
    from kakaobot.answering import SCREEN_LABEL, Answerer
    from kakaobot.guard import OutputGuard
    from kakaobot.llm import Answer, LLMAdapter
    from kakaobot.rag import Chunk, Grounding, Hit

    class KB:
        def ground(self, question, extra_queries=()):
            self.extra = tuple(extra_queries)
            chunk = Chunk(
                doc="설치",
                heading="홈브루 설치 화면이 떴어요",
                text="명령어를 붙여넣으세요.",
            )
            return Grounding(hits=[Hit(chunk=chunk, score=0.8)], threshold=0.58)

    class LLM(LLMAdapter):
        def complete(self, system, user):
            self.user = user
            return Answer(
                text="터미널에 붙여넣고 엔터를 누르세요.",
                model="m",
                mode="t",
                latency_ms=1,
            )

    kb, llm = KB(), LLM()
    o = Answerer(kb, llm, OutputGuard(max_chars=500)).answer(
        "이거 왜 이래요?", screen_text="Install Homebrew"
    )
    assert kb.extra == ("Install Homebrew",)  # 검색에 따로 넣었다
    assert f"{SCREEN_LABEL}\nInstall Homebrew" in llm.user  # 모델도 본다
    grounds = llm.user.split("<근거>")[1].split("</근거>")[0]
    assert "Install Homebrew" not in grounds  # 근거 구역에는 없다
    assert o.screen_text == "Install Homebrew"

    kb2, llm2 = KB(), LLM()
    Answerer(kb2, llm2, OutputGuard(max_chars=500)).answer("수강료 얼마예요?")
    assert kb2.extra == () and SCREEN_LABEL not in llm2.user


# --- 서비스 ---------------------------------------------------------------------


class ScreenKB:
    """글만으로는 근거가 없고, 화면 글자가 함께 오면 근거가 있다."""

    def ground(self, question, extra_queries=()):
        return type("G", (), {"best_score": 0.81 if extra_queries else 0.30})()


def filtered_events(svc):
    from kakaobot.logging_setup import AuditLog, KIND_FILTERED

    path = AuditLog(svc._root / "logs", KIND_FILTERED).path_for()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_photo_alone_is_not_a_question(svc):
    svc._photo_enabled = True
    svc._read_photo = lambda m: pytest.fail("사진만 올린 메시지는 읽지 않는다")
    svc._handle(msg(PHOTO))
    assert svc._answerer.calls == 0
    assert filtered_events(svc)[-1]["stage"] == "media"


def test_question_with_a_photo_is_answered_with_its_screen(svc):
    svc._photo_enabled = True
    svc._read_photo = lambda m: ("zsh: command not found: python", "사진 글자 30자")
    svc._handle(q("이거 왜 이래요?"), photo_hint=True)
    assert svc._answerer.screen_text == "zsh: command not found: python"
    event = audit_events(svc)[-1]
    assert event["photo"] == "사진 글자 30자"
    assert event["screen_text"].startswith("zsh: command not found")


def test_photo_is_not_read_when_the_feature_is_off(svc):
    svc._photo_enabled = False
    svc._read_photo = lambda m: pytest.fail("꺼져 있으면 읽지 않는다")
    svc._handle(q("이거 왜 이래요?"), photo_hint=True)
    assert svc._answerer.screen_text == ""


def test_question_without_mark_is_rescued_by_its_photo(svc):
    """'이거 왜 이래요' — 어미는 질문인데 글만으로는 근거가 없다. 사진 글자로 다시 본다."""
    svc._photo_enabled = True
    svc._ensure_kb = lambda: ScreenKB()
    reads = []

    def read(m):
        reads.append(m.text)
        return "zsh: command not found", "사진 글자 22자"

    svc._read_photo = read
    svc._handle(msg("이거 왜 이래요"), photo_hint=True)
    assert reads == ["이거 왜 이래요"]  # 한 번만 읽는다 (답변 단계에서 또 읽지 않는다)
    assert svc._answerer.screen_text == "zsh: command not found"
    assert audit_events(svc)[-1]["event"] == "answer"


def test_a_statement_with_a_photo_stays_silent_and_unread(svc):
    """'완성했어요' + 사진 — 질문이 아니므로 사진도 안 읽는다 (결과 자랑)."""
    svc._photo_enabled = True
    svc._ensure_kb = lambda: ScreenKB()
    svc._read_photo = lambda m: pytest.fail("질문이 아닌 말의 사진은 읽지 않는다")
    svc._handle(msg("완성했어요"), photo_hint=True)
    assert svc._answerer.calls == 0


def test_poll_marks_only_questions_that_came_with_a_photo(svc):
    svc._photo_enabled = True
    seen = []
    svc._handle = lambda m, photo_hint=False: seen.append((m.text, photo_hint))
    base = [msg("안녕하세요", "Kim")]
    svc._kakao.screens = [
        base,
        base + [msg(PHOTO), msg("이거 왜 이래요?"), msg("수강료 얼마예요?", "Kim")],
    ]
    svc._poll_once()
    svc._poll_once()
    assert seen == [
        (PHOTO, False),
        ("이거 왜 이래요?", True),
        ("수강료 얼마예요?", False),  # Kim 은 사진을 올리지 않았다
    ]


def test_read_photo_rereads_the_screen_to_pick_the_right_one(
    svc, monkeypatch, tmp_path
):
    """폴링 뒤에 다른 사람이 사진을 더 올렸으면 순번이 밀린다 — 화면을 다시 읽어 정한다."""
    import kakaobot.service as service_mod

    svc._photo_dir = tmp_path
    question = msg("이거 왜 이래요?")
    svc._kakao.screens = [[msg(PHOTO), question, msg(PHOTO, "Kim")]]
    picked = []

    def fake_save(room, question_text, offset, save_dir, **kw):
        picked.append((question_text, offset))
        f = tmp_path / "KakaoTalk_Photo_x.png"
        f.write_bytes(b"png")
        return f

    monkeypatch.setattr(service_mod, "save_photo", fake_save)
    monkeypatch.setattr(service_mod, "read_and_discard", lambda p, **kw: "화면 글자")
    assert svc._read_photo(question) == ("화면 글자", "사진 글자 5자")
    assert picked == [("이거 왜 이래요?", -1)]  # Kim 의 사진이 아니라 질문 바로 앞 Mina 의 사진


def test_read_photo_never_raises(svc, monkeypatch, tmp_path):
    import kakaobot.service as service_mod

    svc._photo_dir = tmp_path
    question = msg("이거 왜 이래요?")

    def boom(*a, **k):
        raise RuntimeError("AX 오류")

    monkeypatch.setattr(service_mod, "save_photo", boom)
    svc._kakao.screens = [[msg(PHOTO), question]]
    assert svc._read_photo(question) == ("", "사진 저장 실패: AX 오류")

    svc._kakao.screens = [[msg("다른 말", "Kim")]]
    assert svc._read_photo(question) == ("", "질문이 화면에서 사라짐")

    svc._photo_dir = tmp_path / "없는폴더"
    assert svc._read_photo(question)[0] == ""


def test_a_photo_question_is_not_rewritten_from_chat_history():
    """2026-09-21 실전: '이거 왜이래요??' 를 대화 이력으로 풀어 '앞에서 물던 질문들(…)의
    답변이 왜 이래요?' 가 됐다. 사진이 함께 오면 '이거'는 화면이다 — 다시 쓰지 않는다."""
    from kakaobot.answering import Answerer
    from kakaobot.guard import OutputGuard
    from kakaobot.llm import Answer, LLMAdapter
    from kakaobot.rag import Chunk, Grounding, Hit

    class KB:
        def ground(self, question, extra_queries=()):
            self.question = question
            chunk = Chunk(doc="설치", heading="홈브루 설치 화면이 떴어요", text="붙여넣으세요.")
            return Grounding(hits=[Hit(chunk=chunk, score=0.9)], threshold=0.58)

    class LLM(LLMAdapter):
        calls = 0

        def complete(self, system, user):
            LLM.calls += 1
            return Answer(text="터미널에 붙여넣으세요.", model="m", mode="t", latency_ms=1)

    kb = KB()
    Answerer(kb, LLM(), OutputGuard(max_chars=500)).answer(
        "이거 왜 이래요?", history="Mina: 몇살이세요\nMina: 얼마에요", screen_text="Install Homebrew"
    )
    assert kb.question == "이거 왜 이래요?"
    assert LLM.calls == 1  # 답변 한 번뿐 — 재구성 호출이 없다
