# 직접 검수 이후의 실제 연결 설정

이 문서는 현재 코드가 읽는 설정 이름과 필요한 권한을 정리합니다. 비밀값 자체는 포함하지 않습니다. 지금 검수는 `app.demo`로 진행하며, 이 실행 경로는 아래 값이 있어도 외부 발송·OpenAI 호출·Sheets 내보내기를 허용하지 않습니다. 실제 연동은 검수 이후 별도 운영 데이터 경로에서 일반 서버 `app.main`을 설정하는 작업입니다.

`.env.example`은 설명 파일입니다. 현재 앱은 `.env`를 자동으로 읽지 않습니다. 환경변수는 서버 프로세스에 직접 주입하거나 서비스 관리자의 보안 환경설정으로 전달해야 합니다. 비밀값을 명령행 인자·Git·화면 캡처·채팅에 넣지 않습니다.

## Gmail

| 설정 | 역할 |
| --- | --- |
| `APP_MAIL_MODE` | 기본 `mock`. 실제 Gmail 조회 어댑터를 사용할 때 `live`. 이 값만 바꿔도 발송 승인까지 되는 것은 아닙니다. |
| `GMAIL_ADDRESS` | 실제 OAuth 계정의 기본 메일 주소. 프로필 조회 결과와 일치해야 합니다. |
| `GMAIL_ALIASES` | 선택사항. 회사 발신 주소로 인식할 별칭을 쉼표로 구분합니다. |
| `GOOGLE_CLIENT_ID` | OAuth 클라이언트 ID. |
| `GOOGLE_CLIENT_SECRET` | OAuth 클라이언트 비밀값. |
| `GOOGLE_REFRESH_TOKEN` | 회사 계정이 필요한 범위에 동의한 뒤 발급받은 갱신 토큰. |
| `GOOGLE_TOKEN_FILE` | 선택사항. 위 OAuth 정보를 담은 보호된 JSON 파일의 경로. 저장소 밖에 보관합니다. |

Google Cloud 프로젝트에서 Gmail API를 사용 설정하고 회사 계정에 OAuth 앱 사용 권한을 부여해야 합니다. 필요한 범위는 다음 두 가지입니다.

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

현재 코드는 OAuth 최초 로그인·동의 화면이나 authorization code 교환 화면을 제공하지 않습니다. 회사 관리자가 승인한 OAuth 절차로 refresh token을 먼저 발급받아 전달해야 합니다. Workspace의 타사 앱 접근 정책과 OAuth 테스트 앱의 토큰 만료 정책도 확인합니다. Google 서비스 계정 JSON을 그대로 넣는 방식이나 서비스 계정 위임 흐름은 현재 어댑터가 구현하지 않습니다.

`GOOGLE_TOKEN_FILE`을 사용한다면 JSON 최상위에 `client_id`, `client_secret`, `refresh_token` 키를 둡니다. Google Cloud에서 다운로드한 `web` 또는 `installed` 중첩 구조의 클라이언트 파일을 그대로 읽는 형식은 아닙니다. 같은 필드의 환경변수가 있으면 파일보다 환경변수가 우선합니다. 선택적인 `access_token`, `expires_at`, `scope` 필드도 읽지만, 일시적인 액세스 토큰만 넣는 방식 대신 갱신 가능한 자격정보를 준비합니다.

관리자 화면의 연결 검증은 Gmail 프로필 조회와 계정 일치를 확인합니다. **조회 성공만으로 실제 발송 권한이 검증된 것은 아닙니다.** 실제 발송은 회사의 연락 조건 확인, 관리자 활성화, 회사가 관리하는 시험용 계정의 전송 검증을 별도로 거쳐야 합니다. 서버를 다시 시작하면 관리자 연결 검증을 다시 수행해야 합니다.

네트워크 목적지는 `oauth2.googleapis.com`과 `gmail.googleapis.com`입니다. 현재 신규 문의 수집은 최근 30일을 반복 조회하고 메시지 ID로 중복을 제거합니다. 활성 고객의 발송 직전 조회는 기간 제한이 없지만, 대량 메일 조회 한도를 넘거나 확인에 실패하면 발송을 차단합니다.

## OpenAI

| 설정 | 역할 |
| --- | --- |
| `APP_AI_MODE` | 기본 `mock`. 실제 API 초안 생성 시 `live`. |
| `GAPYEAR_OPENAI_API_KEY` | 사용할 OpenAI 프로젝트의 API 키. 이 이름을 우선 사용합니다. |
| `OPENAI_API_KEY` | 선택적인 대체 이름. `GAPYEAR_OPENAI_API_KEY`가 있으면 그 값이 우선합니다. |
| `OPENAI_MODEL` | 실제 계정에서 사용할 수 있는 모델 ID. 기본 모델을 임의로 선택하지 않습니다. |

