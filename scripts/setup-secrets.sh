#!/usr/bin/env bash
# 카카오봇 자격증명 설정. 토큰은 이 스크립트 안에서만 다뤄지고 화면에 찍히지 않는다.
#
#   bash scripts/setup-secrets.sh
#
# 기본은 macOS 키체인에 저장하고 ~/.zshrc 에는 "키체인에서 꺼내는 코드"만 넣는다.
# 토큰 평문이 셸 프로필에 남지 않는다.

set -euo pipefail

ZSHRC="$HOME/.zshrc"
MARK_BEGIN="# >>> kakaobot secrets >>>"
MARK_END="# <<< kakaobot secrets <<<"

say() { printf '%s\n' "$*"; }
ok()  { printf '  ✅ %s\n' "$*"; }
bad() { printf '  ❌ %s\n' "$*" >&2; }

store() {   # store <키체인이름> <값>
  security add-generic-password -U -a "$USER" -s "$1" -w "$2" 2>/dev/null
}

say "════ 카카오봇 자격증명 설정 ════"
say ""
say "먼저 텔레그램에서, 알림을 받을 방에 봇이 있어야 합니다:"
say "  · 개인 대화 — 봇을 검색해 대화를 열고 /start 를 보낸다"
say "  · 그룹 방   — 그룹에 봇을 초대하고 /start 를 보낸다"
say ""

# ---------- 텔레그램 ----------
say "[1/3] 텔레그램 봇 토큰"
say "      @BotFather 에서 발급한 값. 입력해도 화면에 보이지 않습니다."
printf '      토큰 (건너뛰려면 엔터): '
read -rs TG_TOKEN; echo

if [ -n "${TG_TOKEN:-}" ]; then
  say ""
  say "[2/3] 채팅방 id 조회 중..."
  RESP="$(curl -sf --max-time 15 "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" || true)"

  if [ -z "$RESP" ] || ! printf '%s' "$RESP" | grep -q '"ok":true'; then
    bad "토큰이 올바르지 않거나 텔레그램에 연결할 수 없습니다."
    bad "@BotFather 에서 토큰을 확인하세요."
    exit 1
  fi

  # 봇이 받은 최근 대화에서 방 목록을 뽑는다 (최근 것 먼저). 예전에는 가장 오래된 방 하나를
  # 그냥 골라, 개인 대화와 그룹이 섞이면 엉뚱한 방으로 알림이 갈 수 있었다 (2026-09-26 수정).
  # 그룹은 봇을 초대만 해도 잡힌다 (my_chat_member). 방 이름은 이 화면에만 보인다.
  CHATS="$(printf '%s' "$RESP" | python3 -c '
import json, sys
seen = {}
for u in json.load(sys.stdin).get("result", []):
    for key in ("message", "edited_message", "channel_post", "my_chat_member", "chat_member"):
        c = (u.get(key) or {}).get("chat")
        if not c:
            continue
        name = (c.get("title")
                or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
                or c.get("username") or "(이름 없음)")
        kind = {"private": "개인 대화", "group": "그룹", "supergroup": "그룹",
                "channel": "채널"}.get(c.get("type"), c.get("type", ""))
        seen.pop(c["id"], None)
        seen[c["id"]] = (kind, name.replace("\t", " "))
for cid, (kind, name) in reversed(list(seen.items())):
    print(f"{cid}\t{kind}\t{name}")
