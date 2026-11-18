# PLAN.md — 실시간 회의 번역기 PoC

## 목표

한국어↔영어 실시간 회의 번역 웹 회의방을 사내망에서 동작하는 형태로 구현한다.
Azure 구독이 없는 상태에서도 앱 전체 흐름(방·참가자·발언권·타이머·자막 파이프라인)을
검증할 수 있어야 하며, 자격증명이 준비되면 키만 넣어 실제 번역이 켜져야 한다.

## 성공 기준

인계 문서 `translator-poc-handoff-2026-09-07.md` 의 8개 기준을 그대로 사용한다.
요약: (1) 2 클라이언트 방 공유 (2) 발언 중 타인 PTT 잠금 (3) 25초 경고/30초 자동 종료
(4) interim/final 분리 및 순서 보존 (5) 키 없을 때 `설정 필요` 명시 (6) 영구 저장 없음
(7) 비밀값 유출 없음 (8) 실제 Azure 번역 검증은 구독 확보 후 별도 수행.

## 단계별 계획

| # | 단계 | 검증 방법 |
|---|------|-----------|
| 1 | Git 저장소·브랜치·`.gitignore` | `git check-ignore -v .env` 로 제외 확인 |
| 2 | CLAUDE.md / PLAN.md / DESIGN.md | 파일 존재 |
| 3 | `.env.example`, `requirements.txt` | 변수명만 있고 값 없음 확인 |
| 4 | 설정·언어 레지스트리 (`app/config.py`) | 단위 테스트: 미설정 시 `configured=False` |
| 5 | 방/참가자/Host/발언권 상태 (`app/rooms.py`) | 단위 테스트: 잠금·해제·중복 요청 거부 |
| 6 | 30초 제한 + 25초 경고 타이머 | 단위 테스트: 짧은 타임아웃 주입해 자동 해제 확인 |
| 7 | Provider 인터페이스 + Azure 구현 + 미설정 Provider | 단위 테스트: 미설정 Provider 가 audio 거부 |
| 8 | WebSocket 이벤트 라우팅 (`app/main.py`) | `TestClient` 로 join→floor→release 시퀀스 검사 |
| 9 | 프런트엔드 (동의/입장 화면, 회의 화면) | Edge/Chrome 수동 검증 |
| 10 | 실행 스크립트 + 로컬 HTTPS 절차 | `.bat` ASCII/BOM 정적 검사 후 실행 |
| 11 | 전체 검증 및 실행 기록 | 아래 «실행 기록» 에 결과와 미검증 항목 기재 |

## 실행 기록

### 2026-09-07 (KST)

- 1단계 완료. `git init` + `claude/realtime-translator-poc` 브랜치 생성.
  `.env` 가 `.gitignore:2` 로 제외됨을 `git check-ignore -v` 로 확인.
- 이전 세션에서 만들어진 `.env` 가 작업장에 이미 존재했다(내용 미확인, 미출력).
- 2단계 완료. CLAUDE.md / PLAN.md / DESIGN.md 작성.
- 3~10단계 구현 완료. Python 3.14.6 venv 에 의존성 설치
  (`azure-cognitiveservices-speech==1.42.0` 도 3.14 win_amd64 휠 설치 확인).
- **자동 테스트 23개 전부 통과** (`pytest -q`). 커버 범위:
  방 코드 정규화, Host 배정, 발언권 단독 점유·중복 요청 거부·비보유자 해제 거부,
  발언자 연결 종료 시 잠금 해제, 자막 순서·KST 시각, 방 종료 시 자막 삭제,
  Azure 미설정 시 `floor_denied`, 비보유자 오디오 폐기, Host 만 회의 종료,
  마지막 참가자 퇴장 시 방 제거, **제한 시간 자동 종료(경고→강제 해제)**.
  타이머 테스트는 `FLOOR_LIMIT_SECONDS=2 / WARNING=1` 을 주입해 2초 안에 검증한다.
- 브라우저 수동 검증(Chromium 계열 in-app 브라우저, 1280x900):
  입장 화면 «Azure 설정 필요» 빨간 배지 + 사유 표시, 동의 체크 전 입장 버튼 비활성,
  대소문자 다른 방 코드(`TEAM1`/`team1`)로 같은 방 공유 및 «참가자 2명» 동기화,
  Host 에게만 TXT/JSON/회의 종료 노출, Host 종료 시 양쪽 모두 종료 상태 전환.
