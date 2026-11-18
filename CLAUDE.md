# CLAUDE.md — 실시간 회의 번역기 PoC

## 프로젝트 개요

임직원 글로벌 회의용 실시간 번역 웹 회의방. Teams 통합이 아닌 독립 웹앱.
서버 PC 한 대에서 FastAPI를 실행하고, 같은 사내망 PC들이 HTTPS로 접속한다.

- PoC 활성 언어: 한국어, 영어, 베트남어 (상호 번역)
- 후속 확장 대상: 중국어 (언어 레지스트리 + Provider 인터페이스로 준비만)
- 출력: 음성 합성 없이 원문/번역 자막만
- 배경 조사·가격 비교는 작업장의 HTML 3개 참조. 이 문서에 옮겨 적지 않는다.

## 기술 스택

- Python 3.10+ / FastAPI / uvicorn / WebSocket
- 프런트엔드: 빌드 도구 없는 순수 HTML + CSS + JavaScript
- 음성 인식·번역: Azure Speech Translation (`azure-cognitiveservices-speech`)
- 회의 정리(요약): DeepSeek Chat Completions (`httpx` 직접 호출, 기본 모델 `deepseek-flash`, thinking 끔)
- 회의록 DOCX: `python-docx` 로 메모리에서 생성해 바로 내려줌 (`app/export_docx.py`)
- 로컬 HTTPS: `mkcert` 로 만든 사설 신뢰 인증서

## 명령어

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat                      # IP·인증서 검사, 필요 시 같은 CA로 재발급 후 HTTPS 실행
.venv\Scripts\python -m pytest -q
```

## Azure 자격증명

`.env` 는 키를 직접 담지 않고 **저장소 밖 자격증명 파일 경로**만 가리킨다.

```
AZURE_CREDENTIALS_FILE=C:\path\to\translator_api.txt
```

대상 파일은 `KEY=VALUE` 형식이며 `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` 을 둔다.
`.env` 나 환경변수에 `AZURE_SPEECH_KEY` 를 직접 넣으면 **그쪽이 우선**한다.

회의 정리용 `DEEPSEEK_API_KEY` 도 같은 규칙이다 (키 파일 또는 `.env`, `.env` 우선). 비어 있으면
서버는 뜨되 «회의 정리» 버튼이 비활성화되고 사유를 표시한다. 가짜 요약을 만들지 않는다.

파일이 없거나 키가 비어 있으면 서버는 뜨되 UI 에 `Azure 설정 필요` 와 **어느 파일의 어떤
항목이 비었는지**를 표시한다. 값 자체는 로그·UI·오류 메시지 어디에도 넣지 않는다.

## 로컬 HTTPS 설정 (사내망 2대 PC)

브라우저는 `localhost` 외의 주소에서 **HTTPS 가 아니면 마이크를 허용하지 않는다.**
서버 PC 에서 1회만 설정한다.

```
choco install mkcert            # 또는 mkcert 릴리스 바이너리 사용
mkcert -install                 # 로컬 신뢰 CA 등록 (시스템 변경 - 승인 필요)
mkcert -cert-file certs\localhost.pem -key-file certs\localhost-key.pem localhost 127.0.0.1 <서버-IP>
```

- 클라이언트 PC 에서는 서버 PC 의 `%LOCALAPPDATA%\mkcert\rootCA.pem` 을 «신뢰할 수 있는 루트
  인증 기관»에 설치해야 경고 없이 접속된다.
- `certs\` 와 `*.pem` 은 `.gitignore` 대상이다. 커밋하지 않는다.
- `run.bat` → `launcher.py`: 실제 물리 LAN의 IPv4와 인증서 SAN·서명·개인키·만료일을 검사한다.
  IP 미포함·만료 30일 이내·서버 인증서 오류 시 기존 CA로 재발급하며, 실패 시 HTTP로 전환하지 않는다.
- 기존 CA의 `rootCA.pem`과 `rootCA-key.pem`, 그리고 같은 CA의 공개 복사본 `certs\rootCA.pem`이 필요하다.
  사용자 승인으로 복사한 `certs\ca`를 우선 사용하며, 그 폴더가 없을 때만 mkcert 기본 CA 경로를 조회한다.
  `certs\ca`에는 CA 개인키가 있으므로 공유하지 않는다. 참가자에게는 `certs\rootCA.pem`만 전달한다.
  CA 누락·공개 복사본 불일치 시 중단한다. 실행기는 CA를 새로 만들거나 신뢰 저장소를 변경하지 않는다.
- 복수 물리 LAN은 사용자 선택, LAN 없음은 localhost 전용. 실행 중 IP 변경은 종료 후 재실행한다.
  8443 사용 중이면 기존 서버를 검증해 안내하며 다른 프로세스를 임의 종료하지 않는다.
- 인증서 통합 테스트는 임시 CA와 임시 포트를 사용한다. 기존 mkcert 설치가 필요하고 시스템 신뢰는 등록하지 않는다.
- 정식 운영 시에는 회사 발급 인증서로 교체한다.

## 외부망 접속 (Cloudflare Quick Tunnel)

사내망 밖 참가자는 서버 PC 에서 터널을 띄워 받은 임시 공개 URL 로 접속한다. 계정·도메인 불필요.

```
winget install Cloudflare.cloudflared          # 1회
cloudflared tunnel --url https://localhost:8443 --no-tls-verify   # run.bat 실행 후 별도 창
```

- 출력되는 `https://<무작위>.trycloudflare.com` 을 참가자에게 전달한다. 주소는 실행마다 바뀐다.
- 터널로 열 때는 `.env` 의 `ROOM_PASSCODE` 를 **반드시** 설정한다. 비어 있으면 코드 없이 입장된다.
- Quick Tunnel 은 가동 보장이 없는 테스트용이다. 정식 운영에는 쓰지 않는다.

