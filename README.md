# kakaobot — 카카오톡 오픈채팅 AI 답변 봇 (macOS)

오픈채팅방에 올라온 질문에 **강의 자료(지식베이스)에 있는 내용으로만** 답하는 봇입니다.
맥 카카오톡 앱 화면을 손쉬운 사용(접근성)으로 읽고, 답은 질문 메시지의 **댓글(스레드)** 로 답니다.
자료에 근거가 없으면 지어내지 않고 "확인 후 답변드릴게요"라고 단 뒤 운영자에게 알립니다.

카카오 서버에 로그인하거나 카카오톡 데이터 파일을 읽지 않습니다. 화면에 보이는 것만 읽습니다.

## 무엇을 하나

- **질문 알아보기** — 물음표, "~되나요" 같은 질문 어미, `@운영자` 멘션, 자료와 아주 비슷한 말
- **이어 보낸 메시지는 한 질문으로** — 같은 사람이 연달아 보낸 메시지를 묶어 한 번에 답한다
- **일부만 아는 질문** — 근거가 있는 것만 답하고 "나머지는 확인 후 답변드릴게요"를 붙인다
- **스레드 안의 추가 질문** — 봇의 답 아래에 다시 물으면 같은 스레드에 답한다 ('댓글 알림' 방 이용).
  '채팅방에 함께 보내기'로 방에 올라온 댓글도 그 스레드에서 답한다
- **사진과 함께 한 질문** — 사진의 글자를 맥 안에서 읽어(Vision) 검색에 쓰고, 받은 파일은 바로 지운다
- **안전장치** — 전송 허용 목록(정확한 방 이름), 방 전체 분당 상한, 연속 오류 시 자동 정지,
  보내기 전 출력 검증(링크·전화번호·계좌 차단), 연습 모드, 긴급 정지
- **메뉴바 제어판** — 시작·정지, 긴급 정지, 연습/실제 전송, 답변 엔진 바꾸기, 오늘 처리 수
- **알림 (선택)** — 근거가 없어 답하지 못한 질문, 봇 정지를 텔레그램으로
- **답변 엔진** — `claude -p` (Claude Code 구독, 기본) · Anthropic API · 로컬 LLM (OpenAI 호환 서버)

```
카카오톡 방 창 ──접근성──▶ 1분마다 읽기 ─▶ 질문인가? ─▶ 지식베이스 검색 ─▶ LLM ─▶ 출력 검증 ─▶ 댓글로 답
                                                   └ 근거 없음 ─▶ "확인 후 답변드릴게요" + 운영자 알림
```

## 필요한 것