- 실행 중 발견해 고친 결함 2건:
  1. `.center-view` / `#meeting-view` 의 `display:flex` 가 `[hidden]` 을 덮어써
     입장 후에도 입장 화면이 그대로 보였다 → `[hidden]{display:none!important}` 추가.
  2. Host 종료 후에도 WebSocket 이 열려 있어 «연결됨» 으로 표시됐다 → 클라이언트에서 close.
- 로그 시각을 `KstFormatter` 로 KST 고정. 실제 출력 예: `2026-09-07 22:49:21 KST INFO ...`.
- 작업장에 이미 있던 `.env` 에는 `AZURE_SPEECH_KEY`/`AZURE_SPEECH_REGION` 이 둘 다 없다
  (`/api/status` 응답으로 확인, 내용은 출력하지 않음). 따라서 앱은 미설정 경로로 동작했다.

- 자격증명 참조 방식 변경: `.env` 는 키를 담지 않고 `AZURE_CREDENTIALS_FILE` 로
  `C:\path\to\translator_api.txt` 를 가리킨다. `config.read_credentials_file()`
  이 `KEY=VALUE` 를 읽고, 환경변수 직접 지정이 파일보다 우선한다.
  테스트 5개 추가 → **총 28개 통과**. 실효값 확인 결과 `AZURE_SPEECH_REGION` 은 파일에서
  12자로 채워지고 `AZURE_SPEECH_KEY` 는 길이 0이라 `Azure 설정 필요` 가 유지된다.
  UI 사유 문구가 **파일 경로와 비어 있는 항목명**을 정확히 지목함을 확인했다
  (`... translator_api.txt 에 AZURE_SPEECH_KEY 값이 비어 있습니다.`).

### 2026-09-08 (KST) — Azure 키 확보 후 실검증

- 자격증명 파일에 키가 채워졌다. 실효값: `AZURE_SPEECH_KEY` 84자, `AZURE_SPEECH_REGION`
  `koreacentral`. 상태 `configured`.
- **인증 확인**: `POST https://koreacentral.api.cognitive.microsoft.com/sts/v1.0/issueToken`
  → **HTTP 200**. 키·지역 조합이 유효하다.
- **음성 인식·번역 실검증** (마이크 대신 Azure TTS 로 한국어 음성을 합성해 Provider 에 주입):
  ```
  final 원문 : 안녕하세요.                    final 번역 : {'en': 'Hello.'}
  final 원문 : 오늘 회의를 시작하겠습니다.      final 번역 : {'en': "Let's begin today's meeting."}
  ```
  interim 4건 / final 2건. 검증 스크립트는 scratchpad 에만 두고 프로젝트에 남기지 않았다.
- **서버 전체 경로 실검증**: 실행 중인 서버에 WebSocket 으로 접속해 같은 음성을 스트리밍
  → `welcome(연결됨)` → 발언권 획득 → interim 3건 → final 2건 자막 수신 → `floor_released`.
  자막 시각이 KST 로 기록됨(`00:06:18 KST`). 서버 로그에 턴별 건수 기록 확인:
  `[azure] 턴 종료 - 언어=ko 오디오=146802바이트 interim=3건 final=2건`.
- **30초 제한 기본값 실측**: `25.0s floor_warning(남은 5초)` → `30.0s floor_released(time_limit)`.

#### 실측에서 드러난 결함 1건 (수정 완료)

`stop_turn()` 이 `timer.cancel()` 로 **타이머 태스크 자신을 취소**해, 제한 시간 도달 시
`await session.close()` 에서 CancelledError 가 나고 `floor_released` 브로드캐스트에
도달하지 못했다. 25초 경고는 정상이라 «부분적으로 동작하는» 형태였다.

자동 테스트가 이를 놓친 이유: `FakeSession.close()` 가 실제 대기 지점을 만들지 않아
취소가 전달되지 않았다. **축소된 테스트 더블이 실패 경로를 지운 사례**다.

- 수정: `timer is not asyncio.current_task()` 일 때만 취소한다 (`app/main.py`).
- 회귀 테스트 보강: `FakeSession.close()` 에 `await asyncio.sleep(0.01)` 로 취소 지점을 만들고,
  해제 여부를 **서버 상태 폴링으로 먼저 단언**한다. 이 단언이 없으면 결함 시 테스트가
  실패하지 않고 **영원히 대기**했다(실측 확인). 수정 전 6.5초 만에 실패, 수정 후 통과.
- 현재 **테스트 28개 전부 통과**.

### 미검증 항목 (누적)

