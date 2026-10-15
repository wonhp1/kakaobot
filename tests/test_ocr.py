"""스크린샷 속 글자를 검색에 태우는 경로.

핵심은 두 가지다.
  1. 화면 속 글자는 **검색(embed_text)** 에만 들어가고 **근거(text)** 에는 안 들어간다.
     OCR 부스러기가 근거에 섞이면 모델이 그걸 사실처럼 옮긴다.
  2. 그림을 바꿔 끼우면 색인이 다시 만들어진다 (문서는 그대로여도).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.ocr import MIN_CONFIDENCE, OcrCache, _clean
from kakaobot.rag import (
    OCR_CACHE_NAME,
    image_refs,
    knowledge_fingerprint,
    load_knowledge,
    split_markdown,
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


DOC = """# 안내

## 홈브루 설치가 안 돼요

![설치 화면](images/brew.png)

터미널에 그 명령어를 그대로 붙여넣고 엔터를 누르세요.

## 화면 없는 항목

여기에는 그림이 없습니다.
"""


def test_image_line_leaves_the_grounds_but_feeds_the_search(tmp_path):
    write(tmp_path / "a.md", DOC)
    chunks = split_markdown(tmp_path / "a.md", screen_text=lambda p: "Install Homebrew")
    first, second = chunks

    assert first.images == ("images/brew.png",)
    assert "brew.png" not in first.text  # 근거에 파일 이름이 남지 않는다
    assert first.text.startswith("터미널에")
    assert "Install Homebrew" not in first.text  # 화면 글자도 근거에는 없다

    # 검색에는 들어가되 **본문과 섞이지 않는다** — 섞으면 본문 표현으로 묻는
    # 질문의 점수가 같이 떨어진다 (Chunk.views 주석의 실측표)
    body, screen = first.views
    assert body == first.embed_text and "Install Homebrew" not in body
    assert screen == "홈브루 설치가 안 돼요\nInstall Homebrew"

    assert second.images == ()
    assert second.screen_text == ""
    assert second.views == (second.embed_text,)  # 그림 없으면 관점도 하나


def test_chunk_without_reader_still_loads(tmp_path):
    write(tmp_path / "a.md", DOC)
    (first,) = split_markdown(tmp_path / "a.md")[:1]
    assert first.images == ("images/brew.png",)
    assert first.screen_text == ""
    assert first.embed_text.endswith("엔터를 누르세요.")


def test_image_refs_are_resolved_against_the_document(tmp_path):
    write(tmp_path / "a.md", DOC)
    assert image_refs(tmp_path) == [tmp_path / "images/brew.png"]


def test_fingerprint_notices_a_swapped_screenshot(tmp_path):
    write(tmp_path / "a.md", DOC)
    (tmp_path / "images").mkdir()
    shot = tmp_path / "images" / "brew.png"

    shot.write_bytes(b"\x89PNG-one")
    before = knowledge_fingerprint(tmp_path)
    shot.write_bytes(b"\x89PNG-two-is-longer")
    assert knowledge_fingerprint(tmp_path) != before


def test_fingerprint_notices_a_missing_screenshot(tmp_path):
    """문서가 가리키는데 그림이 없으면, 나중에 넣었을 때 색인이 다시 만들어져야 한다."""
    write(tmp_path / "a.md", DOC)
    missing = knowledge_fingerprint(tmp_path)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "brew.png").write_bytes(b"\x89PNG")
    assert knowledge_fingerprint(tmp_path) != missing


def test_cache_keys_on_content_not_name(tmp_path):
    """이름을 바꿔도 다시 안 읽고, 내용을 고치면 다시 읽는다."""
    calls = []

    class Counting(OcrCache):
        def text_for(self, path):
            calls.append(path.name)
            return super().text_for(path)

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")

    cache = Counting(tmp_path / OCR_CACHE_NAME)
    # 그림으로 열리지 않으므로 OCR 은 실패하고 빈 글자가 캐시된다 — 그래도 캐시는 동작한다
    assert cache.text_for(a) == ""
    assert cache.text_for(b) == ""
    assert calls == ["a.png", "b.png"]
    assert cache.digest(a) == cache.digest(b)


def test_cache_survives_a_restart(tmp_path):
    path = tmp_path / OCR_CACHE_NAME
    shot = tmp_path / "s.png"
    shot.write_bytes(b"bytes")

    first = OcrCache(path)
    first._data[OcrCache.digest(shot)] = {"text": "화면 글자", "lines": 1}
    first._dirty = True
    first.save()

    assert OcrCache(path).text_for(shot) == "화면 글자"


def test_cache_is_dropped_when_the_recognition_settings_change(tmp_path):
    path = tmp_path / OCR_CACHE_NAME
    shot = tmp_path / "s.png"
    shot.write_bytes(b"bytes")
    old = OcrCache(path)
    old._data[OcrCache.digest(shot)] = {"text": "옛 결과"}
    old._dirty = True
    old.save()

    class Newer(OcrCache):
        VERSION = OcrCache.VERSION + 1

    assert Newer(path)._data == {}


def test_broken_screenshot_does_not_stop_indexing(tmp_path, caplog):
    """그림 한 장이 깨져도 나머지 문서는 색인돼야 한다."""
    write(tmp_path / "a.md", DOC)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "brew.png").write_bytes(b"not an image")

    chunks = load_knowledge(tmp_path)
    assert len(chunks) == 2
    assert chunks[0].screen_text == ""
    assert chunks[0].text.startswith("터미널에")  # 근거는 멀쩡하다


class StubModel:
    """텍스트를 미리 정한 벡터로 바꾼다. 임베딩 모델을 안 띄우고 검색 규칙만 본다."""

    def __init__(self, table):
        self._table = table

    def encode(self, texts, **_):
        import numpy as np

        return np.asarray([self._table[t] for t in texts], dtype=float)


def retriever_over(chunks, table):
    from kakaobot.rag import Retriever

    r = Retriever()
    r._model = StubModel(table)
    r.index(chunks)
    return r


def test_a_chunk_scores_by_its_best_view_and_appears_once():
    """화면으로 걸려도 본문으로 걸려도 한 조각은 결과에 한 번만 나온다."""
    from kakaobot.rag import Chunk

    a = Chunk(doc="d", heading="A", text="본문", screen_text="화면")
    b = Chunk(doc="d", heading="B", text="다른 본문")
    c = Chunk(doc="d", heading="C", text="상관없는 본문")
    table = {
        a.views[0]: [1.0, 0.0],  # A 본문
        a.views[1]: [0.0, 1.0],  # A 화면
        b.views[0]: [0.0, 0.8],
        c.views[0]: [0.6, 0.0],
        "화면 쪽 질문": [0.0, 1.0],
        "본문 쪽 질문": [1.0, 0.0],
    }
    r = retriever_over([a, b, c], table)

    hits = r.search("화면 쪽 질문", top_k=3)
    assert [h.chunk.heading for h in hits] == [
        "A",
        "B",
        "C",
    ]  # A 가 두 칸 차지하지 않는다
    assert hits[0].score == pytest.approx(1.0)  # 화면 관점으로 얻은 점수

    hits = r.search("본문 쪽 질문", top_k=3)
    assert hits[0].chunk.heading == "A" and hits[0].score == pytest.approx(1.0)


def test_adding_a_screen_view_never_lowers_a_chunks_score():
    """이어붙임과 달라야 하는 지점 — 화면을 넣었다고 본문 질문이 깎이면 안 된다."""
    from kakaobot.rag import Chunk

    plain = Chunk(doc="d", heading="A", text="본문")
    withshot = Chunk(doc="d", heading="A", text="본문", screen_text="화면")
    table = {
        plain.views[0]: [1.0, 0.0],
        withshot.views[1]: [0.0, 1.0],
        "본문 쪽 질문": [1.0, 0.0],
    }
    before = retriever_over([plain], table).search("본문 쪽 질문", top_k=1)[0].score
    after = retriever_over([withshot], table).search("본문 쪽 질문", top_k=1)[0].score
    assert after == pytest.approx(before)


def test_a_missing_screenshot_does_not_stop_indexing(tmp_path):
    """초안(…-화면필요.md)은 그림 자리가 빈 채로 들어온다. 그래도 색인은 돼야 한다."""
    write(tmp_path / "a.md", DOC)  # images/brew.png 를 가리키지만 파일이 없다
    chunks = load_knowledge(tmp_path)
    assert len(chunks) == 2
    assert chunks[0].images == ("images/brew.png",)
    assert chunks[0].screen_text == ""
    assert chunks[0].views == (chunks[0].embed_text,)


def test_clean_drops_icon_crumbs_but_keeps_content():
    assert _clean(["V", ":", "~", "설치", "+", "1.2.3"]) == "설치\n1.2.3"
    assert _clean(["같은 줄", "같은 줄", "다른 줄"]) == "같은 줄\n다른 줄"


# --- 여기부터는 진짜 Vision 을 부른다 (macOS 전용) ---


def render_png(path: Path, text: str) -> Path:
    """글자를 적은 그림을 만든다. OCR 이 그 글자를 되찾아오는지 보려고."""
    AppKit = pytest.importorskip("AppKit")

    image = AppKit.NSImage.alloc().initWithSize_((900, 200))
    image.lockFocus()
    AppKit.NSColor.whiteColor().setFill()
    AppKit.NSBezierPath.fillRect_(((0, 0), (900, 200)))
    attrs = {
        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(52),
        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor(),
    }
    AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_((40, 70), attrs)
    image.unlockFocus()

    rep = AppKit.NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    png.writeToFile_atomically_(str(path), True)
    return path


@pytest.mark.parametrize("phrase", ["command not found", "설치가 필요합니다"])
def test_vision_reads_the_words_off_a_real_png(tmp_path, phrase):
    pytest.importorskip("Vision")
    from kakaobot.ocr import read_image

    result = read_image(render_png(tmp_path / "s.png", phrase))
    assert phrase.split()[0] in result.text
    assert result.confidence >= MIN_CONFIDENCE


def test_screen_words_reach_the_embedding_end_to_end(tmp_path):
    """문서 + 진짜 스크린샷 → 화면 문구가 검색 대상에 들어간다."""
    pytest.importorskip("Vision")
    write(tmp_path / "a.md", DOC)
    (tmp_path / "images").mkdir()
    render_png(tmp_path / "images" / "brew.png", "command not found")

    chunk = load_knowledge(tmp_path)[0]
    assert "command not found" in chunk.screen_text
    assert "command not found" in "\n".join(chunk.views)
    assert "command not found" not in chunk.text  # 근거에는 안 들어간다
    assert (tmp_path / OCR_CACHE_NAME).exists()  # 두 번째부터는 안 읽는다
