#!/usr/bin/env bash
# 카카오봇 처음 설치 (macOS). 여러 번 돌려도 된다 — 이미 된 단계는 건너뛴다.
#
#   bash scripts/setup.sh                            # 기본
#   bash scripts/setup.sh --allow-unattended-send    # 봇이 답을 보낼 때 Touch ID 승인 없이 보내게
#
# --allow-unattended-send 는 openkakao-cli 의 '전송마다 사람 승인' 보안 장치를 끄는 패치를 넣는다
# (docs/patches/README.md 004). 무인 답변 봇이 되려면 필요하지만, 봇이 보내기로 정한 글은 사람이
# 보지 않고 나간다. 위험을 읽고 동의한 경우에만 쓴다. 없이 설치해도 읽기·연습 모드는 된다.
#
# 하는 일
#   1. 도구 확인: macOS · git · uv · Rust(cargo) · Xcode 명령줄 도구
#   2. 파이썬 환경(.venv) — uv sync
#   3. openkakao-cli 받기(기준 커밋) → 패치 → 빌드 (vendor/openkakao-cli)
#   4. 설정 파일·지식베이스 예시 복사 (이미 있으면 그대로 둔다)
#   5. openkakao 설정 만들기 (~/.config/openkakao/config.toml — 전송 허용 목록은 비워 둔다)
#   6. 시험 (pytest)
#
# 하지 않는 일 — 사람이 한다 (README · CLAUDE.md '세팅'):
#   손쉬운 사용 권한 켜기 · 방 이름과 전송 허용 목록 채우기 · 텔레그램 토큰 (scripts/setup-secrets.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

OPENKAKAO_REPO="https://github.com/JungHoonGhae/openkakao-cli.git"
OPENKAKAO_COMMIT="e9d54e4308d0273e01fd900b651ad26cedc97e7c"
OPENKAKAO_DIR="vendor/openkakao-cli"
OPENKAKAO_BIN="$OPENKAKAO_DIR/target/release/openkakao-cli"
OPENKAKAO_CONFIG="$HOME/.config/openkakao/config.toml"

UNATTENDED=0
for arg in "$@"; do
  case "$arg" in
    --allow-unattended-send) UNATTENDED=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $arg (--help 참고)" >&2; exit 2 ;;
  esac
done

step() { printf '\n▶ %s\n' "$1"; }
ok()   { printf '  ✅ %s\n' "$1"; }
warn() { printf '  ⚠️  %s\n' "$1"; }
die()  { printf '  ❌ %s\n' "$1" >&2; exit 1; }

# --- 1. 도구 ---------------------------------------------------------------
step "도구 확인"
[ "$(uname -s)" = "Darwin" ] || die "macOS 에서만 동작합니다 (카카오톡 맥 앱을 접근성으로 다룹니다)."
command -v git >/dev/null || die "git 이 없습니다 — xcode-select --install"
xcode-select -p >/dev/null 2>&1 || die "Xcode 명령줄 도구가 없습니다 — xcode-select --install"
command -v uv >/dev/null || die "uv 가 없습니다 — brew install uv  (또는 https://docs.astral.sh/uv/)"
command -v cargo >/dev/null || die "Rust 가 없습니다 — brew install rustup && rustup-init -y  (그다음 새 터미널)"
[ -d "/Applications/KakaoTalk.app" ] || warn "카카오톡 맥 앱이 /Applications 에 없습니다 — 앱스토어에서 설치하세요."
ok "macOS · git · uv · cargo"

# --- 2. 파이썬 환경 --------------------------------------------------------
step "파이썬 환경 (.venv)"
uv sync --extra dev --inexact
ok "의존성 설치됨 ($(.venv/bin/python --version))"

# --- 3. openkakao-cli -----------------------------------------------------
step "openkakao-cli (카카오톡 읽기·보내기 도구)"
if [ -x "$OPENKAKAO_BIN" ]; then
  ok "이미 빌드돼 있습니다 — $OPENKAKAO_BIN (다시 빌드하려면 vendor/openkakao-cli 를 지우고 실행)"
