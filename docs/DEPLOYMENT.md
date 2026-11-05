# 운영 배포와 복구 안내

현재는 **배포 구성 작성 단계**입니다. 연결된 호스팅 계정이나 사용 가능한 도메인이 없어
실제 HTTPS 주소, 외부 기기 접근, 상시 실행은 아직 검증하지 않았습니다. 아래 명령은
**새로 준비한 운영 Linux 서버**에서 실행하는 설치 절차입니다. 맥북이나 이 개발 세션을
운영 서버로 간주하지 않습니다. 가입·서버 구매·도메인 구매는 수행하지 않았습니다.

## 필요한 서버

- 관리자 SSH 접근 권한이 있는 Ubuntu 24.04 이상 또는 Python 3.12+·systemd·Caddy를 지원하는 Linux VPS.
- 로컬 영속 디스크. SQLite를 임시 디스크나 NFS·여러 호스트의 공유 볼륨에 놓지 않습니다.
- 사용자가 소유한 도메인의 DNS A/AAAA를 해당 서버로 설정합니다. 방화벽은 80/443과 관리자 SSH만 엽니다.
- Google/OpenAI HTTPS 통신이 허용되어야 합니다. Gmail 권한 승인에는 사용자 브라우저의 `accounts.google.com`,
  서버에는 `oauth2.googleapis.com`, `gmail.googleapis.com`, `api.openai.com`이 필요합니다.
  선택적 Sheets 내보내기를 사용할 때만 `sheets.googleapis.com`도 허용합니다.
- 현재 개발 환경의 허용 목록에는 Google/OpenAI 목적지가 없고 인증정보도 없습니다.
  개발 세션의 제한을 우회하지 않으며, 실제 연결 시험은 준비된 운영 환경에서 수행합니다.

## 디렉터리와 서비스 설치

이 저장소의 검증된 최신 코드를 운영 서버의 `/srv/gapyear`에 배치합니다. Git 저장소는
공개로 바꾸지 않습니다. 코드 배치 시 `.env`, `var/`, `.venv/`, DB, 백업, 토큰을 복사하지 않습니다.
아래 설치 명령 전에 해당 서버에 Python 3.12 및 venv, Caddy를 설치합니다.

```bash
sudo useradd --system --home /var/lib/gapyear --shell /usr/sbin/nologin gapyear
sudo install -d -o gapyear -g gapyear -m 0700 /var/lib/gapyear /var/lib/gapyear-secrets /var/backups/gapyear
sudo install -d -o root -g root -m 0700 /etc/gapyear
cd /srv/gapyear
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.lock
sudo chown -R root:root /srv/gapyear
sudo chmod -R a+rX /srv/gapyear
sudo install -m 0600 deploy/service.env.example /etc/gapyear/service.env
sudoedit /etc/gapyear/service.env
```

기존 `gapyear` 계정이 있으면 `useradd`를 반복하지 않습니다. `service.env`에 실제 소유한 HTTPS
주소를 `APP_BASE_URL`로 설정합니다. 아직 키가 없어도 앱은 연결 대기 상태로 실행할 수 있습니다.
키는 서버의 `sudoedit` 편집기 또는 호스팅의 Secret 설정에 입력합니다. 채팅·GitHub·명령행 인자에 넣지 않습니다.
비밀 파일을 `source`해서 환경 전체를 출력하거나 `env`로 점검하지 않습니다.

```bash
sudo install -m 0644 deploy/gapyear.service /etc/systemd/system/gapyear.service
sudo install -m 0644 deploy/gapyear-backup.service /etc/systemd/system/gapyear-backup.service
sudo install -m 0644 deploy/gapyear-backup.timer /etc/systemd/system/gapyear-backup.timer
```

## 새 운영 관리자 만들기

모의 DB를 복사하지 않고 새 운영 데이터 디렉터리를 초기화합니다. 아래 `--username`에는
사용할 개인 관리자 ID를 입력합니다. 미리 만들어진 계정이나 기본 비밀번호는 없습니다.

```bash
cd /srv/gapyear
sudo -u gapyear env APP_ENV=production APP_DATA_DIR=/var/lib/gapyear APP_MAIL_MODE=live APP_AI_MODE=live .venv/bin/python -m app.cli init --username YOUR_ADMIN_ID
```