- macOS (Apple Silicon 에서 확인), **카카오톡 맥 앱** (25.9.x 에서 실측)
- [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/), Rust (`rustup`), Xcode 명령줄 도구
- 답변 엔진: [Claude Code](https://claude.com/claude-code) (`claude -p`, 기본) 또는 Anthropic API 키 또는 로컬 LLM
- 봇이 쓸 카카오톡 계정이 운영하는 오픈채팅방, 그리고 시험해 줄 다른 사람 한 명
  (봇은 자기 계정이 쓴 글에는 답하지 않으므로 혼자서는 시험할 수 없습니다)

## 설치

### Claude Code 로 (권장)

```bash
git clone <이 저장소 주소> kakaobot
cd kakaobot
claude
```

그리고 **"세팅해줘"** 라고 하면 됩니다. `CLAUDE.md` 의 순서대로 설치하고, 방 이름·권한·창 열기처럼
사람이 해야 하는 단계는 물어보거나 안내합니다.

### 직접

```bash
bash scripts/setup.sh                    # 도구 확인 · Python 환경 · openkakao-cli 빌드 · 설정 예시 복사
```

1. `config/config.toml` — `[room] name`(카카오톡 방 창 제목 그대로), `[trigger] mention_names`(그 방에서 내 닉네임)
2. `~/.config/openkakao/config.toml` — `[safety] allowed_send_chats` 에 같은 방 이름 (보내려면)
3. 손쉬운 사용 권한 — [docs/permissions-setup.md](docs/permissions-setup.md)
4. 카카오톡에서 그 방을 더블클릭해 **창으로** 열어 두기 (+ 스레드 추가 질문을 쓰려면 '댓글 알림' 방도)
5. 점검: `.venv/bin/python -m kakaobot --doctor`
6. 시작: `bash scripts/menubar.sh` → 화면 오른쪽 위 🤖 → **봇 시작** (처음엔 연습 모드 — 보내지 않고 기록만)
7. 지식베이스: `data/knowledge/` 의 예시 문서를 실제 자료로 바꾸기 ([형식](data/knowledge.example/00-README.md))
8. (선택) 텔레그램 알림: `bash scripts/setup-secrets.sh` — 토큰은 키체인에 들어가고 파일에는 남지 않습니다

운영 방법은 [docs/operations.md](docs/operations.md).

## 꼭 알아 둘 것

- **비공식 자동화입니다.** 카카오가 제공하는 기능이 아니라 맥 앱 화면을 자동으로 조작합니다.
  카카오톡 화면이 바뀌면 동작하지 않을 수 있고, 계정 제재 위험을 완전히 배제할 수 없습니다.
  본인 계정으로, 본인이 운영하는 방에서, 각자의 책임으로 쓰세요.
- **봇은 운영자 계정 이름으로 말합니다.** 방 공지 등으로 봇이 답한다는 것을 알려 두기를 권합니다.
- **무인 전송 패치 (선택).** 이 봇은 [openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli)를
  고쳐서 씁니다. 원래 도구는 메시지를 보낼 때마다 Touch ID/암호 승인을 받는데, 무인 봇이 되려면 이 장치를
  꺼야 합니다 (`setup.sh --allow-unattended-send`). 끄면 봇이 보내기로 정한 글은 사람이 보지 않고 나갑니다.
  이유와 남은 방어선은 [docs/patches/README.md](docs/patches/README.md) 004.
- **개인정보.** 읽기·판정·검색은 이 맥 안에서 합니다. 다만 답변 엔진이 `claude -p` 나 API 이면 질문과
  찾은 근거가 Anthropic 으로 갑니다. 처리 기록(`logs/`)에는 질문과 답이 30일, 답하지 않은 메시지는 7일 남습니다.
- 카카오톡 창이 화면에 안 보이면(닫힘·최소화·다른 데스크탑) 봇은 기다렸다가, 돌아오면 그사이 질문에 답합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/operations.md](docs/operations.md) | 메뉴바, 답하는 규칙, 폴링, 텔레그램 |
| [docs/permissions-setup.md](docs/permissions-setup.md) | 권한·창 배치 |
| [docs/followup.md](docs/followup.md) | 스레드 안의 추가 질문 |
| [docs/thread-reply.md](docs/thread-reply.md) | 댓글로 답하는 방법 (접근성 조작) |
| [docs/screenshots.md](docs/screenshots.md) | 사진 글자 읽기 |
| [docs/rag-tuning.md](docs/rag-tuning.md) | 검색 문턱 정하기 |
| [docs/context.md](docs/context.md) | 대화 맥락·기록 보존 |
| [docs/local-llm.md](docs/local-llm.md) | 로컬 LLM |
| [docs/patches/README.md](docs/patches/README.md) | openkakao-cli 패치 |

## 개발

```bash
.venv/bin/python -m pytest -q       # 카카오톡 없이 가짜 접근성 트리로 돈다
```

## 감사

카카오톡 맥 앱을 다루는 부분은 [openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli)
(JungHoonGhae, MIT)에 기대고 있습니다. 이 저장소에는 그 코드가 들어 있지 않고, 설치할 때
원본을 받아 `docs/patches/` 의 패치를 적용해 빌드합니다.

## 라이선스

[MIT](LICENSE)
