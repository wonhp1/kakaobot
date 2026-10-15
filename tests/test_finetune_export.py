"""파인튜닝 후보 추출 — 추론 때와 같은 형식, 거절 포함, 개인정보 치환."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("export_finetune", ROOT / "scripts/export_finetune.py")
ef = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ef)

from kakaobot.llm import SYSTEM_PROMPT, build_user_prompt, prompt_sha  # noqa: E402

CTX = "[근거 1] (출처: 10-수강료 / 수강료)\n49만원입니다."


def rec(**kw):
    base = dict(event="answer", stage="ok", sender="Mina", question="수강료 얼마예요?",
                history="", context=CTX, reply="수강료는 49만원이에요!",
                system_sha=prompt_sha(SYSTEM_PROMPT))
    base.update(kw)
    return base


def test_answer_becomes_chat_example_in_inference_format():
    ex, _ = ef.to_example(rec(), SYSTEM_PROMPT)
    sys_, user, asst = ex["messages"]
    assert sys_ == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["content"] == build_user_prompt("수강료 얼마예요?", CTX, "")
    assert asst["content"] == "수강료는 49만원이에요!"
    assert ex["meta"]["same_prompt"] is True


def test_model_refusal_is_kept_as_refusal():
    ex, _ = ef.to_example(rec(event="escalate", stage="llm", reply="확인 후 답변드릴게요."),
                          SYSTEM_PROMPT)
    assert ex["messages"][2]["content"] == "답변불가"
    assert ex["meta"]["kind"] == "refusal"


def test_non_llm_records_are_excluded():
    for kw in (dict(event="escalate", stage="rag"),      # LLM 을 부르지도 않음
               dict(event="escalate", stage="guard"),    # 나쁜 출력
               dict(event="llm_error", stage="llm"),
               dict(context="")):                        # 이전 형식
        ex, why = ef.to_example(rec(**kw), SYSTEM_PROMPT)
        assert ex is None and why


def test_personal_info_is_scrubbed():
    ex, _ = ef.to_example(rec(
        question="010-1234-5678 로 연락 주세요? a.b@test.com",
        history="Mina: 저번에 물어본 거요\n봇: 네",
    ), SYSTEM_PROMPT)
    user = ex["messages"][1]["content"]
    assert "010-1234-5678" not in user and "[전화번호]" in user
    assert "a.b@test.com" not in user and "[이메일]" in user
    assert "Mina" not in user and "질문자: 저번에" in user