`YOUR_ADMIN_ID`는 바꿔 입력해야 하는 자리표시자입니다. 프롬프트에 새 개인 비밀번호를 두 번
입력하며 화면에는 표시되지 않습니다. 대화에 공유했던 테스트 비밀번호를 재사용하지 않습니다.
운영 로그인은 `production_ready`가 설정된 계정만 허용합니다. 기존 테스트 계정은 자동으로
운영 계정이 되지 않으며 기존 세션으로도 운영 데이터에 접근할 수 없습니다.

직원 추가는 같은 환경변수로 `create-user --username ID --role staff`, 비밀번호 교체는
`reset-password --username ID`, 계정 폐기는 `disable-user --username ID`, 전체 세션 폐기는
`revoke-sessions`를 실행합니다. 비밀번호 교체·계정 폐기는 해당 계정의 기존 세션을 무효화합니다.
이 작업은 서버 관리자만 수행하며 자유 가입 기능은 없습니다.

## HTTPS 시작

`sudoedit /etc/gapyear/caddy.env`에서 `GAPYEAR_DOMAIN=`에 실제 DNS 이름을 입력합니다.
`https://`나 경로를 붙이지 않습니다. 이 값은 비밀키가 아닙니다.

```bash
sudo chmod 0600 /etc/gapyear/caddy.env
sudo install -d /etc/systemd/system/caddy.service.d
sudo install -m 0644 deploy/caddy-environment.conf /etc/systemd/system/caddy.service.d/gapyear.conf
sudo install -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now gapyear.service gapyear-backup.timer
sudo systemctl restart caddy
sudo systemctl enable caddy
sudo systemctl status gapyear.service caddy gapyear-backup.timer --no-pager
```

다른 사이트를 이미 서비스하는 Caddy 서버에서는 기존 Caddyfile을 덮어쓰지 말고 해당 사이트
블록만 병합합니다. Caddy는 도메인과 80/443 접근을 확인한 뒤 공개 인증서를 발급합니다.
도메인이나 인증서 발급이 완료되지 않았다면 HTTPS 배포 성공으로 판단하지 않습니다.

서비스는 loopback 8000에서 웹·수신·예약 작업을 수행하고 Caddy만 외부 요청을 받습니다.
단일 서버에서 `--workers 1`을 유지합니다. `--reload`, 다른 스케줄러 프로세스 또는 여러 서버를
추가하지 않습니다. 서비스가 중지되면 systemd가 재시작하며 맥북 전원과는 무관합니다.
실제 외부 서비스 연결·발송 활성화는 [연결 설정](CONNECTION_SETTINGS.md)의 단계로 별도 확인합니다.

## 배포 확인

1. 휴대폰의 Wi-Fi를 끄고 실제 HTTPS 주소에 접속해 로그인 화면과 인증서가 정상인지 확인합니다.
2. 로그아웃 상태에서 `/api/cases`, `/api/jobs`, `/api/templates`가 401인지 확인합니다.
3. 직원 계정은 상담만, 관리자 계정은 연결·문구 승인·운영 설정까지 접근하는지 확인합니다.
4. `/var/app.sqlite3`, `/backups/`, `/.env`, `/app.sqlite3`에서 데이터나 비밀 파일이 내려받아지지 않는지 확인합니다.
5. 관리자 운영 상태에서 마지막 Gmail 수신 및 예약 실행 시각이 계속 갱신되는지 확인합니다.
6. `sudo systemctl restart gapyear` 후 저장한 상담·초안·예약이 보존되고 연결 상태를 다시 확인할 수 있는지 점검합니다.

로그인 화면과 일반 안내만 공개됩니다. 공개 서버라는 이유로 고객정보 API 인증을 제거하지 않습니다.
세션 쿠키는 HTTPS Secure·HttpOnly·SameSite와 서버 세션을 사용하고, 변경 API는 CSRF 검사를 거칩니다.
웹 루트에는 static 파일만 제공하며 데이터·비밀·백업 경로를 마운트하지 않습니다.
Uvicorn 접근 로그는 OAuth 인증 코드가 URL과 함께 기록되지 않도록 꺼져 있습니다.

