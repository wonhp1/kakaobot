"""대화 기록 → 지식베이스 초안. 실전에서 실제로 틀렸던 것만 고정한다.

2026-09-20 실제 내보내기(3,009개 메시지)를 돌려 드러난 결함:
  - 사진 메시지가 "사진" 한 단어라, 이어진 질문에 붙어 "사진 이곳 비번은 뭔가요?" 가 됐다
  - LLM 이 "## SKIP" 이나 인사말을 얹어 그대로 조각이 됐다
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load():
    """스크립트를 모듈로 불러온다 (dataclass 가 sys.modules 를 찾으므로 먼저 등록)."""
    spec = importlib.util.spec_from_file_location(
        "build_knowledge", ROOT / "scripts" / "build_knowledge.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_knowledge"] = module
    spec.loader.exec_module(module)
    return module


bk = load()
AT = datetime(2026, 9, 20, 10, 0)


def msgs(*items):
    """(초, 누구, 본문) → Msg 목록."""
    return [bk.Msg(AT + timedelta(seconds=s), who, text) for s, who, text in items]


def test_photo_does_not_glue_itself_onto_the_question():
    """실전 오류: '사진' + '이곳 비번은 뭔가요?' → '사진 이곳 비번은 뭔가요?'"""
    (merged,) = bk.merge_bursts(
        msgs((0, "학생", "사진"), (5, "학생", "이곳 비번은 뭔가요?"))
    )
    assert merged.text == "이곳 비번은 뭔가요?"
    assert merged.has_photo is True


def test_a_photo_alone_survives_as_a_marker_not_as_text():
    (only,) = bk.merge_bursts(msgs((0, "학생", "사진")))
    assert only.text == "" and only.has_photo is True


def test_photos_do_not_leak_across_speakers_or_the_gap():
    a, b = bk.merge_bursts(msgs((0, "학생", "사진"), (5, "다른사람", "안녕하세요")))
    assert a.has_photo and not b.has_photo

    a, b = bk.merge_bursts(msgs((0, "학생", "사진"), (300, "학생", "질문 있어요")))
    assert a.has_photo and not b.has_photo  # 5분 뒤면 다른 발화다


def test_a_question_that_came_with_a_photo_is_routed_to_the_screen_pile():
    """지시어가 없어도, 사진이 실제로 있었으면 화면이 필요한 질문이다."""
    assert bk.needs_screen("설치가 안 되는데 봐주실 수 있나요", had_photo=True)
    assert not bk.needs_screen("설치가 안 되는데 봐주실 수 있나요", had_photo=False)
    assert not bk.needs_screen("넵", had_photo=True)  # 너무 짧다


def test_deictic_questions_are_routed_even_without_a_photo():
    """내보내기에서 사진이 안 잡히는 경우도 있다 (여러 줄 메시지 등)."""
    assert bk.needs_screen("이거 맞나요?")
    assert bk.needs_screen("여기 이렇게 하면 되나요")
    assert not bk.needs_screen("환불 규정이 어떻게 되나요")


def test_pair_qa_carries_the_photo_flag_through():
    messages = bk.merge_bursts(
        msgs(
            (0, "학생", "사진"),
            (5, "학생", "이거 왜 이래요?"),
            (
                30,
                "운영자",
                "터미널을 껐다 켜고 다시 해보세요. 경로가 아직 안 잡혀서 그렇습니다.",
            ),
        )
    )
    (pair,) = bk.pair_qa(messages, "운영자", window_min=10, min_answer=10)
    assert pair.question == "이거 왜 이래요?"
    assert pair.has_photo is True


class TestTidyDraft:
    """LLM 출력에서 군더더기를 걷어낸다. 형식을 어기면 고쳐 쓰지 않고 버린다."""

    def test_clean_output_passes_through(self):
        text = "## 환불 되나요 (→ 불가)\n수강 시작 뒤에는 환불이 어렵습니다."
        assert bk.tidy_draft(text) == text

    def test_preamble_is_stripped(self):
        got = bk.tidy_draft(
            "대량의 실제 Q&A 기록 정리 요청이네요. 체계적으로 진행하겠습니다.\n\n"
            "## 환불 되나요\n수강 시작 뒤에는 어렵습니다."
        )
        assert got.startswith("## 환불 되나요")
        assert "체계적으로" not in got

    def test_skip_is_recognised_however_it_is_dressed(self):
        for text in ("SKIP", "## SKIP", "  skip  ", "## SKIP\n무언가 덧붙임"):
            assert bk.tidy_draft(text) == "SKIP", text

    def test_output_without_a_heading_is_discarded(self):
        assert bk.tidy_draft("안녕하세요. 시스템이 로드된 상태입니다.") == ""
        assert bk.tidy_draft("") == ""

    def test_a_heading_with_no_answer_is_discarded(self):
        assert bk.tidy_draft("## 환불 되나요") == ""


def test_slug_makes_a_recognisable_image_name():
    assert (
        bk.slug("## 터미널에서 명령어가 먹히지 않을 때")
        == "터미널에서-명령어가-먹히지-않을-때"
    )
    assert bk.slug("## 환불 되나요 (→ 불가)") == "환불-되나요-불가"
    assert bk.slug("## ???") == "화면"  # 남는 글자가 없어도 이름은 있어야 한다


def test_every_heading_gets_its_own_image_slot():
    """묶음 하나가 제목 여럿으로 갈릴 수 있다. 검색 단위가 제목이므로 전부 붙여야 한다."""
    got = bk.with_image_slots(
        "## 첫 질문\n답입니다.\n\n## 같은 뜻 다른 표현\n같은 답입니다."
    )
    assert got.count("![화면](images/") == 2
    assert "![화면](images/첫-질문.png)" in got
    assert "![화면](images/같은-뜻-다른-표현.png)" in got


def test_image_slots_survive_the_round_trip_into_chunks(tmp_path):
    """초안이 그대로 지식베이스로 옮겨졌을 때 조각마다 그림이 하나씩 달려야 한다."""
    from kakaobot.rag import split_markdown

    body = bk.with_image_slots("## 첫 질문\n답입니다.\n\n## 다른 표현\n같은 답입니다.")
    (tmp_path / "a.md").write_text(f"# 초안\n\n{body}\n", encoding="utf-8")
    chunks = split_markdown(tmp_path / "a.md")
    assert [c.images for c in chunks] == [
        ("images/첫-질문.png",),
        ("images/다른-표현.png",),
    ]
    assert all("![화면]" not in c.text for c in chunks)  # 근거에는 안 남는다
