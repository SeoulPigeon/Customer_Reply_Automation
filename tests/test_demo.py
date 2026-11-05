"""Staff test-drive acceptance checks; no actual provider or customer is contacted."""
from datetime import timedelta
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.core import Service, dt, SEOUL
from app.demo import Demo, INQUIRY, INITIAL_TIME
from app.integrations import MockDrafter, MockGmail
import app.main as web


ACTOR = 'demo-staff'


@pytest.fixture
def demo(tmp_path):
    return Demo(tmp_path / 'demo')


def send_initial(demo, case_id=None):
    service = demo.service
    case_id = case_id or demo.info()['case_id']
    service.select_state(case_id, 'S4', ACTOR)
    draft = service.generate_draft(case_id, ACTOR)
    service.update_draft(draft['id'], '직원이 수정한 검수용 답장입니다. 비용과 가능 여부는 확인 후 안내드립니다.', ACTOR)
    sent = service.send_draft(draft['id'], ACTOR)
    assert sent['status'] == 'sent'
    return case_id, sent


def jobs(demo, case_id):
    return sorted(demo.service.get_case(case_id)['jobs'], key=lambda row: row['step'])


def test_exact_inquiry_state_unselected_five_test_approvals_and_no_live_changes(tmp_path):
    production_path = str(tmp_path / 'production.sqlite3')
    production = Service(production_path, MockGmail(production_path), MockDrafter())
    demo = Demo(tmp_path / 'demo')
    info = demo.info()
    case = demo.service.get_case(info['case_id'])
    assert case['state'] is None
    assert case['history'][0]['body'] == INQUIRY
    assert INQUIRY == '몰타 어학연수를 1월에 3주 정도 가려고 합니다. 참가 가능한가요? 비용도 궁금합니다.'
    assert case['email'].endswith('@example.test')
    assert case['drafts'] == [] and case['jobs'] == []
    assert info['approved_templates'] == [f'S4-{i:02}' for i in range(1, 6)]
    assert len([t for t in demo.service.list_templates() if t['approval'] == '승인 대기']) == 30
    assert all(t['approval'] == '승인 대기' for t in production.list_templates())
    assert not demo.service.settings()['live_enabled']
    assert demo.service.gmail.is_mock and demo.service.drafter.is_mock
    assert dt(info['now']) == dt(INITIAL_TIME)
    assert info['base_sent_at'] is None


def test_refuse_existing_unmarked_database_without_modification(tmp_path):
    path = tmp_path / 'app.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE original(value TEXT)')
        db.execute("INSERT INTO original VALUES('preserve')")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(ValueError, match='기존 데이터베이스'):
        Demo(tmp_path)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not (tmp_path / '.demo-only').exists()


def test_cannot_advance_before_staff_state_and_send(demo):
    with pytest.raises(ValueError, match='S4'):
        demo.advance(1, None, ACTOR)
    demo.service.select_state(demo.info()['case_id'], 'S4', ACTOR)
    with pytest.raises(ValueError, match='후속 주기'):
        demo.advance(1, None, ACTOR)


def test_mock_draft_is_explicit_and_does_not_claim_availability(demo):
    case_id = demo.info()['case_id']
    demo.service.select_state(case_id, 'S4', ACTOR)
    draft = demo.service.generate_draft(case_id, ACTOR)
    assert draft['provider'] == 'mock'
    assert any('모의 응답' in check and '실제 GPT' in check for check in draft['checks'])
    assert '몰타' in draft['body']
    assert '확정하기는 어렵습니다' in draft['body']
    assert len(demo.service.gmail.sent) == 0


def test_staff_can_check_each_s4_day_and_weekend_deferral(demo):
    case_id, sent = send_initial(demo)
    base = dt(sent['sent_at'])
    schedules = jobs(demo, case_id)
    assert [row['template_id'] for row in schedules] == [f'S4-{i:02}' for i in range(1, 6)]
    assert [dt(row['original_due_at']) - base for row in schedules] == [timedelta(days=d) for d in [1, 3, 7, 14, 30]]
    for index, day in enumerate([1, 3, 7, 14, 30], start=1):
        result = demo.advance(day, case_id, ACTOR)
        assert dt(result['target_at']) == base + timedelta(days=day)
        assert dt(result['effective_at']) >= dt(result['target_at'])
        assert len(demo.service.gmail.sent) == index + 1
        assert sum(j['status'] == 'sent' for j in jobs(demo, case_id)) == index
        assert demo.info(case_id)['base_sent_at'] == sent['sent_at']
        if day == 3:
            assert dt(result['target_at']).astimezone(SEOUL).weekday() == 6
            assert dt(result['effective_at']).astimezone(SEOUL).weekday() == 0
            assert dt(result['effective_at']).astimezone(SEOUL).hour == 9


