"""Regression and production-boundary checks; no test contacts an external API."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest

from app.core import DraftConflict, Service
from app.integrations import MockDrafter, MockGmail
from test_workflows import env, prepare, first_reply, inbound, jobs_for, ACTOR, ADMIN, Clock


def test_same_state_save_preserves_jobs_anchor_and_edited_draft(env):
    service, _, clock, _ = env
    case = prepare(env, 'S4')
    first_reply(env, case)
    baseline = jobs_for(service, case['id'])
    state_before = service.get_case(case['id'])
    clock.at(clock() + timedelta(minutes=5))
    saved = service.select_state(case['id'], 'S4', ACTOR,
                                 expected_revision=state_before['state_revision'],
                                 expected_generation=state_before['generation'], confirm_change=False)
    assert saved['state_revision'] == state_before['state_revision']
    assert jobs_for(service, case['id']) == baseline
    assert service.preview_state_change(case['id'], 'S4')['affected_jobs'] == []
    draft = service.generate_draft(case['id'], ACTOR)
    edited = service.update_draft(draft['id'], '직원이 저장한 편집본', ACTOR, draft['edit_version'])
    service.select_state(case['id'], 'S4', ACTOR)
    assert service.generate_draft(case['id'], ACTOR) == edited
    assert jobs_for(service, case['id']) == baseline


def test_real_state_change_previews_and_requires_current_confirmation(env):
    service = env[0]
    case = prepare(env, 'S1')
    first_reply(env, case)
    preview = service.preview_state_change(case['id'], 'S4')
    assert preview['changed'] and preview['requires_confirmation']
    assert len(preview['affected_jobs']) == 5
    with pytest.raises(DraftConflict):
        service.select_state(case['id'], 'S4', ACTOR, confirm_change=False)
    service.select_state(case['id'], 'S4', ACTOR, confirm_change=True)
    assert all(j['status'] == 'cancelled' for j in jobs_for(service, case['id']))
    assert len(jobs_for(service, case['id'])) == 5
    assert service.get_case(case['id'])['drafts'][0]['status'] == 'sent'


def test_autosave_survives_consultation_save_restart_and_detects_conflict(env):
    service, gmail, _, db = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    saved = service.update_draft(draft['id'], '저장할 고객 답변', ACTOR, draft['edit_version'])
    service.update_case(case['id'], {'notes': '상담 정보만 저장'}, ACTOR)
    reopened = Service(db, gmail, MockDrafter())
    assert reopened.get_case(case['id'])['drafts'][0]['body'] == saved['body']
    with pytest.raises(DraftConflict):
        service.update_draft(draft['id'], '다른 창의 오래된 수정본', ACTOR, draft['edit_version'])
    assert service.get_case(case['id'])['drafts'][0]['body'] == '저장할 고객 답변'


def test_two_simultaneous_autosaves_have_one_winner(env):
    service = env[0]
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    def save(body):
        try:
            return service.update_draft(draft['id'], body, ACTOR, draft['edit_version'])['body']
        except DraftConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ['직원 A 수정', '직원 B 수정']))
    assert outcomes.count('conflict') == 1
    assert service.get_case(case['id'])['drafts'][0]['body'] in outcomes


def test_inbound_preserves_stale_editor_text_and_requires_fresh_generation(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    saved = service.update_draft(draft['id'], '보존할 직원 수정본', ACTOR, draft['edit_version'])
    before = service.get_case(case['id'])
    clock.at(clock() + timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body='새 질문입니다.'))
    stale = service.get_case(case['id'])['drafts'][0]
    assert stale['body'] == saved['body'] and stale['status'] == 'stale'
    with pytest.raises(DraftConflict):
        service.update_draft(draft['id'], '새 입력', ACTOR, saved['edit_version'])
    with pytest.raises(DraftConflict):
        service.select_state(case['id'], 'S2', ACTOR, expected_generation=before['generation'])
    service.select_state(case['id'], 'S2', ACTOR)
    fresh = service.generate_draft(case['id'], ACTOR)
    assert fresh['id'] != stale['id']
    assert service.get_case(case['id'])['latest_inquiry']['body'] == '새 질문입니다.'


def test_explicit_regeneration_preserves_staff_revision(env):
    service = env[0]
    case = prepare(env)
    original = service.generate_draft(case['id'], ACTOR)
    edited = service.update_draft(original['id'], '직원이 자세히 수정한 답변', ACTOR, original['edit_version'])
    assert service.generate_draft(case['id'], ACTOR)['body'] == edited['body']
    with pytest.raises(DraftConflict):
        service.generate_draft(case['id'], ACTOR, regenerate=True, expected_version=original['edit_version'])
    regenerated = service.generate_draft(case['id'], ACTOR, regenerate=True, expected_version=edited['edit_version'])
    assert regenerated['id'] != edited['id']
    assert regenerated['supersedes_id'] == edited['id']
    history = {d['id']:d for d in service.get_case(case['id'])['drafts']}
    assert history[edited['id']]['body'] == edited['body']
    assert history[edited['id']]['status'] == 'superseded'


def test_send_rechecks_body_version_and_recipient_under_lock(env):
    service = env[0]
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    approval = service.get_case(case['id'])['approval_context']
    saved = service.update_draft(draft['id'], '다른 직원이 수정한 최종본', ACTOR, draft['edit_version'])
    with pytest.raises(DraftConflict):
        service.send_draft(draft['id'], ACTOR, draft['edit_version'], approval)
    service.update_case(case['id'], {'email': 'other@example.test'}, ACTOR)
    with pytest.raises(DraftConflict):
        service.send_draft(draft['id'], ACTOR, saved['edit_version'], approval)
    assert env[1].sent == []


def test_send_edit_race_after_preflight_fails_closed(env):
    service = env[0]
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    original_preflight = service._preflight
    def racing_preflight(case_id):
        original_preflight(case_id)
        service.update_draft(draft['id'], '승인 이후 수정본', 'other.staff', draft['edit_version'])
    service._preflight = racing_preflight
    with pytest.raises(DraftConflict):
        service.send_draft(draft['id'], ACTOR, draft['edit_version'])
    assert env[1].sent == []


def test_dashboard_counts_and_filtered_lists_use_same_case_predicates(env):
    service, gmail, clock, _ = env
    case = prepare(env, approve=False)
    first_reply(env, case)
    clock.at(jobs_for(service, case['id'])[0]['due_at'])
    service.run_due()
    another = service.ingest_message(inbound(gmail, clock, email='second@example.test', thread='second'))
    service.select_state(another['id'], 'S0', ACTOR)
    service.generate_draft(another['id'], ACTOR)
    dashboard = service.dashboard()
    for key in service.QUEUES:
        selected = service.list_cases(queue=key)
        assert dashboard[key] == len(selected)
        assert len({c['id'] for c in selected}) == len(selected)
    assert dashboard['staff_check'] == 1
    assert dashboard['draft_review'] == 1


def test_company_owes_info_keeps_manual_send_but_does_not_start_cycle(env):
    service = env[0]
    case = prepare(env)
    service.update_case(case['id'], {'company_owes_info': True}, ACTOR)
    first_reply(env, case)
    assert jobs_for(service, case['id']) == []
    assert service.get_case(case['id'])['workflow'] == 'staff_check'


def test_global_auto_pause_preserves_data_and_allows_manual_reviewed_reply(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    service.update_settings({'auto_followups_paused': True}, ADMIN)
    first_reply(env, case)
    jobs = jobs_for(service, case['id'])
    clock.at(jobs[0]['due_at'])
    service.run_due()
    assert len(gmail.sent) == 1
    assert jobs_for(service, case['id'])[0]['status'] == 'staff_check'
    service.update_settings({'auto_followups_paused': False}, ADMIN)
    service.run_due()
    assert len(gmail.sent) == 2
    assert service.operations_status()['scheduler']['ok']


class SimulatedLiveGmail(MockGmail):
    """A LOCAL-only adapter pretending to have an authenticated profile for unit tests."""
    is_mock = False
    account_email = 'office@example.test'


class SimulatedLiveDrafter(MockDrafter):
    def generate(self, context):
        return {**super().generate(context), 'provider': 'openai', 'model': 'unit-test-only',
                'request_id': 'no-real-request', 'usage': {'total_tokens': 12}}


@pytest.fixture
def live_env(tmp_path):
    clock = Clock()
    gmail = SimulatedLiveGmail(str(tmp_path/'simulated-live-mail.sqlite3'))
    service = Service(str(tmp_path/'simulated-live.sqlite3'), gmail, SimulatedLiveDrafter(), clock)
    return service, gmail, clock, str(tmp_path/'simulated-live.sqlite3')


def test_mock_records_cannot_be_promoted_when_adapter_switches_live(env, tmp_path):
    service, _, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    service.gmail = SimulatedLiveGmail(str(tmp_path/'isolated-mail.sqlite3'))
    service.update_settings({'live_enabled': True, 'legal_review_confirmed': True,
                             'connectivity_verified': True, 'verification_recipient': case['email']}, ADMIN)
    with pytest.raises(ValueError):
        service.send_draft(draft['id'], ACTOR, draft['edit_version'])
    with pytest.raises(ValueError):
        service.mark_verification_case(case['id'], ADMIN)
    assert service.gmail.sent == []
    assert service.get_case(case['id'])['data_mode'] == 'mock'


def setup_verification(live_env):
    service, gmail, clock, _ = live_env
    case = prepare(live_env, 'S0')
    service.update_settings({'verification_recipient': case['email'], 'live_enabled': True,
                             'legal_review_confirmed': True, 'connectivity_verified': True}, ADMIN)
    service.mark_verification_case(case['id'], ADMIN)
    return service.get_case(case['id'])


def test_limited_live_verification_uses_one_separate_job_and_production_evidence(live_env):
    service, gmail, clock, _ = live_env
    case = setup_verification(live_env)
    with pytest.raises(ValueError):
        service.update_settings({'operation_mode': 'production'}, ADMIN)
    first_reply(live_env, case)
    assert service.list_jobs() == []  # No ordinary schedules on a verification case.
    cancellation_probe=service.schedule_verification_followup(case['id'], 'S0-01', clock()+timedelta(minutes=10), ADMIN)
    clock.at(clock() + timedelta(minutes=2))
    service.ingest_message(inbound(gmail, clock, body='실제 확인용 답장에 대한 응답'))
    service.select_state(case['id'], 'S0', ACTOR)
    first_reply(live_env, case)
    due = clock() + timedelta(minutes=1)
    job = service.schedule_verification_followup(case['id'], 'S0-01', due, ADMIN)
    assert job['purpose'] == 'verification'
    assert len(service.list_jobs()) == 2
    assert next(j for j in service.list_jobs() if j['id']==cancellation_probe['id'])['status']=='cancelled'
    assert len(service.list_templates()) == 35
    clock.at(due)
    service.run_due()
    assert next(j for j in service.get_case(case['id'])['jobs'] if j['id']==job['id'])['status'] == 'sent'
    assert service.activation_status()['ready']
    service.update_settings({'operation_mode': 'production'}, ADMIN)
    draft = service.generate_draft(case['id'], ACTOR)
    with pytest.raises(ValueError, match='확인용 상담'):
        service.send_draft(draft['id'], ACTOR, draft['edit_version'])


def test_verification_does_not_auto_approve_or_reschedule_regular_jobs(live_env):
    service, _, clock, _ = live_env
    case = setup_verification(live_env)
    first_reply(live_env, case)
    service.approve_template('S0-01', False, ADMIN)
    with pytest.raises(ValueError, match='승인 문구'):
        service.schedule_verification_followup(case['id'], 'S0-01', clock()+timedelta(minutes=1), ADMIN)
    assert service.list_jobs() == []
    assert next(t for t in service.list_templates() if t['id']=='S0-01')['approval'] == '승인 취소'


def test_telemetry_failure_survives_restart_and_preserves_last_success(env):
    service, gmail, clock, db = env
    service.poll_mail()
    success = service.operations_status()['gmail_poll']['last_success_at']
    clock.at(clock()+timedelta(minutes=1))
    gmail.fail_latest = True
    # The mock's poll remains local; simulate a real poll transport failure.
    gmail.poll = lambda: (_ for _ in ()).throw(RuntimeError('transport unavailable'))
    service.poll_mail()
    restarted = Service(db, gmail, MockDrafter(), clock)
    status = restarted.operations_status()['gmail_poll']
    assert not status['ok'] and status['last_success_at'] == success
    assert status['last_attempt_at'] > success


def test_two_consultation_threads_do_not_cancel_each_other(env):
    service, gmail, clock, _ = env
    first = prepare(env, 'S0', thread='project-A')
    first_reply(env, first)
    second = prepare(env, 'S1', thread='project-B')
    first_reply(env, second)
    first_jobs = jobs_for(service, first['id'])
    second_jobs = jobs_for(service, second['id'])
    assert all(j['status'] == 'pending' for j in first_jobs + second_jobs)
    clock.at(clock() + timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, thread='project-A', body='첫 상담의 추가 질문'))
    assert all(j['status'] == 'cancelled' for j in jobs_for(service, first['id']))
    assert jobs_for(service, second['id']) == second_jobs
    service.select_state(first['id'], 'S0', ACTOR)
    first_reply(env, first)
    assert jobs_for(service, second['id']) == second_jobs
    service.ingest_message(inbound(gmail, clock, thread='project-A', direction='outbound', body='Gmail에서 직접 답변'))
    assert jobs_for(service, second['id']) == second_jobs
    assert service.get_case(second['id'])['followup_mode'] == 'active'


def test_cached_draft_does_not_falsely_refresh_successful_model_call(env):
    service, _, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    success = service.operations_status()['openai_draft']['last_success_at']
    clock.at(clock() + timedelta(minutes=2))
    assert service.generate_draft(case['id'], ACTOR)['id'] == draft['id']
    assert service.operations_status()['openai_draft']['last_success_at'] == success
    service.send_draft(draft['id'], ACTOR)
    assert service.operations_status()['staff_send']['details']['data_mode'] == 'mock'


def finish_verification(live_env):
    service, gmail, clock, _ = live_env
    case = setup_verification(live_env)
    first_reply(live_env, case)
    service.schedule_verification_followup(case['id'], 'S0-01', clock()+timedelta(minutes=10), ADMIN)
    clock.at(clock() + timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body='제한된 확인의 고객 답장'))
    service.select_state(case['id'], 'S0', ACTOR)
    first_reply(live_env, case)
    due = clock()+timedelta(minutes=1)
    service.schedule_verification_followup(case['id'], 'S0-01', due, ADMIN)
    clock.at(due)
    service.run_due()
    service.update_settings({'operation_mode':'production'}, ADMIN)


def test_overdue_live_jobs_need_individual_review_after_global_resume(live_env):
    service, gmail, clock, _ = live_env
    finish_verification(live_env)
    case = prepare(live_env, 'S0', email='operational@example.test', thread='operational')
    first_reply(live_env, case)
    job = jobs_for(service, case['id'])[0]
    service.update_settings({'auto_followups_paused':True}, ADMIN)
    clock.at(job['due_at'])
    service.run_due()
    service.update_settings({'auto_followups_paused':False}, ADMIN)
    count = len(gmail.sent)
    service.run_due()
    held = jobs_for(service, case['id'])[0]
    assert held['activation_review_required'] == 1
    assert len(gmail.sent) == count
    assert service.approve_job(job['id'], ACTOR)['status'] == 'sent'
    assert len(gmail.sent) == count+1


def test_additive_migration_preserves_existing_approvals_and_drafts(env):
    service, gmail, _, db = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    service.update_draft(draft['id'], '기존 저장 데이터', ACTOR)
    before_templates = service.list_templates()
    # Emulate the preceding schema without version/provenance columns, keeping
    # original data intact. SQLite DROP COLUMN is supported by this deployment.
    with service.connect() as c:
        for col in ('edit_version','generation_metadata','data_mode','supersedes_id'):
            c.execute('ALTER TABLE drafts DROP COLUMN '+col)
    migrated = Service(db, gmail, MockDrafter())
    assert migrated.list_templates() == before_templates
    recovered = migrated.get_case(case['id'])['drafts'][0]
    assert recovered['body'] == '기존 저장 데이터'
    assert recovered['edit_version'] == 1 and recovered['data_mode'] == 'mock'


def test_verification_reply_evidence_requires_actual_reservation_cancellation(live_env):
    service, gmail, clock, _ = live_env
    case = setup_verification(live_env)
    first_reply(live_env, case)
    clock.at(clock()+timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body='예약이 없을 때 온 응답'))
    assert 'verification_reply_received' not in service.operations_status()
    service.select_state(case['id'], 'S0', ACTOR)
    first_reply(live_env, case)
    scheduled = service.schedule_verification_followup(case['id'], 'S0-01', clock()+timedelta(minutes=10), ADMIN)
    clock.at(clock()+timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body='남은 예약을 취소해야 하는 응답'))
    evidence = service.operations_status()['verification_reply_received']['details']
    assert evidence['cancelled_job_ids'] == [scheduled['id']]
    assert evidence['cancelled_job_count'] == 1
    assert service.get_case(case['id'])['jobs'][0]['status'] == 'cancelled'