else
  if [ ! -d "$OPENKAKAO_DIR/.git" ]; then
    git clone --quiet "$OPENKAKAO_REPO" "$OPENKAKAO_DIR"
  fi
  (
    cd "$OPENKAKAO_DIR"
    if ! git diff --quiet; then
      die "$OPENKAKAO_DIR 에 고친 내용이 있습니다 — 지우고 다시 실행하세요."
    fi
    git checkout --quiet "$OPENKAKAO_COMMIT"
    git apply "$ROOT/docs/patches/openkakao-cli-base.patch"
    if [ "$UNATTENDED" = 1 ]; then
      git apply "$ROOT/docs/patches/openkakao-cli-unattended.patch"
      echo "  ⚠️  무인 전송 패치 적용 — 봇의 답은 사람 승인 없이 나갑니다 (docs/patches/README.md 004)"
    else
      echo "  ℹ️  무인 전송 패치는 넣지 않았습니다 — 봇이 보낼 때마다 Touch ID/암호 창이 뜹니다"
    fi
    echo "  빌드 중 (처음엔 몇 분 걸립니다)…"
    cargo build --release --quiet
  )
  [ -x "$OPENKAKAO_BIN" ] || die "빌드 결과가 없습니다: $OPENKAKAO_BIN"
  ok "빌드됨 — $OPENKAKAO_BIN"
fi

# --- 4. 설정·지식베이스 ------------------------------------------------------
step "설정 파일과 지식베이스"
if [ -f config/config.toml ]; then
  ok "config/config.toml 이 이미 있습니다 — 그대로 둡니다"
else
  cp config/config.example.toml config/config.toml
  ok "config/config.toml 을 만들었습니다 — [room] name 과 [trigger] mention_names 를 채워야 합니다"
fi
if compgen -G "data/knowledge/*.md" >/dev/null; then
  ok "data/knowledge/ 에 문서가 있습니다 — 그대로 둡니다"
else
  mkdir -p data/knowledge
  cp -R data/knowledge.example/. data/knowledge/
  ok "예시 지식베이스를 data/knowledge/ 에 복사했습니다 (지어낸 내용 — 운영 전에 실제 자료로 바꾸세요)"
fi

# --- 5. openkakao 설정 ------------------------------------------------------
step "openkakao 설정 (전송 허용 목록)"
if [ -f "$OPENKAKAO_CONFIG" ]; then
  ok "$OPENKAKAO_CONFIG 이 이미 있습니다 — 그대로 둡니다"
else
  mkdir -p "$(dirname "$OPENKAKAO_CONFIG")"
  cat > "$OPENKAKAO_CONFIG" <<'TOML'
# openkakao-cli 설정 — 카카오봇 scripts/setup.sh 가 만든 기본값

[safety]
# 접근성(AX)으로 카카오톡 창에 글을 넣고 보내는 것을 허용한다. 봇이 답을 보내려면 필요하다.
allow_ax_send = true

# LOCO(서버 프로토콜) 쓰기는 막아 둔다 — 서버 로그인 경로는 계정 제재 사례가 보고됐다.
allow_loco_write = false

# 봇이 보내도 되는 방. 카카오톡 방 창의 제목과 **정확히** 같은 이름. 비어 있으면 아무 데도 보내지 않는다.
# 예: allowed_send_chats = ["나와의 채팅", "우리 강의 질문방"]
allowed_send_chats = []
TOML
  ok "$OPENKAKAO_CONFIG 을 만들었습니다 — 보낼 방 이름을 allowed_send_chats 에 넣어야 합니다"
fi

# --- 6. 시험 ----------------------------------------------------------------
step "시험 (pytest)"
.venv/bin/python -m pytest -q 2>&1 | tail -3

cat <<'NEXT'

설치가 끝났습니다. 남은 일 (사람이 해야 합니다):
  1. 손쉬운 사용 권한 — 시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용에서
     이 명령을 돌린 앱(터미널 · Claude 등)을 켭니다. (docs/permissions-setup.md)
  2. config/config.toml — [room] name(방 창 제목 그대로), [trigger] mention_names(그 방에서의 내 닉네임)
  3. ~/.config/openkakao/config.toml — allowed_send_chats 에 같은 방 이름
  4. 카카오톡에서 그 방을 더블클릭해 창으로 열어 두기 (+ 스레드 추가 질문을 쓰려면 '댓글 알림' 방도)
  5. 점검:  .venv/bin/python -m kakaobot --doctor
  6. 시작:  bash scripts/menubar.sh  → 화면 오른쪽 위 🤖 → 봇 시작 (처음엔 연습 모드)
  선택: 텔레그램 알림 — bash scripts/setup-secrets.sh (토큰은 직접 입력하세요)
NEXT