')"

  COUNT="$(printf '%s\n' "$CHATS" | grep -c . || true)"
  if [ "$COUNT" -eq 0 ]; then
    bad "채팅방을 찾지 못했습니다."
    bad "알림을 받을 방에서 봇에게 /start 를 보낸 뒤 다시 실행하세요 (그룹이면 봇을 초대한 뒤 /start)."
    exit 1
  fi
  PICK=1
  if [ "$COUNT" -gt 1 ]; then
    say "  봇이 들어가 있는 방이 여러 개입니다. 알림을 받을 방을 고르세요 (최근 순):"
    i=0
    while IFS=$'\t' read -r _cid kind name; do
      i=$((i + 1))
      say "    $i) $name — $kind"
    done <<< "$CHATS"
    printf '  번호 [1]: '
    read -r PICK
    PICK="${PICK:-1}"
    case "$PICK" in ''|*[!0-9]*) bad "번호를 입력하세요."; exit 1 ;; esac
    if [ "$PICK" -lt 1 ] || [ "$PICK" -gt "$COUNT" ]; then
      bad "1~$COUNT 중에서 고르세요."
      exit 1
    fi
  fi
  TG_CHAT="$(printf '%s\n' "$CHATS" | sed -n "${PICK}p" | cut -f1)"
  TG_NAME="$(printf '%s\n' "$CHATS" | sed -n "${PICK}p" | cut -f3)"
  ok "알림 받을 방: $TG_NAME (id $TG_CHAT)"

  say ""
  say "  테스트 메시지 전송 중..."
  if curl -sf --max-time 15 -X POST \
       "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
       -d "chat_id=${TG_CHAT}" \
       -d "text=✅ 카카오봇 알림 연결 완료" >/dev/null; then
    ok "텔레그램에 테스트 메시지를 보냈습니다. 확인해보세요."
  else
    bad "전송에 실패했습니다."
    exit 1
  fi

  store kakaobot-telegram-token "$TG_TOKEN"
  store kakaobot-telegram-chat  "$TG_CHAT"
  ok "키체인에 저장했습니다."
  unset TG_TOKEN
fi

# ---------- Anthropic ----------
say ""
say "[3/3] Anthropic API 키 (봇 답변 생성용)"
printf '      키 (건너뛰려면 엔터): '
read -rs AN_KEY; echo

if [ -n "${AN_KEY:-}" ]; then
  if curl -sf --max-time 20 https://api.anthropic.com/v1/messages \
       -H "x-api-key: ${AN_KEY}" \
       -H "anthropic-version: 2023-06-01" \
       -H "content-type: application/json" \
       -d '{"model":"claude-haiku-4-5","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}' \
       >/dev/null 2>&1; then
    ok "API 키가 유효합니다."
    store kakaobot-anthropic-key "$AN_KEY"
    ok "키체인에 저장했습니다."
  else
    bad "API 키가 유효하지 않거나 연결할 수 없습니다."
    exit 1
  fi
  unset AN_KEY
fi

# ---------- ~/.zshrc ----------
say ""
say "셸 설정 갱신 중..."
if [ -f "$ZSHRC" ] && grep -q "$MARK_BEGIN" "$ZSHRC"; then
  python3 - "$ZSHRC" "$MARK_BEGIN" "$MARK_END" <<'PY'
import re, sys
path, b, e = sys.argv[1:4]
t = open(path, encoding="utf-8").read()
t = re.sub(re.escape(b) + r".*?" + re.escape(e) + r"\n?", "", t, flags=re.S)
open(path, "w", encoding="utf-8").write(t)
PY
fi

cat >> "$ZSHRC" <<'BLOCK'
# >>> kakaobot secrets >>>
# 값이 아니라 "키체인에서 꺼내는 코드"만 둔다. 평문이 파일에 남지 않는다.
_kakaobot_secret() { security find-generic-password -a "$USER" -s "$1" -w 2>/dev/null; }
export TELEGRAM_BOT_TOKEN="$(_kakaobot_secret kakaobot-telegram-token)"
export TELEGRAM_CHAT_ID="$(_kakaobot_secret kakaobot-telegram-chat)"
export ANTHROPIC_API_KEY="$(_kakaobot_secret kakaobot-anthropic-key)"
# <<< kakaobot secrets <<<
BLOCK

ok "$ZSHRC 갱신 완료"
say ""
say "════ 완료 ════"
say "새 터미널을 열거나 다음을 실행하세요:"
say ""
say "    source ~/.zshrc"
say ""
say "봇이 설정을 읽어 알림을 보낼 수 있는지 확인 (봇 폴더에서):"
say ""
say "    .venv/bin/python -m kakaobot --test-notify"
say ""
say "그다음 봇을 다시 시작하면 알림이 켜집니다 (메뉴바 → 봇 중지 → 봇 시작)."
say ""