## 코딩 규칙

- 시각은 **KST(UTC+9)** 로 표시한다. 내부 저장은 UTC 가능하나 UI/로그 출력 시 변환한다.
- 음성과 자막을 서버 디스크에 저장하지 않는다. 방 종료 시 메모리에서 제거한다.
- 비밀값은 `.env` 에만 둔다. 코드·HTML·로그·Git 대상 파일에 넣지 않는다.
- `.bat` 은 BOM 없는 순수 ASCII 로 작성한다 (`~/.claude/rules/windows-script.md`).
- 발언권·타이머의 진실 공급원은 **서버**다. 클라이언트 타이머는 표시용일 뿐 신뢰하지 않는다.

## 인계 문서

세션 인계 문서(`/handoff`)는 **작업장 루트에 `handoff.md` 로 저장한다.** OS 임시 폴더에
두지 않는다 — 다음 세션이 경로를 몰라 찾지 못하고, 임시 폴더는 정리되면 사라진다.

- 파일명은 날짜를 붙이지 말고 항상 `handoff.md` 로 고정해 **한 벌만 유지**한다.
  이전 내용이 필요하면 Git 이력에서 찾는다.
- 이미 다른 문서에 있는 내용(`PLAN.md` 실행 기록, `DESIGN.md`, 커밋 메시지)은 반복하지 말고
  경로로 참조한다.
- 키·토큰·비밀번호는 넣지 않는다. 자격증명은 **파일 경로만** 적는다.

## 반복 오류 방어 조건

- **Azure 미설정을 성공으로 표시하지 않는다.** 키가 없거나 잘못되면 UI 는 `Azure 설정 필요` 를
  표시하고 발언을 비활성화한다. 가짜/에코 번역을 자막으로 내보내지 않는다.
- **인식 결과 0건을 성공으로 보고하지 않는다.** 발화 턴마다 interim/final 건수를 로그에 남긴다
  (`~/.claude/rules/silent-failure.md`).
- **회의 정리는 Host 만, 자막 스냅샷으로, 방마다 1건씩만 진행한다.** 결과는 요청자에게만 보내고
  서버에 남기지 않는다. LLM 호출은 `run_summary` 태스크로 돌려 WebSocket 수신 루프를 막지 않는다.
- **발언권 해제 경로를 하나로 모은다.** 정상 해제·30초 초과·연결 끊김 모두 같은 함수를 거쳐야
  잠금이 남지 않는다.
- **Azure 인식·번역과 DeepSeek 정리는 개인 F0/종량제 키로 검증됐다 (2026-09-14, 한→영 실기 마이크 포함).**
  회사 구독으로 전환하거나 새 언어를 켤 때는 다시 실기 검증하고 결과를 보고에 명시한다.