## 백업

`gapyear-backup.timer`는 매일 서울 03:30 이후 SQLite 온라인 백업과 무결성 검사를 실행합니다.
실행 중인 DB 파일을 단순 복사하지 않으므로 WAL의 커밋된 변경도 포함됩니다.

```bash
sudo systemctl start gapyear-backup.service
sudo systemctl status gapyear-backup.service --no-pager
sudo systemctl list-timers gapyear-backup.timer --no-pager
```

저장 위치는 `/var/backups/gapyear`이며 파일 권한은 0600입니다. 본 DB와 메일 수집 범위·AI 사용량
보조 DB를 같은 이름 묶음으로 저장합니다. 각 DB는 일관된 스냅샷이지만 여러 DB를 통틀어 원자적
스냅샷이라고 주장하지 않습니다. 상담·예약·중복 수집/발송의 기준은 본 DB입니다.
OAuth 토큰과 API 키는 이 백업에 넣지 않습니다. 복구 후 관리자 재연결 또는 서버의 비밀 저장소를 사용합니다.

이 구성은 서버 내부의 일일 백업입니다. 서버 자체 손실에 대비한 암호화된 다른 저장소 복사,
디스크 암호화, 확정 보관 기간과 용량 알림은 호스팅 선택 후 추가해야 합니다. 이 설정만으로
오프사이트 백업까지 완료했다고 보고하지 않습니다. 오래된 백업을 자동 삭제하지 않으므로
운영자가 보관 정책과 디스크 사용량을 관리해야 합니다.

## 복원

먼저 실제 발송과 예약을 중지한 뒤 서버 서비스를 중지합니다. 복구는 **비어 있는 새 경로**만
허용하며 기존 DB를 덮어쓰지 않습니다. 백업의 본 파일과 같은 이름의 보조 DB들을 함께 둡니다.

```bash
sudo systemctl stop gapyear
sudo install -d -o gapyear -g gapyear -m 0700 /var/lib/gapyear-restored
cd /srv/gapyear
sudo -u gapyear .venv/bin/python -m app.cli restore --input /var/backups/gapyear/SELECTED_BACKUP.sqlite3 --data-dir /var/lib/gapyear-restored
```

`SELECTED_BACKUP.sqlite3`은 실제 선택한 백업 파일명으로 바꾸고 복구 폴더의 상위 경로를 서비스
사용자가 만들 수 있도록 사전에 권한을 부여합니다. 복구 과정은 로그인 세션과 연결 검증 기록을
폐기하고 실제 발송을 끄며 전체 자동 후속과 상담별 후속을 보류합니다. 문구 내용·승인 상태와
상담·발송 이력은 보존합니다. 미확정 발송은 성공으로 바꾸거나 재발송하지 않습니다.

서비스를 다시 시작하기 전에 `APP_DATA_DIR`과 systemd의 `ReadWritePaths`를 복구 경로로 함께
변경합니다. 새 로그인·Gmail 재검증·최신 메일 동기화·미확정 발송 대조·상담별 예약 검토 후에만
승인 범위의 발송을 재개합니다. 복원 시 오래된 예약을 일괄 실행하지 않습니다.

## 중지와 장애 복구

- 관리자 **전체 자동 후속 중지**: 고객 데이터나 초안을 지우지 않고 자동 후속을 멈춥니다.
- Gmail 수신 장애: 무응답 자동 후속을 보류합니다. 재연결·동기화 성공을 확인하고 검토합니다.
- OpenAI 장애: 실제 오류로 표시합니다. 모의 답변으로 대체하지 않습니다.
- 결과 불명확: 추가 발송을 보류하고 Gmail 전송 기록 확인을 사용합니다. 새 메일로 재시도하지 않습니다.
- 긴급 전체 중지: `sudo systemctl stop gapyear`를 실행합니다. 데이터는 디스크에 남습니다.
- 일반 재시작: `sudo systemctl restart gapyear`; 연결 설정을 바꾼 경우 성공 상태와 수집 범위를 다시 확인합니다.
- 서버 로그: `sudo journalctl -u gapyear --since today`; 비밀키·OAuth 코드·토큰이 들어간 외부 오류 원문은 기록하지 않습니다.
