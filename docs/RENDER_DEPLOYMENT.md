# Render 배포 준비와 사용자 작업

> 현재 작업에서는 Render 가입·비용 확인·배포를 진행하지 않습니다. 회사 서버 이전도 별도 작업입니다. 먼저 [맥북 실제 연결 안내](LOCAL_LIVE_CONNECTION.md)로 기존 사이트의 Gmail·OpenAI를 연결합니다. 아래는 이전에 준비한 참고 구성입니다.

현재 상태는 **Render용 코드·설정 작성 및 로컬 검증 완료, 계정·비용 승인·실제 배포 대기**입니다.
사이트 주소를 아직 발급받지 않았으며 실제 Render 실행·HTTPS·휴대폰 접속·API 연동을 확인한 상태가 아닙니다.
`onrender.com` 주소는 서비스를 실제 생성하면 Render가 정합니다. 예상 주소를 접속 주소로 안내하지 않습니다.
별도 도메인을 구매할 필요 없이 Render가 발급하는 기본 HTTPS 주소로 진행할 수 있습니다.

## 이번에 준비한 구성

- `render.yaml`: Python 웹 서비스 **1개**, 인스턴스 **1개**, Starter 계획, 영속 디스크 **1GB**.
- 시작 명령: `python -m app.render_start`. 웹사이트와 Gmail 조회·후속 예약 실행기가 같은 프로세스에서 동작합니다.
- 기본 조회·예약 간격은 기존 코드의 30초입니다. 별도 Background Worker나 Cron Job을 추가하지 않습니다.
- Python 3.12.14와 기존 `requirements.lock`을 사용합니다. 이 Python 버전의 Render 빌드 성공 여부는 실제 배포에서 확인합니다.
- `PORT`에 바인딩하며 HTTPS는 Render가 처리합니다. 앱은 실제 `RENDER_EXTERNAL_URL`을 시작 전에
  `APP_BASE_URL`로 설정하고 Secure 쿠키·CSRF·Host 검사를 유지합니다.
- 전달받은 `X-Forwarded-*`를 무조건 신뢰하지 않습니다. 앱이 사용하는 공개 출처와 HTTPS 설정은 명시적으로 지정합니다.
- GitHub 변경에 따른 자동 배포는 꺼져 있습니다. `autoDeploy: false`이며 재배포는 관리자가 직접 실행합니다.
- 인증정보가 없어도 사이트는 연결 대기로 시작합니다. 초기에는 실제 발송과 자동 후속이 활성화되지 않습니다.
- 기존 모의 데이터와 테스트 계정을 운영 디스크로 복사하지 않습니다. 운영 관리자와 직원은 별도로 만듭니다.

이 구성은 **유료 리소스를 신청하는 설정**입니다. 이 파일을 작성하거나 로컬 테스트한 것만으로 비용이 발생하지는 않습니다.
Render 계정 생성·결제 정보 등록·리소스 생성은 진행하지 않았습니다.

## 비용 확인과 승인

작성 시점에 이 개발 환경의 네트워크 정책으로 Render 공식 요금·디스크 문서를 직접 읽지 못했습니다.
따라서 월 요금을 임의로 적지 않았습니다. 아래 공식 페이지와 실제 생성 화면에서 확인합니다.

