# kakaobot — Claude Code 안내

카카오톡 오픈채팅 질문에 지식베이스 근거로만 답하는 macOS 봇. 카카오톡 맥 앱 화면을 접근성(AX)으로
읽고, 답은 질문 메시지의 댓글(스레드)로 단다. 서버 로그인·카카오톡 데이터 파일 복호화는 하지 않는다.
사람용 설명은 `README.md`, 운영은 `docs/operations.md`.

## 사용자가 "세팅해줘" / "설치해줘" 라고 하면

아래 순서대로 한다. 사람이 해야 하는 단계는 **해 달라고 말하고 기다린다.**

1. **설명하고 확인받기** — 무엇을 설치하는지 짧게 말한다: Python 환경(uv), openkakao-cli(Rust 로
   빌드, 처음엔 몇 분), 설정 파일. 필요한 것: macOS, 카카오톡 맥 앱(로그인됨), Homebrew.
2. **무인 전송 동의 묻기** — 봇이 답을 보낼 때마다 Touch ID/암호 창이 뜨지 않게 하려면 openkakao-cli 의
   '전송마다 사람 승인' 장치를 끄는 패치가 필요하다 (docs/patches/README.md 004). 대신 봇이 보내기로
   정한 글은 사람이 보지 않고 나간다 (전송 허용 목록·출력 검증·근거 없으면 안 답함이 남은 방어선).
   **사용자가 동의할 때만** `--allow-unattended-send` 를 붙인다. 동의하지 않아도 읽기·연습 모드는 된다.
3. **도구 확인·설치** — `uv`, `cargo`(rustup), Xcode 명령줄 도구. 없으면 설치 명령을 알려 주고,
   사용자가 원하면 실행한다 (`brew install uv`, `brew install rustup && rustup-init -y`, `xcode-select --install`).
4. **설치 스크립트** — `bash scripts/setup.sh` (또는 `bash scripts/setup.sh --allow-unattended-send`).
   여러 번 돌려도 된다. 실패하면 출력의 ❌ 줄을 보고 고친다.
5. **방 정하기** — 사용자에게 묻는다:
   - 봇이 볼 방 이름 — 카카오톡 방 창 제목과 **정확히** 같게
   - 그 방에서 쓰는 운영자 닉네임 (`@닉네임` 멘션에 답하려고)
   그 값을 `config/config.toml` 의 `[room] name`, `[trigger] mention_names` 에 넣는다.
   실제로 보내려면 `~/.config/openkakao/config.toml` 의 `[safety] allowed_send_chats` 에도 같은 방 이름을
   넣는다 — **사용자가 그 방에 보내도 된다고 한 경우에만.** 처음엔 시험용 방을 권한다.
6. **권한** (사람이) — docs/permissions-setup.md. 시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용에서
   봇을 띄울 앱(터미널 또는 Claude)을 켠 뒤 그 앱을 다시 연다. 다운로드 폴더 접근을 물으면 허용.
7. **카카오톡 창** (사람이) — 그 방을 더블클릭해 창으로 열고 지금 데스크탑에 둔다 (최소화 X).
   스레드 추가 질문에도 답하려면 '댓글 알림' 방도 창으로 연다.
8. **점검** — `.venv/bin/python -m kakaobot --doctor`. ❌ 를 하나씩 풀어 준다.
   답변 엔진 기본값은 `claude -p`(Claude Code 구독) — `claude` 가 설치·로그인돼 있어야 한다.
9. **엔진 점검** (선택) — `.venv/bin/python -m kakaobot --check-llm` (시험 질문 3개, 1분쯤).
10. **시작** — `bash scripts/menubar.sh` → 화면 오른쪽 위 🤖 → '봇 시작'. 처음엔 **연습 모드**
    (`dry_run = true`, 보내지 않고 기록만). 시험용 방에서 다른 사람이 질문해 보고 `logs/` 를 같이 확인한 뒤,
    사용자가 원하면 메뉴바 '실제 전송 켜기'.
11. **선택** — 텔레그램 알림: 사용자가 직접 `bash scripts/setup-secrets.sh` 를 돌린다 (아래 규칙).
    지식베이스: `data/knowledge/` 의 예시를 실제 자료로 바꾼다 (`data/knowledge.example/00-README.md`).

## 꼭 지킬 것

- **비밀값을 다루지 않는다.** 텔레그램 토큰·API 키는 사용자가 `scripts/setup-secrets.sh` 로 직접
  키체인에 넣는다. 채팅에 붙여 달라고 하지 말고, 붙여 넣었으면 폐기(재발급)를 권한다. 설정 파일에 쓰지 않는다.
- **카카오톡 창을 대신 열거나 앞으로 가져오지 않는다.** 사용자가 다른 일을 하는 중일 수 있다.
  필요한 창은 열어 달라고 한다. `screencapture` 를 쓰지 않는다 (화면 기록 권한 창이 뜬다) —
  화면 확인은 `--doctor` 나 AX 읽기로.
- **설정한 방과 사용자가 이름을 댄 창만 읽는다.** 다른 대화 창(1:1 등)은 열려 있어도 읽지 않는다.
- **보내는 것은 전송 허용 목록의 방뿐.** 목록에 방을 넣는 것은 사용자가 그 방을 말했을 때만.
  사람 대신 메시지를 보내는 시험은 사용자 확인을 받고 한다.
- 카카오 서버 로그인 경로(openkakao `login`, LOCO 쓰기)는 쓰지 않는다 — 계정 제재 사례가 있다.
- `config/config.toml`, `data/knowledge/`, `logs/`, `vendor/` 는 git 에 올리지 않는다 (`.gitignore`).
  방 이름·닉네임·실제 대화·강의 자료가 들어간다.

## 개발

- 시험: `.venv/bin/python -m pytest -q` (카카오톡 없이 가짜 접근성 트리로 돈다)
- 구조: `src/kakaobot/` — `service.py`(폴링·판정·답변 흐름), `kakao.py`(읽기·보내기), `thread.py`(댓글 스레드),
  `followup.py`('댓글 알림'), `photo.py`·`ocr.py`(사진 글자), `rag.py`(검색), `answering.py`·`llm.py`(답변),
  `guard.py`(출력 검증), `menubar.py`(제어판)
- 설계 기록: `docs/` — 동작을 바꾸면 해당 문서도 같이 고친다