키가 속한 프로젝트의 API 사용 권한·결제·할당량과 선택한 모델 접근 권한이 필요합니다. 현재 코드는 먼저 `GET /v1/models/{모델 ID}`를 확인하고, 초안 생성에는 `POST /v1/chat/completions`와 `response_format: {"type":"json_object"}`를 사용합니다. 따라서 선택한 모델은 **Chat Completions와 JSON 객체 응답 형식**을 지원해야 합니다. 모델 목록 조회 성공만으로 실제 초안 생성까지 검증했다고 판단하지 않습니다.

실제 생성 시 고객 문의·상담이력·직원이 입력한 확인 정보가 API에 전달됩니다. 회사의 개인정보 처리 기준에 따라 전송 범위를 확인합니다. 결과는 항상 직원이 검토하는 초안이며 모델에 고객 상태 저장이나 발송 도구 권한을 주지 않습니다. 자동 후속 문구는 OpenAI로 생성하지 않습니다.

네트워크 목적지는 `api.openai.com`입니다.

## Google Sheets

| 설정 | 역할 |
| --- | --- |
| `SHEETS_SPREADSHEET_ID` | 보고용 스프레드시트 ID. 전체 URL 대신 ID를 지정합니다. 이 값이 없으면 일반 앱에서도 연결하지 않습니다. |
| `SHEETS_TOKEN_FILE` | 선택사항. Sheets용 별도 OAuth 자격정보 JSON 파일 경로. |
| `SHEETS_CLIENT_ID` | 선택사항. Sheets용 별도 OAuth 클라이언트 ID. |
| `SHEETS_CLIENT_SECRET` | 선택사항. Sheets용 별도 OAuth 클라이언트 비밀값. |
| `SHEETS_REFRESH_TOKEN` | 선택사항. Sheets용 별도 갱신 토큰. |

별도 Sheets 설정을 선택하지 않으면 Gmail과 같은 `GOOGLE_*` 자격정보를 사용합니다. 같은 계정을 쓸 경우 그 계정의 refresh token에 Sheets 권한도 포함되어 있어야 합니다. 별도 `SHEETS_TOKEN_FILE`, `SHEETS_CLIENT_ID`, `SHEETS_REFRESH_TOKEN` 중 하나라도 설정하면 `SHEETS_*` 묶음을 선택합니다. 이 경우 누락된 개별 값을 `GOOGLE_*`에서 섞어 가져오지 않으므로 별도 자격정보 전체를 준비합니다. `SHEETS_CLIENT_SECRET`만 설정하는 것으로는 별도 묶음으로 전환되지 않습니다.

Google Cloud 프로젝트에서 Sheets API를 사용 설정하고 OAuth 계정에 해당 문서의 **편집 권한**을 부여합니다. 필요한 범위는 다음과 같습니다.

```text
https://www.googleapis.com/auth/spreadsheets
```

보고용 문서에 이름이 정확히 **고객관리**인 시트를 미리 만듭니다. 현재 관리자 동기화 버튼은 기본 범위 `'고객관리'!A1:Z5000`을 전체 교체합니다. 첫 행을 포함해 최대 5,000행이며, 보고 대상이 줄면 이전 데이터가 남지 않도록 빈 셀로 지웁니다. 이 범위에 수동 관리 자료나 별도 문구를 함께 두지 않습니다. 출력은 `RAW` 값으로 저장합니다.

연결 검증은 문서 메타데이터와 시트 목록을 조회합니다. 실제 쓰기 권한·범위는 회사 시험용 문서에서 동기화해 별도로 검증합니다. 동기화 오류가 나도 SQLite 실행 데이터와 예약을 삭제하지 않습니다. 연결 복구 후 같은 전체 보고 데이터를 다시 내보낼 수 있습니다.

현재 방향은 **SQLite → Sheets**뿐입니다. 시트에서 편집한 고객 상태나 문구를 사이트로 가져오는 기능은 없습니다. 발송 문구는 사이트의 후속 문구 관리에서 수정·승인하며 SQLite의 버전을 기준으로 실행합니다.

네트워크 목적지는 `oauth2.googleapis.com`과 `sheets.googleapis.com`입니다.

## 검수 완료 후 연결 순서

1. 모의 검수 결과를 확인하고 실제 운영용 데이터 경로·회사 인증 주체를 정합니다. 검수 DB를 실제 운영 DB로 재사용하지 않습니다.
2. 필요한 API를 사용 설정하고 위 권한을 승인받습니다. 값은 보안 환경설정에 입력합니다.
3. 일반 서버에서 Gmail 조회·모델 접근·Sheets 메타데이터 연결 검증을 수행합니다.
4. 회사가 관리하는 시험 계정과 시험 문서에서 수신·초안·스레드 답장·Sheets 쓰기를 확인합니다.
5. 회사 운영 책임자가 문구와 연락 조건을 개별 검토하고 실제 발송 전환을 결정합니다.

위 순서는 이후 작업 안내이며 이번 직접 검수 과정에서 실제 고객 발송, 운영 배포, 전체 문구 승인 또는 GitHub 푸시를 수행하지 않습니다.
