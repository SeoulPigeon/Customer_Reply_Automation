"""Local HTTP integration boundaries; every provider request uses a local double."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from app.auth import Auth
from app.core import Service
from app.google_connect import GMAIL_SCOPES, GoogleConnect
from app.integrations import IntegrationError, MockDrafter, MockGmail, OpenAIDrafter
from app.main import create_app


ORIGIN = 'http://127.0.0.1:8001'
CALLBACK = ORIGIN + '/api/connections/gmail/callback'
ADMIN_CREDENTIAL = 'fresh-local-operator-credential'
STAFF_CREDENTIAL = 'fresh-local-colleague-credential'
API_KEY_SENTINEL = 'private-api-key-for-offline-test'
PROVIDER_SENTINEL = 'private-provider-body-that-must-not-reach-client'
SOURCE = Path(__file__).resolve().parents[1] / 'data' / 'followup_templates.xlsx'


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 4, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class LocalMailbox(MockGmail):
    """Live-mode boundary double; delivery still only writes the temp mock DB."""
    is_mock = False

    def __init__(self, path, connected=True):
        super().__init__(path)
        self.own_email = self.account_email = 'operator@example.test'
        self.connected = connected
        self.allow_send = False

    def verify(self):
        if not self.connected:
            raise IntegrationError('Gmail authorization is not configured')
        return {'provider': 'gmail', 'read_verified': True, 'account_email': self.account_email}

    def collection_status(self):
        return {'configured': False, 'label': None, 'start_at': None,
                'account_email': self.account_email, 'reconnect_required': False}


@pytest.fixture
def local_environment(tmp_path, monkeypatch):
    for key in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN',
                'GOOGLE_TOKEN_FILE', 'SHEETS_SPREADSHEET_ID', 'APP_BOOTSTRAP_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('APP_REQUIRE_PRODUCTION_ACCOUNTS', 'true')
    monkeypatch.setenv('APP_BASE_URL', ORIGIN)
    monkeypatch.setenv('APP_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('APP_SECRET_DIR', str(tmp_path / 'secrets'))
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.setenv('ENABLE_SCHEDULER', 'false')
    return str(tmp_path / 'data' / 'app.sqlite3')


def provision(path):
    auth = Auth(path, production=True)
    auth.create_user('operator', ADMIN_CREDENTIAL, 'admin')
    auth.create_user('colleague', STAFF_CREDENTIAL, 'staff')
    return auth


def login(client, username='operator'):
    credential = ADMIN_CREDENTIAL if username == 'operator' else STAFF_CREDENTIAL
    response = client.post('/api/login', json={'username': username, 'password': credential},
                           headers={'Origin': ORIGIN})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response


def ai_success():
    return httpx.Response(200, headers={'x-request-id': 'req-local-offline-check'}, json={
        'id': 'chat-local-offline-check', 'model': 'accessible-test-model',
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30},
        'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'body': '확인한 내용을 안내드립니다.', 'checks': []})}}],
    })


def openai(path, handler):
    return OpenAIDrafter(API_KEY_SENTINEL, 'accessible-test-model',
                        client=httpx.Client(transport=httpx.MockTransport(handler)), db_path=path)


def verification_case(service, clock):
    service.update_settings({'verification_recipient': 'check@example.test'}, 'operator')
    message = service.gmail.inject({
        'id': uuid.uuid4().hex, 'thread_id': 'local-verification-thread',
        'message_id': '<local-verification@example.test>', 'email': 'check@example.test',
        'name': '연결 확인용 가상 고객', 'subject': '연결 확인', 'body': '개별 상담 내용',
        'direction': 'inbound', 'received_at': clock().isoformat(),
    })
    case = service.ingest_message(message)
    service.mark_verification_case(case['id'], 'operator')
    service.select_state(case['id'], 'S4', 'operator')
    return service.get_case(case['id'])


def test_http_loopback_requires_fresh_accounts_without_secure_cookie(local_environment):
    path = local_environment
    service = Service(path, MockGmail(path), MockDrafter())
    legacy = Auth(path)
    legacy.create_user('shared-demo', 'previous-demo-credential', 'admin')
    old_session, _ = legacy.login('shared-demo', 'previous-demo-credential')
    provision(path)
    with pytest.raises(ValueError, match='인증'):
        create_app(service, auth=legacy, scheduler=False)
    # Auth is intentionally not injected: the application must enforce the flag.
    app = create_app(service, scheduler=False)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set('gapyear_session', old_session)
        assert client.get('/api/cases').status_code == 401
        client.cookies.clear()
        assert client.post('/api/login', json={'username': 'shared-demo',
            'password': 'previous-demo-credential'}).status_code == 401
        response = login(client)
        cookie = response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=lax' in cookie
        assert '; secure' not in cookie
        assert 'strict-transport-security' not in response.headers
        assert client.get('/api/cases').status_code == 200
        assert client.get('/health', headers={'host': 'attacker.example.test'}).status_code == 400
        assert client.post('/api/logout', headers={'Origin': 'https://attacker.example.test'}).status_code == 403
        client.headers.pop('X-CSRF-Token')
        assert client.post('/api/logout').status_code == 403
        assert client.get('/api/me').status_code == 200
        login(client, 'colleague')
        assert client.get('/api/connections/gmail').status_code == 403
        assert client.post('/api/connections/verify', json={}).status_code == 403


def test_localhost_oauth_uses_exact_callback_without_https(local_environment, monkeypatch):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'offline-google-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'offline-google-secret-sentinel')
    connection = GoogleConnect()
    assert connection.status()['configured'] is True
    assert connection.redirect_uri == CALLBACK
    parameters = parse_qs(urlparse(connection.authorization_url('one-time-state', 'pkce-challenge')).query)
    assert parameters['redirect_uri'] == [CALLBACK]
    assert parameters['state'] == ['one-time-state']
    assert parameters['code_challenge_method'] == ['S256']
    assert set(parameters['scope'][0].split()) == set(GMAIL_SCOPES)
    assert 'offline-google-secret-sentinel' not in json.dumps(parameters)


def test_local_oauth_callback_keeps_original_admin_session(local_environment, monkeypatch):
    path = local_environment
    mailbox = LocalMailbox(path)
    service = Service(path, mailbox, MockDrafter())
    auth = provision(path)
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'offline-google-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'offline-google-secret-sentinel')
    exchanged = []

    def exchange(connection, code, verifier, expected_account=None):
        assert connection.redirect_uri == CALLBACK
        assert code == 'offline-authorization-code' and len(verifier) >= 43
        assert expected_account == mailbox.account_email
        exchanged.append(code)
        return {'account_email': mailbox.account_email, 'read_verified': True}

    monkeypatch.setattr(GoogleConnect, 'exchange', exchange)
    monkeypatch.setattr('app.main.gmail_from_env', lambda path: mailbox)
    app = create_app(service, auth, scheduler=False)
    with TestClient(app, base_url=ORIGIN) as client:
        login(client)
        started = client.post('/api/connections/gmail/start', json={})
        assert started.status_code == 200
        parameters = parse_qs(urlparse(started.json()['authorization_url']).query)
        assert parameters['redirect_uri'] == [CALLBACK]
        callback = {'state': parameters['state'][0], 'code': 'offline-authorization-code'}
        with TestClient(app, base_url=ORIGIN) as anonymous:
            assert anonymous.get('/api/connections/gmail/callback', params=callback).status_code == 401
        completed = client.get('/api/connections/gmail/callback', params=callback, follow_redirects=False,
                               headers={'Origin': 'https://accounts.google.com'})
        assert completed.status_code == 303 and completed.headers['location'] == '/#settings'
        assert client.get('/api/connections/gmail').json()['read_verified'] is True
        assert client.get('/api/me').json()['username'] == 'operator'
        assert client.get('/api/connections/gmail/callback', params=callback).status_code == 409
        assert exchanged == ['offline-authorization-code']
        assert mailbox.sent == [] and not service.settings()['live_enabled']


def test_openai_probe_runs_without_gmail_and_does_not_send_customer_data(local_environment):
    path = local_environment
    calls = []

    def provider(request):
        calls.append(request)
        assert request.url.path == '/v1/chat/completions'
        payload = json.loads(request.content)
        assert 'connection_check' in payload['messages'][1]['content']
        assert 'PRIVATE_CUSTOMER_INQUIRY' not in request.content.decode()
        assert payload['store'] is False
        return ai_success()

    mailbox = LocalMailbox(path, connected=False)
    service = Service(path, mailbox, openai(path, provider))
    service.ingest_message(mailbox.receive(email='private@example.test', body='PRIVATE_CUSTOMER_INQUIRY'))
    with TestClient(create_app(service, provision(path), scheduler=False), base_url=ORIGIN) as client:
        login(client)
        result = client.post('/api/connections/verify', json={})
        assert result.status_code == 200
        data = result.json()
        assert data['gmail']['ok'] is False and data['connectivity_verified'] is False
        assert data['openai']['verified'] is True
        assert data['openai']['request_id'] == 'req-local-offline-check'
        assert len(calls) == 1
        assert client.get('/api/operations').json()['capabilities']['openai_draft']['state'] == 'normal'
        assert not service.settings()['live_enabled'] and mailbox.sent == []
        rendered = result.text + client.get('/api/settings').text + client.get('/api/operations').text + client.get('/').text
        assert API_KEY_SENTINEL not in rendered
        assert 'PRIVATE_CUSTOMER_INQUIRY' not in rendered


def test_provider_auth_error_is_safe_503_and_new_probe_recovers_status(local_environment, caplog):
    path = local_environment
    clock = Clock()
    failing = [True]

    def provider(request):
        if failing[0]:
            return httpx.Response(401, json={'error': {'code': 'invalid_api_key',
                'message': PROVIDER_SENTINEL + ' ' + API_KEY_SENTINEL}})
        return ai_success()

    mailbox = LocalMailbox(path, connected=False)
    service = Service(path, mailbox, openai(path, provider), clock=clock)
    case = verification_case(service, clock)
    with TestClient(create_app(service, provision(path), scheduler=False), base_url=ORIGIN) as client:
        login(client)
        failed = client.post(f"/api/cases/{case['id']}/draft", json={})
        assert failed.status_code == 503
        assert failed.json()['code'] == 'openai_authentication_failed'
        assert 'API 키' in failed.json()['detail']
        assert failed.json()['http_status'] == 401
        assert client.get('/api/me').status_code == 200  # Provider 401 must not log staff out.
        assert service.get_case(case['id'])['drafts'] == []  # No substitute mock response.
        assert client.get('/api/operations').json()['capabilities']['openai_draft']['state'] == 'error'
        clock.value += timedelta(seconds=1)
        failing[0] = False
        successful = client.post('/api/connections/verify', json={})
        assert successful.json()['openai']['verified'] is True
        operation = client.get('/api/operations')
        assert operation.json()['capabilities']['openai_draft']['state'] == 'normal'
        assert operation.json()['telemetry']['openai_draft']['ok'] is False
        assert operation.json()['telemetry']['openai_connection']['ok'] is True
        public = failed.text + successful.text + operation.text + caplog.text
        assert PROVIDER_SENTINEL not in public and API_KEY_SENTINEL not in public
        assert mailbox.sent == []


def test_local_live_restart_holds_elapsed_job_before_scheduler_runs(local_environment, monkeypatch):
    path = local_environment
    clock = Clock()
    mailbox = LocalMailbox(path)
    service = Service(path, mailbox, openai(path, lambda request: ai_success()), clock=clock)
    service.initialize(SOURCE)
    case = verification_case(service, clock)
    service.update_settings({'live_enabled': True, 'connectivity_verified': True,
                             'legal_review_confirmed': True}, 'operator')
    service.update_case(case['id'], {'email_allowed': True, 'consent_note': '오프라인 테스트 동의',
                                    'fields': {'프로젝트명': '확인용 프로그램', '핵심 조건': '확인 완료'}}, 'operator')
    draft = service.generate_draft(case['id'], 'operator')
    service.send_draft(draft['id'], 'operator')
    service.approve_template('S4-01', True, 'operator')
    job = service.schedule_verification_followup(case['id'], 'S4-01', clock() + timedelta(minutes=5), 'operator')
    clock.value += timedelta(minutes=10)
    baseline_sends = len(mailbox.sent)
    observed = []

    def check_due():
        saved = service.get_case(case['id'])['jobs'][0]
        assert saved['activation_review_required'] == 1
        assert saved['status'] == 'staff_check'
        assert saved['original_due_at'] == job['original_due_at']
        observed.append(saved['id'])
        return []

    monkeypatch.setattr(service, 'run_due', check_due)
    # The real lifespan runs its overdue hold; a later tick may only see held jobs.
    with TestClient(create_app(service, provision(path), scheduler=True), base_url=ORIGIN) as client:
        assert client.get('/health').status_code == 200
        check_due()
        assert service.operations_status()['scheduler_startup']['details']['held_jobs'] == 1
    assert observed and len(mailbox.sent) == baseline_sends
