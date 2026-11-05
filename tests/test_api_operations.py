"""HTTP boundary tests. Providers are doubles, never live customer services."""
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient
import pytest

from app.auth import Auth
from app.core import Service
from app.integrations import MockDrafter, MockGmail
from app.main import create_app


class BoundMailbox(MockGmail):
    is_mock = False

    def __init__(self, path):
        super().__init__(path)
        self.own_email = self.account_email = 'operator@example.test'
        self.allow_send = False
        self.selection = {'configured': False, 'label': None, 'start_at': None}

    def verify(self):
        return {'read_verified': True, 'account_email': self.account_email, 'provider': 'gmail'}

    def collection_status(self):
        return {**self.selection, 'account_email': self.account_email, 'reconnect_required': getattr(self, 'reconnect_required', False)}

    def preview_collection(self, label, start_at):
        return {'label': label, 'start_at': start_at, 'count': 3}

    def configure_collection(self, label, start_at):
        self.selection = {'configured': True, 'label': label, 'start_at': start_at}
        return self.collection_status()


class FakeConnect:
    def status(self):
        return {'configured': True, 'connected': False, 'redirect_uri': 'http://testserver/api/connections/gmail/callback'}

    def authorization_url(self, state, challenge):
        assert len(challenge) == 43
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({'state': state, 'code_challenge': challenge})

    def exchange(self, code, verifier, expected_account=None):
        assert code == 'test-authorization-code'
        assert len(verifier) >= 43
        assert expected_account == 'operator@example.test'
        return {'account_email': expected_account, 'read_verified': True}


@pytest.fixture
def live_boundary(tmp_path, monkeypatch):
    path = str(tmp_path / 'app.sqlite3')
    mailbox = BoundMailbox(path)
    service = Service(path, mailbox, MockDrafter())
    auth = Auth(path)
    auth.create_user('manager', 'test-password-manager', 'admin')
    auth.create_user('staff', 'test-password-staff', 'staff')
    monkeypatch.setattr('app.google_connect.GoogleConnect', FakeConnect)
    monkeypatch.setattr('app.main.gmail_from_env', lambda path: mailbox)
    app = create_app(service, auth, scheduler=False)
    with TestClient(app) as client:
        response = client.post('/api/login', json={'username': 'manager', 'password': 'test-password-manager'})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client, app, mailbox


def test_gmail_callback_requires_original_admin_session_and_one_use(live_boundary):
    client, app, mailbox = live_boundary
    started = client.post('/api/connections/gmail/start', json={})
    assert started.status_code == 200
    state = parse_qs(urlparse(started.json()['authorization_url']).query)['state'][0]
    with TestClient(app) as second:
        assert second.get('/api/connections/gmail/callback', params={'state': state, 'code': 'test-authorization-code'}).status_code == 401
        second.post('/api/login', json={'username': 'manager', 'password': 'test-password-manager'})
        assert second.get('/api/connections/gmail/callback', params={'state': state, 'code': 'test-authorization-code'}).status_code == 409
    completed = client.get('/api/connections/gmail/callback', params={'state': state, 'code': 'test-authorization-code'}, follow_redirects=False)
    assert completed.status_code == 303
    assert completed.headers['location'] == '/#settings'
    assert client.get('/api/settings').json()['verified_this_process'] is True
    assert mailbox.allow_send is False
    assert client.get('/api/connections/gmail/callback', params={'state': state, 'code': 'test-authorization-code'}).status_code == 409


def test_collection_requires_preview_bound_to_exact_range(live_boundary):
    client, _, mailbox = live_boundary
    request = {'label': '한국갭이어/문의', 'start_at': '2026-11-05T00:00:00+00:00'}
    assert client.put('/api/connections/gmail/collection', json=request).status_code == 409
    preview = client.post('/api/connections/gmail/collection/preview', json=request).json()
    assert preview['count'] == 3
    tampered = {**request, 'label': '개인 메일', 'preview_token': preview['preview_token']}
    assert client.put('/api/connections/gmail/collection', json=tampered).status_code == 409
    assert mailbox.collection_status()['configured'] is False
    preview = client.post('/api/connections/gmail/collection/preview', json=request).json()
    result = client.put('/api/connections/gmail/collection', json={**request, 'preview_token': preview['preview_token']})
    assert result.status_code == 200
    assert result.json()['label'] == '한국갭이어/문의'
    assert client.put('/api/connections/gmail/collection', json={**request, 'preview_token': preview['preview_token']}).status_code == 409


def test_revoked_connection_reports_reconnect_and_blocks_automatic_work(live_boundary, monkeypatch):
    client, app, mailbox = live_boundary
    mailbox.configure_collection('한국갭이어/문의', '2026-11-05T00:00:00+00:00')
    assert client.post('/api/connections/verify', json={}).json()['connectivity_verified'] is True
    assert client.get('/api/connections/gmail').json()['read_verified'] is True
    app.state.service.update_settings({'live_enabled': True, 'legal_review_confirmed': True,
                                      'verification_recipient': 'check@example.test'}, 'manager')
    def revoked():
        mailbox.reconnect_required = True
        raise RuntimeError('Simulated revoked provider grant')
    monkeypatch.setattr(mailbox, 'poll', revoked)
    app.state.tick()
    status = client.get('/api/connections/gmail').json()
    assert status['connected'] is False and status['read_verified'] is False
    assert status['reconnect_required'] is True
    assert status['last_read_verified_at'] is not None
    assert status['last_sync']['ok'] is False
    assert status['error']
    assert mailbox.allow_send is False
    assert client.get('/api/operations').json()['capabilities']['auto_followup']['state'] == 'error'


def test_production_host_secure_cookie_and_legacy_account_block(tmp_path, monkeypatch):
    path = str(tmp_path / 'app.sqlite3')
    service = Service(path, MockGmail(path), MockDrafter())
    Auth(path).create_user('old-demo', 'old-demo-credential', 'admin')
    auth = Auth(path, production=True)
    auth.create_user('operator', 'unpredictable-new-credential', 'admin', production=True)
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_BASE_URL', 'https://staff.example.test')
    monkeypatch.setenv('APP_DATA_DIR', str(tmp_path))
    with TestClient(create_app(service, auth, scheduler=False), base_url='https://staff.example.test') as client:
        assert client.post('/api/login', json={'username': 'old-demo', 'password': 'old-demo-credential'}).status_code == 401
        response = client.post('/api/login', json={'username': 'operator', 'password': 'unpredictable-new-credential'})
        assert response.status_code == 200
        assert 'Secure' in response.headers['set-cookie']
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'SameSite=lax' in response.headers['set-cookie']
        assert response.headers['strict-transport-security'] == 'max-age=31536000'
        assert client.get('/api/cases').status_code == 200
        assert client.get('/health', headers={'host': 'attacker.example.test'}).status_code == 400
