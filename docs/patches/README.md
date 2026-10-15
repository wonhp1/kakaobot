# openkakao-cli 패치

봇은 [openkakao-cli](https://github.com/JungHoonGhae/openkakao-cli) (MIT)를 고쳐서 쓴다.
upstream 을 포크하지 않고 **패치 파일 두 개**로 관리한다. `scripts/setup.sh` 가 upstream 을
아래 기준 커밋으로 받아(`vendor/openkakao-cli`) 패치를 적용하고 빌드한다.

| 파일 | 내용 |
|---|---|
| `openkakao-cli-base.patch` | 아래 001·002·003·005 — 읽기·창 찾기·보낸 사람 판별 고침 |
| `openkakao-cli-unattended.patch` | 아래 004 — 전송마다 Touch ID 승인을 끈다. ⚠️ **보안 통제 제거** — 동의한 경우에만 적용 (`setup.sh --allow-unattended-send`) |

기준 커밋: `e9d54e4308d0273e01fd900b651ad26cedc97e7c` (upstream main, 2026-09-03)

손으로 하려면:

```bash
git clone https://github.com/JungHoonGhae/openkakao-cli.git vendor/openkakao-cli
cd vendor/openkakao-cli
git checkout e9d54e4308d0273e01fd900b651ad26cedc97e7c
git apply ../../docs/patches/openkakao-cli-base.patch
git apply ../../docs/patches/openkakao-cli-unattended.patch   # 무인 전송에 동의할 때만
cargo build --release
```

004 를 적용하지 않으면 봇이 답을 보낼 때마다 Touch ID/암호 창이 뜬다 (읽기와 연습 모드는 그대로 된다).

아래는 바뀐 내용 하나하나의 이유다. 처음에는 파일 다섯 개(001~005)로 나눠 두었다가 위 두 개로 합쳤다.

## 001 · AX 스냅샷 예산

| | |
|---|---|
| 대상 | `openkakao-cli/src/ax_send.rs:250` |
| 변경 | `SNAPSHOT_MAX_DURATION: 5초 → 30초` |
| 사유 | BLOCKER-4 — 개발에 쓴 계정은 채팅방이 451개라 전체 채팅 탭 AX 순회에 63.6초 소요. 기본 5초 예산으로는 `local-send`가 스냅샷 단계에서 무조건 실패 |
| 성격 | 상수 1줄. 로직 변경 없음 |
| upstream | "AX 스냅샷 예산을 설정 가능하게" 이슈 제기 대상 |

### 측정 근거

| 탭 | 채팅방 행 수 | AX 순회 시간 |
|---|---|---|
| 오픈채팅 | 17 | 2.6초 |
| 전체 채팅 | 451 | 63.6초 |

30초는 오픈채팅 탭(2.6초)에 충분한 여유를 주면서, 전체 채팅 탭 순회(63.6초)는
여전히 차단해 "잘못된 탭에서 동작 중"임을 실패로 알려주는 값이다.



## 002 · 이미 열린 방 창 쓰기

| | |
|---|---|
| 대상 | `openkakao-cli/src/ax_send.rs` — `send_via_ax_classified`, `read_via_ax` |
| 변경 | `open_chat_row()` 무조건 호출 → **이미 열린 정확한 제목의 창이 없을 때만** 호출 |
| 사유 | KakaoTalk 25.9.1의 채팅 행(`AXRow`)과 테이블(`AXTable`)이 **AX 액션을 하나도 노출하지 않는다**. `AXPress`·`AXConfirm` 전무 → `open_chat_row`가 이 빌드에서는 절대 성공할 수 없음 |
| 성격 | **로직 변경.** 001(상수)과 달리 동작이 바뀜 |

### 안전 속성 유지 근거

원본 주석은 "동명 방 둘 중 하나가 목록 단계 모호성 검사를 우회할 수 있다"를 우려한다.
그 속성은 `find_chat_window`가 독립적으로 보장한다:

```rust
ChatMatch::Ambiguous(count) => Err(anyhow!(
    "chat name '{chat_display_name}' matches {count} open windows exactly — ambiguous, refusing to guess"
))
```

- **정확 제목 일치만** 허용 (부분 일치 불가)
- 동일 제목 창이 2개 이상이면 **거부**

따라서 패치 후에도 동명 방 보호는 그대로다.

### 전제 조건

대상 방이 **별도 창으로 열려 있어야 한다.** 이는 계획서 §7이 이미 요구하는 운영 조건과 동일하다.

### 검증

| 조건 | 결과 |
|---|---|
| 별도 창 열림 + 입력창 포커스 | **전송 성공**, 포커스 안 뺏김 |
| 창 화면 밖 (x=3000) | **전송 성공** |
| 창 최소화 | 실패 (입력창 포커스 불가) |

### upstream 이슈 제기 대상

1. AX 스냅샷 예산을 설정 가능하게 (001 관련)
2. KakaoTalk 25.9.x에서 채팅 행 AX 액션 소실 → `open_chat_row` 동작 불가 (002 관련)

## 003 · 보낸 사람·보낸 쪽 내보내기

| | |
|---|---|
| 대상 | `src/ax_send.rs` (`AxMessage`, `read_visible_messages_from_node`), `src/commands/ax_read.rs` |
| 변경 | 메시지 행에서 **발신자 이름**을 추출하고, `sender`·`outgoing`을 `--json` 출력에 노출 |
| 사유 | upstream은 `find_first("AXStaticText")`로 첫 라벨만 시각으로 쓰고 **발신자 이름을 버린다.** `outgoing`은 계산해 놓고 JSON에 안 싣는다. 이 둘 없이는 계획서 §6-1 [1] 자기 메시지 필터(무한 루프 방지)와 §6-5 사용자별 레이트 리밋이 불가능 |
| 성격 | 로직 추가. 기존 동작 파괴 없음 (필드만 추가) |

### 판별 근거

KakaoTalk은 두 `AXStaticText` 라벨을 `AXHelp` 유무로 구분한다:

```
AXStaticText value="1\n13:16"  help="2026. 9. 17."   ← 시각 (help 있음)
AXStaticText value="Kim"       help=<없음>            ← 발신자 (help 없음)
```

- `help`가 있는 라벨 → 시각
- `help`가 없는 첫 라벨 → 발신자

### 검증 결과

| 메시지 종류 | `sender` | `outgoing` |
|---|---|---|
| 봇 자신이 보낸 것 | `null` | `true` |
| 타인의 메시지 | `"Kim"` | `false` |
| 시스템 공지("…님이 들어왔습니다.") | `null` | `false` |

**시스템 공지가 `sender: null` + `outgoing: false`로 실제 질문과 구분된다.**

### upstream 이슈 제기 대상 (3건으로 갱신)

1. AX 스냅샷 예산을 설정 가능하게 (001)
2. KakaoTalk 25.9.x에서 채팅 행 AX 액션 소실 → `open_chat_row` 동작 불가 (002)
3. 그룹 채팅 발신자 이름이 AX에 있으나 추출되지 않음 + `outgoing`이 JSON에서 누락 (003)

## 004 · 전송마다 Touch ID 승인 끄기 ⚠️ 보안 통제 제거

| | |
|---|---|
| 대상 | `src/ax_send.rs` — `require_device_owner_auth` 호출 |
| 변경 | **Touch ID / 암호 승인 게이트 제거** |
| 성격 | **의도적으로 설치된 보안 경계의 제거.** 001~003과 성격이 다르다 |
| 결정 | 2026-09-17, 프로젝트 소유자가 위험을 인지하고 승인. 새로 설치하는 사람은 setup.sh 에서 따로 동의한다 |

### 왜 제거했나

upstream은 AX 전송 1건마다 사람의 Touch ID/암호 승인을 요구한다:

```
openkakao-cli에서 다음 동작을 시도함:
Approve one KakaoTalk message to '테스트방' after reviewing the terminal preview
```

사람이 직접 쓰는 CLI에는 맞지만 **무인 답변 봇과는 양립할 수 없다.**
답변 1건당 Touch ID 1회면 봇이 아니다.

우회 수단이 없다는 점도 확인했다:
- `--unattended` / `--allow-non-interactive-send` 는 **LOCO 서버 전송 경로에만** 연결되어 있고 AX 경로에는 닿지 않는다 (`main.rs:890~1085`)
- 설정 키·환경변수 없음
- 호출이 트랜스포트 안쪽에 있어 커맨드 레벨에서 건너뛸 수 없다 (upstream이 의도한 설계)

### 무엇을 포기했나

**전송 직전 사람의 내용 검토.** 파이프라인이 보내기로 정한 것은 그대로 나간다.
익명 오픈채팅 참여자의 **프롬프트 인젝션**으로 유도된 답변도 포함된다.

### 남은 방어선

| 방어 | 위치 |
|---|---|
| `safety.allowed_send_chats` — 방 1개, 정확 이름 일치 | `~/.config/openkakao/config.toml` |
| 방 바인딩 + 레이트 리밋 + 서킷 브레이커 | `src/kakaobot/pipeline.py` |
| dry-run 모드 | `config/config.toml` |
| **출력 검증** — 길이 상한, 링크 차단, 계좌·전화번호 패턴, 시스템 프롬프트 유출 탐지 | Phase 1-E |
| **RAG 근거 없으면 답하지 않고 에스컬레이션** | Phase 1-C/D |
| LLM에 도구·파일·셸 접근 미부여 | 설계 원칙 |

### 피해 범위

최대 피해는 **"바인딩된 1개 방에 텍스트 게시"** 다.
다른 방·DM·파일·네트워크에 도달할 수 없다.
다만 그 메시지는 **운영자 계정 이름으로 나가므로** 사칭 효과가 있다.

### 판단 근거

Touch ID 게이트는 **내용을 검증하지 않는다.** 사람이 미리보기를 읽고 취소하기를 기대할 뿐이고,
하루 수십 번 반복되면 읽지 않고 누르게 되어 방어력이 사실상 사라진다.
반면 **출력 검증 필터는 매번 동일하게 작동한다.** 계획서 §6-6이 지정한 방어도 이쪽이다.

**따라서 Phase 1-E의 출력 검증은 선택이 아니라 이 패치의 전제 조건이다.**


## 005 · 받은/보낸 메시지를 여백으로 가르기

| | |
|---|---|
| 대상 | `src/ax_send.rs` — `read_visible_messages_from_node` |
| 변경 | ① 받은/보낸 메시지 판정을 **말풍선 중심 → 좌우 여백 비교**로 ② 시각 라벨이 없을 때 이름을 시각으로 쓰던 대체 경로 제거 |
| 성격 | 로직 수정 (오탐 제거) |
| 사유 | 2026-09-20 실전 테스트 |

### ① 왜 중심 비교가 위험한가

카카오톡은 **받은 말풍선의 왼쪽 끝**과 **보낸 말풍선의 오른쪽 끝**을 각각 고정 여백에
붙이고, 말풍선은 거기서 가운데 방향으로 자란다. 그래서 중심은 글 길이에 따라 움직인다.

측정 (테스트방, 표 x=2554 폭=367 → 중심 2737.5):

| 메시지 | 왼쪽 끝 | 폭 | 중심 | 왼 여백 | 오른 여백 |
|---|---|---|---|---|---|
| 받은 것 (짧음) | 2618 | 22 | 2629 | 64 | 281 |
| 받은 것 (가장 김) | 2618 | 222 | **2729** | 64 | 81 |
| 보낸 것 (짧음) | 2832 | 67 | 2865 | 278 | 22 |
| 보낸 것 (김) | 2635 | 264 | 2767 | 81 | 22 |

가장 긴 받은 메시지의 중심이 표 중심을 **8px 차이로** 겨우 밑돈다. 조금만 더 길면
**상대의 질문을 내 메시지로 읽어 조용히 무시한다.** 여백 비교는 길이와 무관하게
왼쪽 64 / 오른쪽 22라는 고정값만 보므로 안전하다.

### ② 시각 라벨

`help` 를 가진 라벨만 시각이다. 기존 대체 경로는 첫 라벨의 **값**을 시각으로 썼고,
이름만 있고 시계가 없는 행(묶음의 첫 줄)에서 `time="Kim"` 처럼 발신자 이름이
시각 자리에 들어갔다.


### upstream 이슈 제기 대상 (4건으로 갱신)

4. 긴 수신 메시지가 발신 메시지로 분류됨 — 말풍선 중심 기준 판정 (005)
