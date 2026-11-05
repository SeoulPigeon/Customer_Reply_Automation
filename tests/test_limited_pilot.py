"""Limited real-customer enrollment boundary, exercised only with local adapters."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.core import Service
from app.integrations import MockDrafter, MockGmail
from test_workflows import prepare, first_reply, inbound, jobs_for, ACTOR, ADMIN
from test_workflow_improvements import live_env, finish_verification


def enable_limited(env):
    finish_verification(env)
    env[0].update_settings({'operation_mode':'limited'}, ADMIN)


def pilot_case(env, *, email='pilot@example.test', thread='pilot', state='S2', approve=False):
    case=prepare(env,state,email=email,thread=thread,approve=approve)
    env[0].enroll_pilot_case(case['id'],ADMIN)
    return env[0].get_case(case['id'])


def test_limited_mode_needs_real_verification_and_preserves_existing_data(live_env):
    service, gmail, clock, _=live_env
    with pytest.raises(ValueError,match='검증'):
        service.update_settings({'operation_mode':'limited'},ADMIN)
    finish_verification(live_env)
    old=prepare(live_env,'S2',email='existing@example.test',thread='existing')
    first_reply(live_env,old)
    templates=service.list_templates()
    old_jobs=jobs_for(service,old['id'])
    service.update_settings({'operation_mode':'limited'},ADMIN)
    assert service.list_templates()==templates
    assert service.pilot_status()['enrolled_count']==0
    assert service.get_case(old['id'])['history']
    assert [j['original_due_at'] for j in jobs_for(service,old['id'])]==[j['original_due_at'] for j in old_jobs]
    clock.at(old_jobs[0]['due_at'])
    count=len(gmail.sent)
    service.run_due()
    assert len(gmail.sent)==count
    assert not service.get_case(old['id'])['pilot_enrolled']


def test_pilot_cap_is_five_even_for_concurrent_admin_enrollment(live_env):
    service=live_env[0]
    enable_limited(live_env)
    cases=[prepare(live_env,'S0',email=f'p{i}@example.test',thread=f'p{i}') for i in range(6)]
    def enroll(case):
        try:
            service.enroll_pilot_case(case['id'],ADMIN)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(enroll,cases))
    assert sum(results)==5
    assert service.pilot_status()['enrolled_count']==5
    enrolled=service.pilot_status()['cases'][0]
    service.enroll_pilot_case(enrolled['id'],ADMIN,enabled=False)
    remaining=next(c for c in cases if not service.get_case(c['id'])['pilot_enrolled'] and c['id']!=enrolled['id'])
    service.enroll_pilot_case(remaining['id'],ADMIN)
    assert service.pilot_status()['enrolled_count']==5


def test_partial_approval_and_explicit_followup_optin_preserve_anchor(live_env):
    service,gmail,clock,_=live_env
    enable_limited(live_env)
    case=pilot_case(live_env)
    with pytest.raises(ValueError):
        service.set_pilot_followups(case['id'],True,ADMIN)
    _,sent=first_reply(live_env,case)
    assert jobs_for(service,case['id'])==[]
    with pytest.raises(ValueError,match='승인 문구'):
        service.set_pilot_followups(case['id'],True,ADMIN)
    service.approve_template('S2-02',True,ADMIN)
    template_snapshot=service.list_templates()
    clock.at(clock()+timedelta(minutes=20))
    enabled=service.set_pilot_followups(case['id'],True,ADMIN)
    jobs=jobs_for(service,case['id'])
    assert enabled['pilot_followups_enabled']
    assert len(jobs)==5
    assert jobs[0]['original_due_at']==(type(clock()).fromisoformat(sent['sent_at'])+timedelta(days=2)).isoformat(timespec='microseconds')
    assert service.list_templates()==template_snapshot
    assert enabled['operational_status']['approved_template_count']==1
    clock.at(jobs[0]['due_at'])
    count=len(gmail.sent)
    service.run_due()
    assert len(gmail.sent)==count
    assert jobs_for(service,case['id'])[0]['status']=='staff_check'
    clock.at(jobs[1]['due_at'])
    service.run_due()
    assert len(gmail.sent)==count+1
    assert jobs_for(service,case['id'])[1]['status']=='sent'


def test_optin_toggle_same_state_and_restart_do_not_reset_anchor(live_env):
    service,gmail,clock,db=live_env
    enable_limited(live_env)
    case=pilot_case(live_env,approve=True)
    first_reply(live_env,case)
    service.set_pilot_followups(case['id'],True,ADMIN)
    initial=jobs_for(service,case['id'])
    service.select_state(case['id'],'S2',ACTOR)
    assert service.get_case(case['id'])['pilot_followups_enabled']
    service.set_pilot_followups(case['id'],False,ADMIN)
    service.set_pilot_followups(case['id'],True,ADMIN)
    restarted=Service(db,gmail,service.drafter,clock)
    assert restarted.get_case(case['id'])['pilot_enrolled']
    assert restarted.get_case(case['id'])['pilot_followups_enabled']
    assert [j['id'] for j in restarted.get_case(case['id'])['jobs']]==[j['id'] for j in initial]
    assert [j['original_due_at'] for j in jobs_for(restarted,case['id'])]==[j['original_due_at'] for j in initial]


def test_new_customer_reply_cancels_pilot_cycle_and_needs_new_optin(live_env):
    service,gmail,clock,_=live_env
    enable_limited(live_env)
    case=pilot_case(live_env,approve=True)
    first_reply(live_env,case)
    service.set_pilot_followups(case['id'],True,ADMIN)
    clock.at(clock()+timedelta(minutes=1))
    service.ingest_message(inbound(gmail,clock,email=case['email'],thread='pilot',body='추가 문의'))
    refreshed=service.get_case(case['id'])
    assert refreshed['pilot_enrolled'] and not refreshed['pilot_followups_enabled']
    assert all(j['status']=='cancelled' for j in refreshed['jobs'])
    service.select_state(case['id'],'S2',ACTOR)
    first_reply(live_env,case)
    assert len(jobs_for(service,case['id']))==5
    service.set_pilot_followups(case['id'],True,ADMIN)
    assert len(jobs_for(service,case['id']))==10


def test_restart_holds_missed_live_jobs_and_single_review_releases_only_one(live_env):
    service,gmail,clock,db=live_env
    enable_limited(live_env)
    case=pilot_case(live_env,approve=True)
    first_reply(live_env,case)
    service.set_pilot_followups(case['id'],True,ADMIN)
    jobs=jobs_for(service,case['id'])
    clock.at(jobs[1]['due_at'])
    restarted=Service(db,gmail,service.drafter,clock)
    before=len(gmail.sent)
    result=restarted.hold_overdue_after_restart()
    assert result['held_count']==2
    restarted.run_due()
    assert len(gmail.sent)==before
    assert restarted.approve_job(jobs[0]['id'],ACTOR)['status']=='sent'
    restarted.run_due()
    assert len(gmail.sent)==before+1
    assert jobs_for(restarted,case['id'])[1]['activation_review_required']==1


def test_new_recipient_needs_reenrollment_and_new_actual_approved_reply(live_env):
    service=live_env[0]
    enable_limited(live_env)
    case=pilot_case(live_env,approve=True)
    first_reply(live_env,case)
    service.update_case(case['id'],{'email':'changed@example.test'},ACTOR)
    with pytest.raises(ValueError,match='재등록'):
        service.set_pilot_followups(case['id'],True,ADMIN)
    service.enroll_pilot_case(case['id'],ADMIN)
    with pytest.raises(ValueError,match='실제 정상 발송'):
        service.set_pilot_followups(case['id'],True,ADMIN)


def test_reception_latency_is_measured_once_and_remains_on_case(live_env):
    service,gmail,clock,_=live_env
    message=inbound(gmail,clock,email='latency@example.test',thread='latency')
    clock.at(clock()+timedelta(seconds=45))
    result=service.poll_mail()
    assert result['new_messages']==1
    assert result['last_reception_latency_seconds']==45
    case=service.get_case(result['case_ids'][0])
    assert case['latest_reception_latency_seconds']==45
    second=service.poll_mail()
    assert second['new_messages']==0 and second['duplicate_messages']==1
    assert second['last_reception_latency_seconds'] is None
    assert service.get_case(case['id'])['latest_reception_latency_seconds']==45


def test_operational_status_explains_enrollment_and_followup_actions(live_env):
    service=live_env[0]
    enable_limited(live_env)
    case=prepare(live_env,'S2',email='explain@example.test',thread='explain')
    status=service.get_case(case['id'])['operational_status']
    assert status['manual_send_blocked'] and any('개별 등록' in text for text in status['blockers'])
    service.enroll_pilot_case(case['id'],ADMIN)
    first_reply(live_env,case)
    status=service.get_case(case['id'])['operational_status']
    assert status['followup_blocked'] and '개별 활성화' in status['next_action']
    service.set_pilot_followups(case['id'],True,ADMIN)
    status=service.get_case(case['id'])['operational_status']
    assert not status['followup_blocked'] and '다음 후속 예정' in status['next_action']


class SpyRealDrafter:
    """Records inputs locally; never calls OpenAI or any external service."""
    is_mock=False
    def __init__(self):
        self.calls=[]
    def generate(self,context):
        self.calls.append(context)
        return {'body':'확인된 상담 답변','checks':[],'provider':'openai','model':'local-spy-only'}


def test_unenrolled_real_customer_never_reaches_real_drafter(live_env):
    service=live_env[0]
    enable_limited(live_env)
    case=prepare(live_env,'S2',email='not-selected@example.test',thread='not-selected')
    spy=SpyRealDrafter()
    service.drafter=spy
    status=service.get_case(case['id'])['operational_status']
    assert status['generation_blocked'] and '개별 등록' in status['generation_block_reason']
    with pytest.raises(ValueError,match='개별 등록'):
        service.generate_draft(case['id'],ACTOR)
    assert spy.calls==[]
    assert service.get_case(case['id'])['drafts']==[]
    service.enroll_pilot_case(case['id'],ADMIN)
    service.update_settings({'live_enabled':False},ADMIN)
    assert not service.get_case(case['id'])['operational_status']['generation_blocked']
    draft=service.generate_draft(case['id'],ACTOR)
    assert draft['body']=='확인된 상담 답변'
    assert len(spy.calls)==1  # Draft generation does not enable email transmission.


def test_verification_real_ai_requires_marked_recipient_and_scoped_history(live_env):
    service,gmail,clock,_=live_env
    case=prepare(live_env,'S0',email='review@example.test',thread='allowed')
    other=service.ingest_message(inbound(gmail,clock,email=case['email'],thread='not-authorized',body='다른 상담의 비공개 내용'))
    assert other['id']!=case['id']
    spy=SpyRealDrafter()
    service.drafter=spy
    with pytest.raises(ValueError,match='확인용 상담'):
        service.generate_draft(case['id'],ACTOR)
    assert spy.calls==[]
    service.update_settings({'verification_recipient':case['email']},ADMIN)
    service.mark_verification_case(case['id'],ADMIN)
    assert not service.settings()['live_enabled']
    service.generate_draft(case['id'],ACTOR)
    assert len(spy.calls)==1
    assert all('비공개' not in message['body'] for message in spy.calls[0]['history'])


def test_changed_pilot_recipient_cannot_regenerate_via_real_ai(live_env):
    service=live_env[0]
    enable_limited(live_env)
    case=pilot_case(live_env)
    spy=SpyRealDrafter();service.drafter=spy
    draft=service.generate_draft(case['id'],ACTOR)
    service.update_case(case['id'],{'email':'unselected-new@example.test'},ACTOR)
    with pytest.raises(ValueError,match='개별 등록'):
        service.generate_draft(case['id'],ACTOR,regenerate=True,expected_version=draft['edit_version'])
    assert len(spy.calls)==1


def test_late_template_approval_holds_only_overdue_live_jobs_for_review(live_env):
    service,gmail,clock,_=live_env
    enable_limited(live_env)
    origin=clock()
    service.approve_template('S4-01',True,ADMIN)
    first=pilot_case(live_env,email='first-s4@example.test',thread='first-s4',state='S4')
    first_reply(live_env,first)
    service.set_pilot_followups(first['id'],True,ADMIN)
    first_jobs=jobs_for(service,first['id'])
    clock.at(first_jobs[0]['due_at'])
    service.run_due()
    assert jobs_for(service,first['id'])[0]['status']=='sent'
    clock.at(origin+timedelta(days=2))
    second=pilot_case(live_env,email='second-s4@example.test',thread='second-s4',state='S4')
    first_reply(live_env,second)
    service.set_pilot_followups(second['id'],True,ADMIN)
    second_jobs=jobs_for(service,second['id'])
    clock.at(first_jobs[1]['due_at'])
    service.run_due()
    overdue=jobs_for(service,first['id'])[1]
    future=jobs_for(service,second['id'])[1]
    assert overdue['status']=='staff_check' and not overdue['activation_review_required']
    assert future['due_at']>overdue['due_at']
    count=len(gmail.sent)
    service.approve_template('S4-02',True,ADMIN)
    held=jobs_for(service,first['id'])[1]
    assert held['activation_review_required']==1
    assert held['due_at']==overdue['due_at'] and held['original_due_at']==overdue['original_due_at']
    assert jobs_for(service,second['id'])[1]==future
    service.run_due()
    assert len(gmail.sent)==count
    assert service.approve_job(held['id'],ACTOR)['status']=='sent'
    assert len(gmail.sent)==count+1
    service.run_due()
    assert len(gmail.sent)==count+1
    assert not jobs_for(service,second['id'])[1]['activation_review_required']