- [Render 가격](https://render.com/pricing)
- [영속 디스크 조건](https://render.com/docs/disks)
- [Blueprint 설정](https://render.com/docs/blueprint-spec)
- [Render 환경변수](https://render.com/docs/environment-variables)

확인할 비용은 **Starter 웹 인스턴스 1개 + 1GB 영속 디스크**, 적용되는 워크스페이스 요금·트래픽·세금입니다.
사용자가 확인한 `5 GB of bandwidth included`는 포함 전송량으로 디스크 저장공간과 다릅니다.
`Pro $25/mo + compute`, `Scale $499/mo + compute`도 서버 요금이 포함된 가격이 아닙니다.
우선 Hobby 워크스페이스에서 구성을 검토하며 이 앱에 Pro 조직 관리 기능은 필수가 아닙니다.
직원 사이트 로그인 계정과 Render 관리 좌석도 별개입니다. 현재 서버·디스크 단가는 미확인입니다.
OpenAI API 사용료는 Render 요금과 별개입니다. 무료 계획으로 바꾸거나 디스크를 생략해서 진행하지 않습니다.
등록 화면의 최신 가격, 결제 주기, 디스크 제한, 인스턴스 자원을 확인한 뒤 그 범위의 비용을 승인해야 실제 생성합니다.
인증정보나 신용카드 번호를 채팅으로 보내지 않습니다.

1GB는 처음 확인하기 위한 저장 크기입니다. DB와 최근 백업 7세트가 함께 들어가므로 사용량을 지켜보고 증설해야 합니다.
여러 직원이 동시에 사용할 때 메모리·응답 시간도 실제 배포에서 측정합니다. Starter가 충분하다고 미리 단정하지 않습니다.

## Render에서 서비스 만들기

1. [Render 대시보드](https://dashboard.render.com/)에 본인이 가입·로그인합니다.
2. GitHub 연결 화면에서 `SeoulPigeon/Customer_Reply_Automation` 저장소 접근을 허용합니다.
   가능하면 해당 저장소만 선택합니다. 저장소 공개 전환은 필요하지 않습니다.
3. 먼저 Render가 읽을 `review/staff-test-drive` 브랜치에 이번 `render.yaml`과 코드가 반영되어 있는지 확인합니다.
   로컬에서 수정한 파일은 GitHub에 반영하기 전까지 Render가 읽을 수 없습니다.
4. 대시보드의 **New → Blueprint**에서 저장소와 해당 브랜치를 선택합니다. 표시 이름은 바꿀 수 있습니다.
5. 생성 예정 웹 서비스 1개와 `/var/data` 영속 디스크 1GB, 유료 계획, 자동 배포 꺼짐을 검토합니다.
   서비스가 여러 개이거나 예상하지 못한 리소스가 있다면 적용하지 않습니다.
6. 비용을 승인한 뒤 Blueprint 생성·적용을 실행합니다. 비밀키는 아직 없어도 됩니다.
7. 빌드와 서비스가 **Live**가 되는지 확인합니다. 서비스 상세에 실제로 표시된 HTTPS 주소를 엽니다.
   빌드가 실패하면 로그의 오류를 먼저 해결하며 Live 또는 접속 확인 전에는 배포 완료로 판단하지 않습니다.

Blueprint를 쓰지 않고 Web Service 화면으로 직접 만들 경우에도 동일하게 설정합니다.

| 설정 | 값 |
|---|---|
| Runtime | Python |
| Branch | `review/staff-test-drive` |
| Build Command | `pip install -r requirements.lock` |
| Start Command | `python -m app.render_start` |
| Health Check Path | `/health` |
| Instance count | `1` |
| Plan | Starter, 실제 가격과 자원 확인 후 승인 |
| Persistent Disk | Mount Path `/var/data`, Size 1GB |
| Auto Deploy | Off |

환경변수는 `render.yaml`의 `envVars`와 동일하게 넣습니다. 시작 전에 영속 디스크가 반드시 있어야 합니다.
코드는 `/var/data`가 실제 마운트가 아니면 시작하지 않습니다. 임시 파일시스템에 DB를 조용히 만드는 것을 방지합니다.

## 영속 데이터와 비밀정보 위치

| 경로 | 내용 |
|---|---|
| `/var/data/app/app.sqlite3` | 상담·원문·초안·문구 승인·예약·발송·직원 계정의 기준 저장소 |
| `/var/data/app/app.sqlite3.gmail-state.sqlite3` | Gmail 계정별 승인된 수집 라벨·시작 시점 |
| `/var/data/app/app.sqlite3.ai-usage.sqlite3` | OpenAI 사용량과 제한 설정 |
| `/var/data/secrets/gmail-token.json` | Google OAuth 토큰, 파일 권한 0600 |
| `/var/data/backups/snapshot-…/` | 완료된 SQLite 백업 세트, 비밀키·토큰 제외 |

디렉터리는 0700이며 HTTP 경로로 제공하지 않습니다. `/static`에는 기존 정적 화면 파일만 있습니다.
Render 디스크를 서로 다른 서비스나 여러 인스턴스에서 공유하는 구성을 만들지 않습니다.
런타임 파일 잠금도 두 번째 앱 프로세스의 실행을 막습니다. Shell에서 `app.main` 서버를 추가로 실행하지 않습니다.

## 운영 관리자 만들기

서비스가 실행된 뒤 해당 서비스의 **Shell**을 엽니다. 맥북 터미널에서 실행하는 명령이 아닙니다.
아래 `YOUR_PERSONAL_ADMIN_ID`는 본인이 사용할 새 관리자 ID로 바꿉니다.

```bash
python -m app.cli create-user --username YOUR_PERSONAL_ADMIN_ID --role admin
```

프롬프트에 새 개인 비밀번호를 두 번 입력합니다. 비밀번호는 화면에 표시되지 않습니다.
대화에서 공유했던 테스트 계정·비밀번호를 재사용하지 않습니다. 기본 계정은 없습니다.
`APP_BOOTSTRAP_PASSWORD`를 영구 환경변수로 등록하는 방법은 사용하지 않습니다.

직원 계정은 같은 Shell에서 다음과 같이 만듭니다.

```bash
python -m app.cli create-user --username YOUR_STAFF_ID --role staff
```

관리자 설정·Gmail 연결·문구 승인은 관리자만 할 수 있습니다. 직원 로그인은 Gmail OAuth와 별개의 인증입니다.
비밀번호 교체는 `reset-password --username ID`, 계정 폐기는 `disable-user --username ID`,
전체 세션 폐기는 `revoke-sessions` 명령을 사용합니다. 비밀번호 변경·계정 폐기 때 기존 세션도 무효화됩니다.

## Gmail과 OpenAI 설정

실제 서비스 주소가 준비된 뒤 [연결 설정 문서](CONNECTION_SETTINGS.md)의 Google Cloud/OAuth 절차를 진행합니다.

1. Google Cloud에서 Gmail API를 켜고 OAuth 앱과 **Web application** 클라이언트를 만듭니다.
2. 관리자 화면의 Gmail 연결 설정에 표시되는 `redirect_uri`를 그대로 Google의 Authorized redirect URIs에 등록합니다.
   주소는 **실제 Render HTTPS 주소 + `/api/connections/gmail/callback`**입니다. 예시 주소를 등록하지 않습니다.
3. Render 서비스 **Environment → Add Environment Variable**에서 다음 값을 등록하고 저장·재배포합니다.

| 환경변수 | 등록할 값 |
|---|---|
| `GOOGLE_CLIENT_ID` | 본인이 만든 Web OAuth 클라이언트 ID |
| `GOOGLE_CLIENT_SECRET` | 같은 OAuth 클라이언트의 비밀값 |
| `GAPYEAR_OPENAI_API_KEY` | 사용할 OpenAI 프로젝트에서 생성한 API 키 |
| `OPENAI_MODEL` | 그 API 프로젝트가 실제로 사용할 수 있는 모델 ID |

비밀값은 Render 설정 화면에만 넣고 채팅·GitHub·스크린샷·실행 로그로 공유하지 않습니다.
`GOOGLE_TOKEN_FILE`은 이미 `/var/data/secrets/gmail-token.json`으로 설정되어 있으며 토큰의 실제 값은 직접 넣지 않습니다.
관리자가 사이트의 **Gmail 연결**을 눌러 Google 인증 화면에서 계정을 직접 선택하고 승인하면 서버가 저장합니다.

4. 사이트로 돌아와 실제 연결된 Gmail 주소를 확인합니다. 문의 전용 라벨과 수집 시작 시점을 선택하고,
   **범위 미리보기 → 확인·적용**으로 건수를 먼저 확인합니다. 개인 메일 전체를 수집하지 않습니다.
5. 실제 OpenAI 연결 확인을 누릅니다. 고객정보 없는 작은 생성 요청이 실제 API로 실행되고 사용량에 포함됩니다.
   성공하면 모델·요청 식별값·사용량이 표시됩니다. 설정값만 있다는 이유로 연결 성공으로 표시하지 않습니다.
6. 지정한 확인용 수신자와 제한된 검증을 완료한 뒤 관리자 화면에서 실제 운영 범위를 활성화합니다.
   아직 실제 Gmail·OpenAI 검증 기록이 없다면 운영 전환이 완료된 것이 아닙니다.

Google OAuth 앱의 Testing·게시·검증 상태는 Render 공개 사이트 여부와 다릅니다. Gmail 민감/제한 범위의
검증 및 Testing 토큰 제한은 Google 설정과 공식 안내를 확인해야 합니다. Render가 Live라고 Google 검증이 끝난 것은 아닙니다.

## 자동 백업과 복구

Render에는 기존 Linux 서버용 systemd timer가 없으므로 `app.render_start`가 백업 루프를 함께 실행합니다.
기존 DB가 있으면 `app.main`을 가져오거나 DB 구조를 갱신하기 **전에** 본 DB와 수집 범위·AI 사용량 DB를
SQLite 온라인 백업하고 무결성을 검사합니다. 이 사전 백업이 실패하면 앱을 시작하지 않으며 구조 갱신도 진행하지 않습니다.
사전 백업의 `complete.json`에는 `phase: "pre-start"`가 기록됩니다. DB가 없는 첫 설치에서는 사전 백업을 건너뜁니다.
앱 시작 후에도 한 번, 이후 기본 24시간마다 운영 중 백업을 수행합니다.
본 DB와 수집 범위·AI 사용량 DB가 모두 완료된 뒤 `snapshot-…` 디렉터리를 공개하며 사전·운영 백업을 합쳐 최근 **7세트**를 보관합니다.
실패한 백업은 완료 세트로 취급하지 않습니다. 관리자 운영 기록에 `backup`의 성공·오류 시각을 남깁니다.

이 백업은 **같은 Render 영속 디스크 안의 복구본**입니다. 디스크·계정 자체를 잃는 재해에 대비한 별도 보관소 백업은
아직 구성하지 않았습니다. 외부 백업 서비스 선택·인증·비용 승인이 필요하며 준비 없이 완료했다고 보고하지 않습니다.
Render 자체 디스크 스냅샷의 보관·복원 보장은 현재 요금·계획 문서를 확인해야 합니다.

관리자 Shell에서 즉시 별도 백업을 만들려면 다음을 실행합니다.

```bash
python -m app.cli backup --directory /var/data/manual-backups
```

복구 작업은 직원 편집을 멈추는 점검 시간에 진행합니다. 먼저 사이트 관리자에서 **실제 발송 활성화**를 끄고
**전체 자동 후속 중지**도 켭니다. 진행 중·불명확한 발송은 Gmail 기록과 대조하고, 직원이 새 상담을
등록하거나 초안을 수정하지 않게 합니다. Render Environment에서 `ENABLE_SCHEDULER=false`로 재배포하여
수집·예약 작업을 멈춘 다음 마지막 백업과 복구본 전환을 수행합니다. 이렇게 해야 복구 준비 중 생긴
새 수신·수정 내용이 전환 과정에서 빠지는 것을 방지할 수 있습니다.

실행 결과에 출력된 실제 백업 경로를 사용합니다. 자동 백업 복구 때는 실제 `snapshot-…/app.sqlite3` 파일을 사용합니다.
복구는 비어 있는 새 디렉터리로만 가능합니다. 아래 두 경로는 실제 선택한 값으로 바꿉니다.

```bash
python -m app.cli restore --input ACTUAL_BACKUP_FILE --data-dir /var/data/restored-app
```

복구 후 Render **Environment**에서 `APP_DATA_DIR=/var/data/restored-app`로 변경하고 재배포합니다.
복구본은 실제 발송·자동 후속·기존 세션이 중지되어 있습니다. 관리자 로그인, Gmail 재조회, 불명확한 발송과
상담별 예약을 확인한 뒤 필요한 범위만 재개합니다. OAuth 토큰은 DB 백업에 없으므로 필요한 경우 Gmail을 다시 연결합니다.
`ENABLE_SCHEDULER=true`로 되돌려 재배포한 뒤에도 실제 발송·자동 후속은 검토를 마칠 때까지 중지 상태로 둡니다.
문구 승인 상태는 백업에 저장된 상태 그대로 유지합니다.

## 실제 배포 후 확인할 항목

- 서비스 상세의 실제 HTTPS 주소로 로그인 화면이 열리고 인증서 오류가 없는지.
- Wi-Fi를 끈 휴대폰 등 다른 네트워크에서도 같은 주소가 열리는지.
- 비로그인 `/api/cases`, `/api/jobs`, `/api/templates` 접근이 401인지.
- `/app.sqlite3`, `/backups/`, `/.env`, `/var/data/`에서 파일이 다운로드되지 않는지.
- 직원 계정으로 관리자 연결·문구 승인 API가 거부되는지.
- Render 재시작·재배포 후 상담·저장된 초안·예약·문구 승인 상태가 유지되는지.
- 맥북을 종료해도 운영 상태의 Gmail 수신·예약 실행 성공 시각이 계속 갱신되는지.
- 백업 완료 세트가 생성되고 실제 복원 절차로 데이터와 발송 중지 상태가 확인되는지.

SQLite 단일 인스턴스 구성은 무중단 수평 확장을 지원하지 않습니다. 디스크를 쓰는 서비스의 배포·재시작 중에는
짧은 중단이 있을 수 있으며 수신·예약 작업도 그동안 멈춥니다. 복구 후 오래된 예약을 무조건 몰아서 발송하지 않습니다.
프로세스 재시작 지원과 네트워크·스토리지 가용성은 Render 서비스의 실제 상태와 약관을 기준으로 판단합니다.

공식 [Render FastAPI 예제의 render.yaml](https://github.com/render-examples/fastapi/blob/main/render.yaml)은
읽기 전용으로 확인했습니다. 여기서 Python 런타임·빌드/시작 명령·`PORT`·`autoDeploy: false` 구성을 확인했습니다.
이 확인은 본 서비스의 실제 배포 성공이나 현재 요금 검증을 뜻하지 않습니다.
