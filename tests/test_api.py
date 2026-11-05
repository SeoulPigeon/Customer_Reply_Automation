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


def select_state(client, case_id, state):
    case = client.get(f'/api/cases/{case_id}').json()
    return client.post(f'/api/cases/{case_id}/state', json={'state': state,
        'expected_revision': case['state_revision'], 'expected_generation': case['generation'], 'confirm_change': True})


def approval_data(client, case_id, draft):
    case = client.get(f'/api/cases/{case_id}').json()
    return {'expected_version': draft['edit_version'],
            'approval': {key: case[key] for key in ('email', 'subject', 'generation', 'state_revision')}}


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
    assert select_state(client, case_id, 'S2').status_code == 200
    assert client.patch(f'/api/cases/{case_id}', json={'email_allowed': True,
        'consent_note': '테스트 수신 동의 확인', 'fields': {'프로젝트명': '확인된 프로젝트'}}).status_code == 200
    draft = client.post(f'/api/cases/{case_id}/draft').json()
    edited = client.put(f"/api/drafts/{draft['id']}", json={'body': '확인한 기간을 안내드립니다.', 'expected_version': draft['edit_version']})
    assert edited.status_code == 200
    draft = edited.json()
    approved = approval_data(client, case_id, draft)
    sent = client.post(f"/api/drafts/{draft['id']}/send", json=approved)
    assert sent.status_code == 200, sent.text
    assert sent.json()['status'] == 'sent'
    assert len(client.get('/api/jobs').json()) == 5
    again = client.post(f"/api/drafts/{draft['id']}/send", json=approved).json()
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
    assert client.post(f"/api/drafts/{draft['id']}/send", json=approval_data(client, result.json()['id'], draft)).status_code == 409
    call = client.get('/api/calls').json()[0]
    assert client.post(f"/api/calls/{call['id']}/complete").json()['status'] == 'completed'


def test_revision_required_conflict_and_stale_approval(web):
    client, service = web
    login(client)
    case = client.post('/api/mock/inbound', json={'email': 'review@example.test', 'body': '문의'}).json()
    case_id = case['id']
    assert client.post(f'/api/cases/{case_id}/state', json={'state': 'S4'}).status_code == 409
    assert select_state(client, case_id, 'S4').status_code == 200
    client.patch(f'/api/cases/{case_id}', json={'email_allowed': True, 'consent_note': '검증용 동의'})
    draft = client.post(f'/api/cases/{case_id}/draft', json={}).json()
    assert client.put(f"/api/drafts/{draft['id']}", json={'body': '저장 누락 버전'}).status_code == 409
    approved = approval_data(client, case_id, draft)
    changed = client.put(f"/api/drafts/{draft['id']}", json={'body': '직원 A가 저장한 최신 답변', 'expected_version': draft['edit_version']})
    assert changed.status_code == 200
    assert client.put(f"/api/drafts/{draft['id']}", json={'body': '직원 B의 오래된 답변', 'expected_version': draft['edit_version']}).status_code == 409
    assert client.post(f"/api/drafts/{draft['id']}/send", json=approved).status_code == 409
    approved = approval_data(client, case_id, changed.json())
    client.patch(f'/api/cases/{case_id}', json={'email': 'different@example.test'})
    assert client.post(f"/api/drafts/{draft['id']}/send", json=approved).status_code == 409
    assert service.gmail.sent == []


def test_state_change_confirmation_same_state_preserves_jobs_and_queue_counts(web):
    client, _ = web
    login(client)
    case = client.post('/api/mock/inbound', json={'email': 'queue@example.test', 'body': '문의'}).json()
    case_id = case['id']
    assert select_state(client, case_id, 'S4').status_code == 200
    client.patch(f'/api/cases/{case_id}', json={'email_allowed': True, 'consent_note': '검증용 동의'})
    draft = client.post(f'/api/cases/{case_id}/draft', json={}).json()
    assert client.post(f"/api/drafts/{draft['id']}/send", json=approval_data(client, case_id, draft)).status_code == 200
    jobs = client.get('/api/jobs').json()
    assert select_state(client, case_id, 'S4').status_code == 200
    assert client.get('/api/jobs').json() == jobs
    impact = client.get(f'/api/cases/{case_id}/state-impact', params={'state': 'S3'}).json()
    assert impact['requires_confirmation'] is True
    current = client.get(f'/api/cases/{case_id}').json()
    response = client.post(f'/api/cases/{case_id}/state', json={'state': 'S3',
        'expected_revision': current['state_revision'], 'expected_generation': current['generation']})
    assert response.status_code == 409
    assert client.get('/api/jobs').json() == jobs
    assert select_state(client, case_id, 'S3').status_code == 200
    assert all(j['status'] == 'cancelled' for j in client.get('/api/jobs').json())
    metrics = client.get('/api/dashboard').json()
    for queue in ('new_inquiries', 'state_selection', 'draft_review', 'new_replies', 'today_followups', 'sent_followups', 'staff_check', 'errors'):
        assert len(client.get('/api/cases', params={'queue': queue}).json()) == metrics[queue]


def test_operations_and_privileged_controls(web):
    client, _ = web
    assert client.get('/api/operations').status_code == 401
    assert client.get('/openapi.json').status_code == 404
    login(client, 'staff')
    ops = client.get('/api/operations').json()
    assert set(ops['capabilities']) == {'gmail_receive', 'openai_draft', 'manual_send', 'auto_followup'}
    for route in ('/api/connections/gmail/start', '/api/connections/gmail/collection/preview', '/api/operations/auto-followups/pause'):
        assert client.post(route, json={}).status_code == 403
    assert client.get('/api/connections/gmail').status_code == 403
    login(client)
    assert client.post('/api/operations/auto-followups/pause', json={}).status_code == 200
    assert client.get('/api/operations').json()['capabilities']['auto_followup']['state'] == 'stopped'
    assert client.post('/api/connections/gmail/start', json={}).status_code == 409


def test_request_origin_and_private_files(web):
    client, _ = web
    login(client)
    assert client.post('/api/logout', headers={'origin': 'https://untrusted.example'}).status_code == 403
    for path in ('/app.sqlite3', '/var/app.sqlite3', '/.env', '/backups/backup.sqlite3', '/static/../app.sqlite3'):
        assert client.get(path).status_code == 404


def test_pilot_enrollment_requires_admin_and_cannot_promote_mock_cases(web):
    client, service = web
    routes = ('/api/cases/any/pilot', '/api/cases/any/pilot-followups')
    for route in routes:
        assert client.post(route, json={'enabled': True}).status_code == 401
    login(client, 'staff')
    for route in routes:
        assert client.post(route, json={'enabled': True}).status_code == 403
    login(client)
    for route in routes:
        assert client.post(route, json={'enabled': 'true'}).status_code == 409
        assert client.post(route, json={'enabled': True}).status_code == 409
    assert client.get('/api/operations').json()['pilot']['enrolled_count'] == 0
    assert service.settings()['operation_mode'] == 'verification'
