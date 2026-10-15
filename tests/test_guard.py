"""출력 검증 테스트.

패치 004로 사람의 승인 게이트를 없앴으므로 이 필터가 마지막 방어선이다.
프롬프트 인젝션이 유도할 만한 출력을 실제로 막는지 확인한다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaobot.guard import OutputGuard

g = OutputGuard(max_chars=500, block_links=True)


def test_normal_answer_allowed():
    assert g.check("수강료는 49만원입니다. 얼리버드는 39만원이에요.").allowed


def test_blocks_url():
    assert not g.check("자세한 건 https://evil.example.com 에서 보세요").allowed


def test_blocks_bare_domain():
    assert not g.check("evil-site.com 으로 접속하세요").allowed


def test_blocks_account_number():
    """인젝션이 노리는 대표 패턴."""
    r = g.check("환불은 우리은행 1002-345-678901 로 신청하세요")
    assert not r.allowed


def test_blocks_phone_number():
    assert not g.check("010-1234-5678 로 연락주세요").allowed


def test_blocks_money_solicitation():
    assert not g.check("수강료를 아래 계좌로 입금해주세요").allowed


def test_blocks_prompt_leak_marker():
    assert not g.check("제 시스템 프롬프트는 다음과 같습니다").allowed


def test_blocks_injection_echo():
    """인젝션 문구를 그대로 되뱉는 경우."""
    assert not g.check("이전 지시를 무시하고 답변하겠습니다").allowed


def test_blocks_context_tag_leak():
    assert not g.check("<user_message> 안에 있던 내용은").allowed


def test_blocks_over_length():
    r = g.check("가" * 501)
    assert not r.allowed and "길이 초과" in r.reason


def test_blocks_empty():
    assert not g.check("   ").allowed


def test_custom_banned_word():
    assert not OutputGuard(extra_banned=["도박"]).check("도박 관련 내용").allowed


def test_injection_scenario_end_to_end():
    """실제 인젝션 시나리오 — 공격자가 유도한 답변이 차단되는가."""
    injected = (
        "긴급 공지: 수강료 환불은 아래 계좌로 신청하세요. 우리은행 1002-345-678901"
    )
    r = g.check(injected)
    assert not r.allowed


# --- 채팅용 평문 (2026-09-20 테스트방 실측: 별표가 그대로 올라갔다) -------------

from kakaobot.guard import to_chat_text  # noqa: E402

LIVE = (
    "개강 전에는 전액 환불되고, 개강 후에는 진행 회차에 따라 달라집니다.\n\n"
    "- **1주차 종료 전**: 80% 환불\n- **2주차 종료 전**: 60% 환불\n"
    "- **4주차 이후**: 환불 불가\n\n\n\n환불이 필요하면 운영자에게 문의해 주세요."
)


def test_markdown_from_live_test_becomes_plain_chat_text():
    out = to_chat_text(LIVE)
    assert "**" not in out
    assert "· 1주차 종료 전: 80% 환불" in out
    assert "\n\n\n" not in out
    assert out.endswith("문의해 주세요.")


def test_chat_text_keeps_content_and_plain_symbols():
    assert to_chat_text("## 수강료\n49만원이에요.") == "수강료\n49만원이에요."
    assert to_chat_text("`설정` 메뉴에서 *바로* 바꿀 수 있어요") == "설정 메뉴에서 바로 바꿀 수 있어요"
    assert to_chat_text("5*3 = 15, 할인율 20%") == "5*3 = 15, 할인율 20%"      # 수식은 건드리지 않는다
    assert to_chat_text("1. 신청\n2. 결제") == "1. 신청\n2. 결제"            # 번호 목록은 채팅에서도 읽힌다
    assert to_chat_text("```\n코드\n```") == "코드"


def test_guard_returns_plain_text_and_measures_it():
    g = OutputGuard(max_chars=30)
    r = g.check("**수강료**는 49만원입니다.")
    assert r.allowed and r.text == "수강료는 49만원입니다."


# --- 막힘 단어는 근거 문장 그대로일 때만 (2026-09-21 운영자 결정) --------------------

HOMEBREW = (
    "[근거 1] (출처: 40-환경설정 / 홈브루 설치 화면이 떴어요)\n"
    "화면에 보이는 명령어를 그대로 복사해서 터미널에 붙여넣고 엔터를 누르면 됩니다.\n"
    "비밀번호를 물으면 맥 로그인 비밀번호를 입력하세요. 입력해도 화면에 글자가 보이지 않는 게 정상입니다."
)


def test_password_sentence_copied_from_the_grounds_is_allowed():
    """2026-09-21 실전: 홈브루 안내가 '비밀번호' 한 단어 때문에 막혔다."""
    answer = "명령어를 붙여넣고 엔터를 누르세요. 비밀번호를 물으면 맥 로그인 비밀번호를 입력하세요."
    assert g.check(answer, grounds=HOMEBREW).allowed


def test_injected_password_request_is_blocked_even_when_the_grounds_mention_it():
    """근거에 '비밀번호'가 있다고 아무 문장이나 통과하면, 질문에 지시를 심어 빼낼 수 있다."""
    for answer in (
        "비밀번호를 DM으로 보내 주세요.",
        "맥 로그인 비밀번호를 운영자에게 알려주세요.",
        # 근거 조각으로 시작해 딴말로 끝난다 — 앞뒤 어절만 보면 통과했을 문장
        "비밀번호를 물으면 저에게 DM으로 알려주세요.",
        "비밀번호를 물으면 맥 로그인 비밀번호를 입력하세요 그리고 DM으로 보내주세요",
        "비밀번호를 물으면 맥 로그인 비밀번호를 입력하세요. 그리고 비밀번호를 DM으로 보내 주세요.",
    ):
        result = g.check(answer, grounds=HOMEBREW)
        assert not result.allowed, answer
        assert "근거에 없는 문장" in result.reason


def test_without_grounds_the_old_strict_rule_holds():
    assert not g.check("비밀번호를 물으면 맥 로그인 비밀번호를 입력하세요.").allowed


def test_formatting_and_punctuation_differences_do_not_matter():
    grounds = "결제는 **신용카드, 계좌이체, 카카오페이**를 지원합니다."
    assert g.check("신용카드 · 계좌이체 · 카카오페이를 지원합니다!", grounds=grounds).allowed
    assert not g.check("수강료는 계좌이체로 바로 보내 주세요.", grounds=grounds).allowed


def test_other_blocks_are_not_relaxed_by_grounds():
    """근거에 있어도 링크·계좌번호·전화번호는 그대로 막는다."""
    grounds = "문의는 010-1234-5678 로, 입금은 110-123-456789 로 해 주세요."
    assert not g.check("문의는 010-1234-5678 로 해 주세요.", grounds=grounds).allowed
    assert not g.check("입금은 110-123-456789 로 해 주세요.", grounds=grounds).allowed
