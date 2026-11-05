from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest
from app.auth import Auth
from app.core import Service
from app.integrations import MockDrafter, MockGmail
from app.main import create_app


@pytest.fixture
def web(tmp_path):
    path = str(tmp_path / 'web.sqlite3')
    service = Service(path, MockGmail(path), MockDrafter(),
                      clock=lambda: datetime(2026, 11, 4, 1, tzinfo=timezone.utc))
    auth = Auth(path)
    auth.create_user('manager', 'test-password-manager', 'admin')
    auth.create_user('staff', 'test-password-staff', 'staff')
    with TestClient(create_app(service, auth, scheduler=False)) as client:
        yield client, service


def login(client, username='manager'):
    response = client.post('/api/login', json={'username': username, 'password': f'test-password-{username}'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response


def test_authentication_permissions_csrf_and_secure_headers(web):
    client, service = web
    assert client.get('/api/cases').status_code == 401
    response = login(client, 'staff')
    assert 'HttpOnly' in response.headers['set-cookie']
    assert client.get('/api/templates').status_code == 200
    assert client.get('/api/settings').status_code == 200
    assert client.post('/api/templates/S0-01/approval', json={'approved': True}).status_code == 403
    assert client.put('/api/settings', json={'live_enabled': True}).status_code == 403
    assert client.get('/api/audit').status_code == 403
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/logout').status_code == 403
    assert client.get('/').headers['x-frame-options'] == 'DENY'
    assert client.get('/').headers['cache-control'] == 'no-store'


def test_full_staff_flow_via_http_with_pending_templates(web):
    client, service = web
    login(client)
    response = client.post('/api/mock/inbound', json={'email': 'web@example.test', 'name': '가상 고객',
        'subject': '프로젝트 문의', 'body': '기간이 궁금합니다.'})
    assert response.status_code == 200, response.text
    case_id = response.json()['id']
    assert client.post(f'/api/cases/{case_id}/state', json={'state': 'S2'}).status_code == 200
    assert client.patch(f'/api/cases/{case_id}', json={'email_allowed': True,
        'consent_note': '테스트 수신 동의 확인', 'fields': {'프로젝트명': '확인된 프로젝트'}}).status_code == 200
    draft = client.post(f'/api/cases/{case_id}/draft').json()
    assert client.put(f"/api/drafts/{draft['id']}", json={'body': '확인한 기간을 안내드립니다.'}).status_code == 200
    sent = client.post(f"/api/drafts/{draft['id']}/send")
    assert sent.status_code == 200, sent.text
    assert sent.json()['status'] == 'sent'
    assert len(client.get('/api/jobs').json()) == 5
    again = client.post(f"/api/drafts/{draft['id']}/send").json()
    assert again['id'] == sent.json()['id']
    assert len(service.gmail.sent) == 1
    assert all(t['approval'] == '승인 대기' for t in client.get('/api/templates').json())
    assert client.post('/api/templates/S2-01/approval', json={'approved': True}).status_code == 200
    reply = client.post('/api/mock/inbound', json={'email': 'web@example.test',
        'thread_id': response.json()['thread_id'], 'body': '다시 상담하고 싶어요.'})
    assert reply.status_code == 200
    assert all(j['status'] == 'cancelled' for j in client.get('/api/jobs').json())


def test_admin_cannot_forge_live_verification_in_mock_mode(web):
    client, _ = web
    login(client)
    assert client.put('/api/settings', json={'connectivity_verified': True}).status_code == 409
    assert client.put('/api/settings', json={'legal_review_confirmed': True, 'live_enabled': True}).status_code == 409
    verified = client.post('/api/connections/verify')
    assert verified.status_code == 200
    assert verified.json()['connectivity_verified'] is False
    assert client.post('/api/sheets/sync').status_code == 409


def test_phone_without_email_creates_task_and_blocks_email(web):
    client, _ = web
    login(client, 'staff')
    result = client.post('/api/calls', json={'name': '전화 고객', 'phone': '010-0000-0000',
        'body': '방향을 상담합니다.', 'state': 'S0', 'followup_at': '2026-11-05T10:00:00+09:00'})
    assert result.status_code == 200, result.text
    draft = client.post(f"/api/cases/{result.json()['id']}/draft").json()
    assert client.post(f"/api/drafts/{draft['id']}/send").status_code == 409
    call = client.get('/api/calls').json()[0]
    assert client.post(f"/api/calls/{call['id']}/complete").json()['status'] == 'completed'