def test_direct_jump_30_simulates_intervening_jobs_once(demo):
    case_id, sent = send_initial(demo)
    demo.advance(30, case_id, ACTOR)
    current = jobs(demo, case_id)
    assert all(job['status'] == 'sent' for job in current)
    assert len(demo.service.gmail.sent) == 6
    assert len({job['sent_at'] for job in current}) == 5
    assert [job['sent_at'] for job in current] == [job['due_at'] for job in current]
    assert demo.info()['base_sent_at'] == sent['sent_at']
    demo.service.run_due()
    assert len(demo.service.gmail.sent) == 6


def test_reply_cancels_remaining_requires_reselection_and_new_cycle(demo):
    case_id, sent = send_initial(demo)
    demo.advance(1, case_id, ACTOR)
    reply = demo.reply(case_id, '다른 시작 날짜로 다시 문의합니다.', ACTOR)
    assert reply['workflow'] == 'state_selection'
    old = jobs(demo, case_id)
    assert old[0]['status'] == 'sent'
    assert all(row['status'] == 'cancelled' for row in old[1:])
    with pytest.raises(ValueError):
        demo.service.generate_draft(case_id, ACTOR)
    with pytest.raises(ValueError, match='후속 주기'):
        demo.advance(3, case_id, ACTOR)
    send_initial(demo, case_id)
    current = demo.service.get_case(case_id)['jobs']
    assert len(current) == 10
    assert len({job['cycle_id'] for job in current}) == 2
    assert sum(job['status'] == 'pending' for job in current) == 5


def test_restart_keeps_clock_history_and_admin_revocation(demo):
    case_id, sent = send_initial(demo)
    demo.advance(1, case_id, ACTOR)
    demo.service.approve_template('S4-02', False, ACTOR)
    before = demo.info()
    restarted = Demo(demo.folder)
    assert restarted.info()['now'] == before['now']
    assert restarted.info()['case_id'] == case_id
    assert len(restarted.service.list_cases()) == 1
    assert 'S4-02' not in restarted.info()['approved_templates']
    assert len(restarted.service.gmail.sent) == 2


def test_new_scenario_preserves_old_history_and_clock_never_reverses(demo):
    original, _ = send_initial(demo)
    demo.advance(7, original, ACTOR)
    with pytest.raises(ValueError, match='과거'):
        demo.advance(1, original, ACTOR)
    snapshot = demo.service.get_case(original)
    created = demo.scenario(ACTOR)
    assert created['case_id'] != original
    assert len(demo.service.list_cases()) == 2
    assert demo.service.get_case(original)['history'] == snapshot['history']
    assert demo.service.get_case(created['case_id'])['state'] is None
    assert demo.info()['case_id'] == created['case_id']


def test_demo_http_never_constructs_external_adapters_even_with_live_env(demo, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Demo attempted to construct a live provider')
    monkeypatch.setenv('APP_MAIL_MODE', 'live')
    monkeypatch.setenv('APP_AI_MODE', 'live')
    monkeypatch.setenv('SHEETS_SPREADSHEET_ID', 'test-sheet-id')
    monkeypatch.setattr(web, 'sheets_from_env', forbidden)
    monkeypatch.setattr(web, 'gmail_from_env', forbidden)
    monkeypatch.setattr(web, 'drafter_from_env', forbidden)
    demo.auth.create_user('reviewer', 'demo-test-password-123', 'admin')
    with TestClient(web.create_app(demo.service, demo.auth, scheduler=True, demo=demo)) as client:
        login = client.post('/api/login', json={'username': 'reviewer', 'password': 'demo-test-password-123'})
        assert login.status_code == 200
        client.headers['X-CSRF-Token'] = login.json()['csrf_token']
        info = client.get('/api/demo').json()
        assert info['enabled'] and not info['actual_sending']
        assert client.get('/api/settings').json()['demo']
        assert client.post('/api/sheets/sync').status_code == 409
        assert client.post('/api/connections/verify').json()['connectivity_verified'] is False
        assert client.put('/api/settings', json={'live_enabled': True, 'legal_review_confirmed': True}).status_code == 409
        assert client.post('/api/demo/advance', json={'day': 1, 'case_id': info['case_id']}).status_code == 409
        assert client.post('/api/demo/scenario').status_code == 200


def test_demo_controls_absent_from_regular_application(demo):
    demo.auth.create_user('staff', 'demo-test-password-123', 'staff')
    with TestClient(web.create_app(demo.service, demo.auth, scheduler=False)) as client:
        login = client.post('/api/login', json={'username': 'staff', 'password': 'demo-test-password-123'})
        client.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert client.get('/api/demo').json() == {'enabled': False}
        assert client.post('/api/demo/advance', json={'day': 1}).status_code == 404
        assert client.post('/api/demo/reply', json={}).status_code == 404
        assert client.post('/api/demo/scenario').status_code == 404
