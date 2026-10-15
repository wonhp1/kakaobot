"""스크린샷 속 글자 읽기 (macOS Vision).

지식베이스에 "이 화면에서 ○○를 누르세요" 류의 답이 들어가려면, 검색이
**화면에 적힌 말**과도 맞아떨어져야 한다. 수강생은 화면을 보고 그 안의
문구를 그대로 받아 적어 묻기 때문이다 ("command not found 라고 떠요").
그래서 문서가 참조하는 그림을 글자로 바꿔 임베딩에 함께 넣는다.

세 가지 성질이 이 방식을 고른 이유다:
  - **기기 안에서 끝난다.** Vision 은 macOS 내장이라 수강생 화면이 밖으로
    나가지 않는다 (계획서 §9 의 최소 권한 원칙).
  - **권한이 늘지 않는다.** 이미 있는 파일을 읽을 뿐이라 화면 녹화 권한과
    무관하다.
  - **빠르다.** 실측 0.4~1.3초/장. 결과는 파일 내용 해시로 캐시하므로
    같은 그림은 두 번 읽지 않는다.

한계: 읽어낸 글자는 **검색에만** 쓰고 답변 근거로는 주지 않는다. OCR 은
아이콘·구분선을 "V" ":" "~" 같은 부스러기로 흘리는데, 그게 근거에 섞이면
모델이 그걸 사실처럼 옮길 수 있다. 화면을 설명해야 하면 운영자가 문서
본문에 직접 쓴다 — 근거는 사람이 쓴 문장만.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kakaobot.ocr")

# 인식 언어. 앞에 둔 것이 우선된다.
LANGUAGES = ("ko-KR", "en-US")

# 이 아래 신뢰도의 줄은 버린다. 아이콘·장식에서 나온 헛글자가 대부분이다.
MIN_CONFIDENCE = 0.30

# 한두 글자짜리 줄도 버린다 ("V", ":", "~"). 단, 숫자는 남긴다 (버전·개수).
_JUNK_LINE = re.compile(r"^[^\w가-힣]{0,4}$|^[a-zA-Z가-힣]$")


class OcrUnavailable(RuntimeError):
    """Vision 을 쓸 수 없다 (macOS 가 아니거나 바인딩이 없음)."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    lines: int
    confidence: float

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _clean(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or _JUNK_LINE.match(s):
            continue
        if out and out[-1] == s:  # 표·목록에서 같은 줄이 연달아 잡히는 일이 잦다
            continue
        out.append(s)
    return "\n".join(out)


def read_image(path: str | Path) -> OcrResult:
    """그림 한 장에서 글자를 뽑는다. 네트워크를 쓰지 않는다."""
    try:
        import Quartz
        import Vision
    except ImportError as exc:  # pragma: no cover - macOS 전용
        raise OcrUnavailable(
            "pyobjc-framework-Vision 이 없습니다. uv pip install pyobjc-framework-Vision"
        ) from exc

    url = Quartz.CFURLCreateWithFileSystemPath(
        None, str(path), Quartz.kCFURLPOSIXPathStyle, False
    )
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None or Quartz.CGImageSourceGetCount(source) == 0:
        raise OcrUnavailable(f"그림으로 열 수 없습니다: {path}")
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(list(LANGUAGES))
    request.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, err = handler.performRequests_error_([request], None)
    if not ok:  # pragma: no cover - Vision 내부 실패
        raise OcrUnavailable(f"글자 인식 실패: {err}")

    lines, scores = [], []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)
        if not candidate:
            continue
        best = candidate[0]
        if best.confidence() < MIN_CONFIDENCE:
            continue
        lines.append(best.string())
        scores.append(float(best.confidence()))

    return OcrResult(
        text=_clean(lines),
        lines=len(lines),
        confidence=sum(scores) / len(scores) if scores else 0.0,
    )


class OcrCache:
    """파일 내용 해시로 기억한다.

    이름을 바꾸거나 옮겨도 다시 읽지 않고, 그림을 **고치면** 해시가 달라져
    저절로 다시 읽는다. 수정 시각을 쓰지 않는 이유는 git 이나 복사가
    시각을 흔들기 때문이다.
    """

    VERSION = 1  # 인식 설정을 바꾸면 올린다 — 옛 결과를 버리게 된다

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._dirty = False
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if loaded.get("version") == self.VERSION:
                    self._data = loaded.get("entries", {})
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("OCR 캐시를 읽지 못해 새로 만듭니다: %s", exc)

    @staticmethod
    def digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()[:32]

    def text_for(self, path: Path) -> str:
        """캐시에 있으면 그대로, 없으면 읽어서 넣는다. 실패해도 빈 글자만 돌려준다.

        어떤 실패도 색인을 멈추지 않는다. 그림은 검색을 **거들 뿐**이라,
        한 장 때문에 지식베이스 전체가 안 올라오는 쪽이 훨씬 나쁘다. 특히
        초안(`…-화면필요.md`)은 그림 자리가 비어 있는 채로 들어온다.
        """
        try:
            key = self.digest(path)
        except OSError as exc:
            log.warning("문서가 가리키는 그림이 없습니다 (%s): %s", path.name, exc)
            return ""
        hit = self._data.get(key)
        if hit is not None:
            return hit["text"]
        try:
            result = read_image(path)
        except Exception as exc:  # noqa: BLE001 - 색인을 지키는 것이 우선
            log.warning("화면 글자 읽기 실패 (%s): %s", path.name, exc)
            return ""
        log.info(
            "화면 글자 읽음: %s — %d줄, 신뢰도 %.2f",
            path.name,
            result.lines,
            result.confidence,
        )
        self._data[key] = {
            "text": result.text,
            "lines": result.lines,
            "confidence": round(result.confidence, 3),
            "name": path.name,  # 사람이 캐시를 열어봤을 때 알아보라고
        }
        self._dirty = True
        return result.text

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": self.VERSION, "entries": self._data},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)
        self._dirty = False