- ~~실제 Azure 음성 인식·번역~~ → **2026-09-08 검증 완료** (위 실행 기록 참조).
- **사내망 2대 PC 간 HTTPS 접속**: 서버 PC 1대 + 클라이언트 PC 1대 실환경 필요.
  `mkcert` 설치는 시스템 신뢰 저장소를 바꾸므로 사용자 승인 후 실행한다.
- **마이크 캡처(AudioWorklet → 16kHz PCM)**: 자동화 브라우저에 마이크가 없어 실행하지 못했다.
  Edge/Chrome 실기에서 1회 확인 필요.
- ~~30초 제한의 실제 동작~~ → **2026-09-08 실측 완료** (25.0s 경고 / 30.0s 자동 종료).
- **브라우저 UI 의 자막 렌더**: 서버가 보내는 `interim`/`final` 메시지는 실데이터로 확인했으나,
  그것이 화면에 그려지는 것은 마이크 실기 검증에서 함께 확인해야 한다.
- **Host 회의록 TXT/JSON 내보내기**: 실제 자막이 쌓인 상태에서 내려받아 보지 않았다.

### 2026-09-08 (KST) — 방문 시연용 HTTPS 실행기

- 승인 범위: 실행 시 IP·인증서 검사, 동일 CA 자동 재발급, 중복 서버 처리, 테스트·안내서 갱신.
- `run.bat`은 ASCII/CRLF 호출부를 유지하고 `launcher.py`가 처리한다. 추가 라이브러리 없이
  Python 표준 SSL 기능으로 실제 TLS 검증, PowerShell로 물리 LAN 조회를 수행한다.
- IP 미포함·만료 30일 이내·서버 인증서 오류 때만 재발급한다. 공개 배포본과 CA를 대조하고,
  CA 누락/변경 시 중단한다. 신규 CA 생성·신뢰 저장소 변경·HTTP 자동 전환은 하지 않는다.
- 임시 경로에서 발급 후 TLS 서명·개인키·주소·기간을 검증하고 교체한다. 파일 교체 오류는
  메모리에 보관한 원본으로 복구한다. Windows 파일 잠금으로 동시 실행 중 인증서 교체를 막는다.
- 검증: 기존 28개 + 실행기 18개 = **46개 통과**. 실제 격리 CA 재발급, CA 불변,
  잘못된 키, 생성 실패/잘못된 생성 결과, 두 번째 파일 교체 실패 복구, 만료 경계,
  다중 LAN 선택/조회 실패, 중복 실행, 임시 포트 HTTPS 시작·응답·자식 프로세스 종료 포함.
- 실환경 읽기 확인: 물리 Wi-Fi `<서버-IP>` 탐지, 기존 인증서 SAN 일치.
  기존 8443 서버를 검증해 브라우저를 열었으며 운영 인증서 해시가 변경되지 않았음을 확인.
- 최종 검사: 실행기 18개 재통과. `C:\Windows\System32`에서 run.bat을 cp949/65001 각각 실행해
  정상 종료 코드 0과 기존 서버 재사용 확인. BAT ASCII·BOM 없음·CRLF, 안내서 목차 8개 정상 확인.
- 제한: 실제 회사 간 이동/여러 물리 LAN 환경과 참가자 PC 접속은 미검증이다.
  IP 변경 시 기존 서버를 종료하고 run.bat을 다시 실행해야 한다.

### 2026-09-08 (KST) — 사용자 실행의 CA 경로 오류 보완

- 도구에서는 AppData CA가 보이지만 사용자 탐색기·실행기에서 읽지 못하는 현상 재현 보고.
  환경 간 접근 차이의 근본 원인은 미확정이며, 오류에 실제 경로와 접근 거부 여부를 추가했다.
- 개인키 복사·권한 적용은 자동 승인 검토가 차단해 실행하지 않았고, 이후 사용자의 명시적 승인을 받았다.
- 기존 CA 두 파일을 `certs\ca`로 복사하고 원본 ACL을 적용해 상위 폴더 권한 상속을 차단했다.
  공개 인증서·개인키의 해시 일치, 원본 보존, Git 제외를 확인했다. 배포용 공개 인증서는 유지했다.
- 실행기는 certs\ca 우선, 해당 폴더 자체가 없을 때만 기본 CA 경로 조회. 폴더 내 파일 누락은
  다른 CA로 전환하지 않고 중단한다. 재발급 시 기존 CAROOT 지정 로직을 그대로 사용한다.
- 전체 테스트 **48개 통과**. AppData 조회 없이 보호 폴더 사용, 누락 시 중단, 접근 거부 진단 포함.
