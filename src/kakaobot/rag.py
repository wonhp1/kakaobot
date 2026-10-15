"""RAG 검색 (계획서 §6-1 [6][7], §6-3).

답변 근거는 여기서만 나온다. 모델의 일반 지식에 의존하지 않는다 (계획서 §1).
근거가 약하면 답하지 않고 에스컬레이션한다 — 침묵이 오답보다 낫다 (계획서 §3).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kakaobot.rag")

# 임베딩 모델. 장비 교체 후에도 이 값만 바꾸면 된다.
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"


@dataclass(frozen=True)
class Chunk:
    """검색 단위. 문서를 '## 제목' 기준으로 쪼갠 조각."""

    doc: str
    heading: str
    text: str
    # 이 조각이 참조하는 스크린샷 (문서 폴더 기준 상대 경로)
    images: tuple[str, ...] = ()
    # 그 스크린샷에서 읽어낸 화면 속 글자. 검색에만 쓰고 근거로는 주지 않는다
    screen_text: str = ""

    @property
    def id(self) -> str:
        raw = f"{self.doc}::{self.heading}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def embed_text(self) -> str:
        """제목을 본문 앞에 붙여 넣는다. 제목이 질문 형태라 검색 정확도가 오른다."""
        return f"{self.heading}\n{self.text}"

    @property
    def views(self) -> tuple[str, ...]:
        """이 조각을 찾아낼 수 있는 여러 관점. 각각 따로 벡터가 된다.

        화면 속 글자를 본문 뒤에 **이어 붙이면 안 된다.** 임베딩은 글 전체의
        뜻을 평균 내므로, 105자 본문에 415자 화면 글자를 붙이면 본문 표현으로
        묻는 질문의 점수가 같이 떨어진다. 실측 (2026-09-20, 홈브루 설치 화면):

            질문                      본문만   이어붙임   화면만
            "GitHub release 에서…"     0.383    0.412    0.445
            "붙여넣고 엔터 치면 되나요"    0.534    0.480    0.449

        이어붙임은 어느 쪽에서도 최선이 아니다. 관점을 따로 두고 **더 높은
        쪽을 그 조각의 점수로** 삼으면 둘 다 지킬 수 있다 (Retriever.search).
        """
        out = [self.embed_text]
        if self.screen_text:
            out.append(f"{self.heading}\n{self.screen_text}")
        return tuple(out)


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


# 문서가 스크린샷을 가리키는 방법: ![설명](images/파일.png)
_IMAGE_REF = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")


def split_markdown(path: Path, screen_text=None) -> list[Chunk]:
    """'## 제목' 단위로 쪼갠다. 제목 없는 앞부분은 버린다.

    그림 참조 줄(`![](images/x.png)`)은 본문에서 **떼어낸다**. 근거로 넘어가는
    text 에 남으면 모델이 파일 이름을 답에 옮길 수 있는데, 카카오톡으로는
    글자만 보내므로 수강생에게는 뜻 없는 말이 된다.

    screen_text: 그림 경로를 받아 화면 속 글자를 돌려주는 함수 (없으면 안 읽는다).
    """
    text = path.read_text(encoding="utf-8")
    doc = path.stem
    chunks: list[Chunk] = []
    heading: str | None = None
    buf: list[str] = []
    images: list[str] = []

    def flush() -> None:
        if heading and buf:
            body = "\n".join(buf).strip()
            if body:
                screen = ""
                if images and screen_text is not None:
                    read = [screen_text(path.parent / ref) for ref in images]
                    screen = "\n".join(s for s in read if s)
                chunks.append(
                    Chunk(
                        doc=doc,
                        heading=heading,
                        text=body,
                        images=tuple(images),
                        screen_text=screen,
                    )
                )

    for line in text.splitlines():
        m = re.match(r"^##\s+(.*\S)\s*$", line)
        if m:
            flush()
            heading = m.group(1)
            buf, images = [], []
        elif heading is not None:
            ref = _IMAGE_REF.match(line)
            if ref:
                images.append(ref.group(1))
            else:
                buf.append(line)
    flush()
    return chunks


def image_refs(directory: str | Path) -> list[Path]:
    """지식베이스 문서들이 가리키는 그림 파일 (없는 파일도 그대로 돌려준다)."""
    d = Path(directory)
    out: list[Path] = []
    for p in knowledge_files(d):
        for line in p.read_text(encoding="utf-8").splitlines():
            ref = _IMAGE_REF.match(line)
            if ref:
                out.append(p.parent / ref.group(1))
    return out


def knowledge_files(directory: str | Path) -> list[Path]:
    d = Path(directory)
    if not d.exists():
        raise FileNotFoundError(f"지식베이스 디렉터리가 없습니다: {d}")
    # 00- 로 시작하는 README 등 안내 문서는 제외
    return [p for p in sorted(d.glob("*.md")) if not p.name.startswith("00-")]


OCR_CACHE_NAME = ".ocr-cache.json"


def load_knowledge(directory: str | Path, read_screens: bool = True) -> list[Chunk]:
    """문서를 조각으로 읽는다.

    read_screens=False 는 그림을 건드리지 않는다 — Vision 이 없는 환경이나
    검색 품질 비교(화면 글자를 넣었을 때와 뺐을 때)에 쓴다.
    """
    d = Path(directory)
    cache = None
    if read_screens:
        from .ocr import OcrCache

        cache = OcrCache(d / OCR_CACHE_NAME)

    chunks: list[Chunk] = []
    for p in knowledge_files(d):
        chunks.extend(split_markdown(p, screen_text=cache.text_for if cache else None))
    if cache is not None:
        cache.save()
    return chunks


def knowledge_fingerprint(directory: str | Path) -> tuple:
    """문서가 추가·수정·삭제됐는지 가볍게 알아본다 (파일 이름·크기·수정 시각).

    문서가 가리키는 그림도 함께 본다. 그림만 바꿔 넣었을 때도 색인이 다시
    만들어져야 화면 속 글자가 갱신된다.
    """
    out = []
    for p in list(knowledge_files(directory)) + image_refs(directory):
        try:
            st = p.stat()
        except OSError:
            out.append((p.name, -1, 0))  # 문서가 가리키는데 없는 그림 — 이것도 변화다
            continue
        out.append((p.name, st.st_size, st.st_mtime_ns))
    return tuple(out)


class Retriever:
    """임베딩 + 코사인 유사도 검색.

    문서 수가 수천 단위를 넘지 않는 한 벡터 DB 없이 메모리 행렬로 충분하다.
    ChromaDB 도입은 규모가 커진 뒤에 결정한다 (지금 넣으면 복잡도만 는다).
    """

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL) -> None:
        self._model_name = model_name
        self._model = None
        self._chunks: list[Chunk] = []
        self._matrix = None

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("임베딩 모델 로딩: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def index(self, chunks: list[Chunk]) -> int:
        if not chunks:
            raise ValueError("색인할 문서가 없습니다")
        import numpy as np

        model = self._ensure_model()
        self._chunks = chunks
        texts: list[str] = []
        owner: list[int] = []  # 벡터 → 그 벡터가 속한 조각
        for i, chunk in enumerate(chunks):
            for view in chunk.views:
                texts.append(view)
                owner.append(i)
        self._owner = np.asarray(owner)
        self._matrix = model.encode(
            texts,
            normalize_embeddings=True,  # 정규화하면 내적 = 코사인 유사도
            show_progress_bar=False,
        )
        extra = len(texts) - len(chunks)
        log.info(
            "색인 완료: 문서 조각 %d개%s",
            len(chunks),
            f" (화면 글자 관점 {extra}개 포함)" if extra else "",
        )
        return len(chunks)

    def search(self, query: str, top_k: int = 4) -> list[Hit]:
        """조각마다 관점 중 **가장 잘 맞은 것**의 점수를 준다.

        관점을 늘려도 조각 수만큼만 결과가 나온다 — 같은 조각이 본문과 화면
        양쪽으로 걸려 top_k 를 혼자 차지하는 일이 없어야 하기 때문이다.
        """
        return self.search_many([query], top_k=top_k)

    def search_many(
        self, queries: list[str], top_k: int = 4, diverse: bool = False
    ) -> list[Hit]:
        """여러 질문으로 한꺼번에 찾는다. 조각 점수 = 가장 잘 맞은 (질문, 관점) 쌍.

        질문 글과 수강생 화면의 글자를 **이어 붙여 한 질문으로 만들지 않는다.**
        지식베이스 쪽에서 본 것과 같은 이유다 — 짧은 질문에 긴 화면 글자를 붙이면
        질문의 뜻이 묽어진다 (Chunk.views 주석의 실측표). 따로 찾고 최댓값을 쓴다.

        diverse: 여러 가지를 한 번에 물었을 때 (2026-09-26). 점수 순으로 top_k 만 자르면
        한 주제의 조각들이 자리를 다 차지해 다른 물음의 근거가 빠진다 — 실전에서 여섯 줄
        질문의 '난이도' 근거가 5등으로 밀려 답하지 못했다. 그래서 **물음마다 가장 잘 맞는
        조각을 먼저 넣고** 나머지를 점수 순으로 채운다 (최대 DIVERSE_LIMIT 개).
        """
        if self._matrix is None:
            raise RuntimeError("색인을 먼저 만들어야 합니다")
        import numpy as np

        model = self._ensure_model()
        q = model.encode(list(queries), normalize_embeddings=True)
        sims = self._matrix @ np.asarray(q).T                    # (관점, 질문)
        per_chunk = np.full((len(self._chunks), sims.shape[1]), -1.0)
        np.maximum.at(per_chunk, self._owner, sims)              # (조각, 질문)
        scores = per_chunk.max(axis=1)
        order = [int(i) for i in scores.argsort()[::-1]]
        if diverse and len(queries) > 1:
            picked: list[int] = []
            for j in range(per_chunk.shape[1]):
                best = int(per_chunk[:, j].argmax())
                if best not in picked:
                    picked.append(best)
            limit = min(max(top_k, len(picked)), DIVERSE_LIMIT)
            keep = sorted(picked, key=lambda i: -scores[i])[:limit]
            for i in order:
                if len(keep) >= limit:
                    break
                if i not in keep:
                    keep.append(i)
            order = sorted(keep, key=lambda i: -scores[i])
        else:
            order = order[:top_k]
        return [Hit(chunk=self._chunks[i], score=float(scores[i])) for i in order]


# 여러 가지를 한 번에 물었을 때 모델에 넘길 근거 조각의 상한 (search_many diverse)
DIVERSE_LIMIT = 8


@dataclass
class Grounding:
    """검색 결과와 그 판정 (계획서 §6-1 [7])."""

    hits: list[Hit]
    threshold: float

    @property
    def best_score(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    @property
    def confident(self) -> bool:
        """임계값 미달이면 답하지 않는다. 이 시스템의 핵심 튜닝 포인트 (D3)."""
        return self.best_score >= self.threshold

    def as_prompt_context(self) -> str:
        """LLM 에 넘길 근거 블록. 임계값을 넘은 조각만 넣는다."""
        parts = []
        for i, h in enumerate(self.hits, 1):
            if h.score < self.threshold:
                continue
            parts.append(
                f"[근거 {i}] (출처: {h.chunk.doc} / {h.chunk.heading})\n{h.chunk.text}"
            )
        return "\n\n".join(parts)

    def sources(self) -> list[str]:
        return [
            f"{h.chunk.doc}/{h.chunk.heading}"
            for h in self.hits
            if h.score >= self.threshold
        ]


class KnowledgeBase:
    """지식베이스 전체를 감싼다."""

    def __init__(
        self,
        directory: str | Path,
        threshold: float = 0.35,
        top_k: int = 4,
        model_name: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self._dir = Path(directory)
        self._threshold = threshold
        self._top_k = top_k
        self._model_name = model_name
        self._retriever = Retriever(model_name)
        self._ready = False
        self._fingerprint: tuple = ()

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def model_name(self) -> str:
        return self._model_name

    def configure(self, threshold: float, top_k: int) -> None:
        """임계값·top_k 는 다시 색인하지 않고 바꾼다."""
        self._threshold = threshold
        self._top_k = top_k

    def build(self) -> int:
        fingerprint = knowledge_fingerprint(self._dir)
        chunks = load_knowledge(self._dir)
        n = self._retriever.index(chunks)
        self._fingerprint = fingerprint
        self._ready = True
        return n

    def refresh_if_changed(self) -> int | None:
        """문서가 바뀌었으면 다시 색인한다. 임베딩 모델은 다시 읽지 않는다.

        운영자가 지식베이스에 문서를 넣으면 재시작 없이 다음 질문부터 반영된다
        (계획서 §6-3 성장 루프). 바뀐 게 없으면 None.
        """
        if self._ready and knowledge_fingerprint(self._dir) == self._fingerprint:
            return None
        return self.build()

    def ground(
        self, question: str, extra_queries: tuple[str, ...] = (), diverse: bool = False
    ) -> Grounding:
        """extra_queries: 질문과 **따로** 찾을 글 (함께 올린 화면의 글자, 여러 가지 물음의 한 줄씩).
        diverse: 여러 가지를 물었다 — 물음마다 가장 잘 맞는 근거를 빠뜨리지 않는다."""
        if not self._ready:
            raise RuntimeError("build() 를 먼저 호출해야 합니다")
        queries = [question, *(q for q in extra_queries if q and q.strip())]
        hits = self._retriever.search_many(queries, top_k=self._top_k, diverse=diverse)
        return Grounding(hits=hits, threshold=self._threshold)
