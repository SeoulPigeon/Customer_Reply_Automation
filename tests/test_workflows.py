"""Executable acceptance scenarios A–I; all mail stays in the local mock mailbox.

The clock deliberately starts on a Wednesday at 10:00 Asia/Seoul, so the S2
2-day and 5-day checkpoints both fall within weekday business hours.
"""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import multiprocessing
import uuid

import openpyxl
import pytest

from app.core import Service
from app.integrations import MockDrafter, MockGmail


ACTOR = "test.staff"
ADMIN = "test.admin"
SOURCE = Path(__file__).resolve().parents[1] / "data" / "followup_templates.xlsx"
SCHEDULES = {
    "S0": [3, 7, 14, 30, 60], "S1": [3, 7, 14, 30, 60],
    "S2": [2, 5, 10, 21, 45], "S3": [2, 5, 10, 21, 45],
    "S4": [1, 3, 7, 14, 30], "S5": [1, 3, 7, 14, 30],
    "S6": [1, 2, 5, 7, 14],
}


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def at(self, value):
        self.now = stamp(value) if isinstance(value, str) else value


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    gmail = MockGmail(str(tmp_path / "mail.sqlite3"))
    db = str(tmp_path / "app.sqlite3")
    service = Service(db, gmail, MockDrafter(), clock=clock)
    service.initialize(SOURCE)
    return service, gmail, clock, db


def inbound(gmail, clock, *, email="customer@example.test", thread="thread-1", body="프로젝트 일정과 비용이 궁금합니다.", direction="inbound", automated=False):
    ident = uuid.uuid4().hex
    return gmail.inject({
        "id": ident, "email": email, "name": "가상 고객", "thread_id": thread,
        "message_id": f"<{ident}@example.test>", "subject": "프로젝트 문의",
        "body": body, "received_at": clock().isoformat(),
        "direction": direction, "is_automated": automated, "app_generated": False,
    })


def prepare(env, state="S2", *, approve=True, fields=None, email="customer@example.test", thread="thread-1", consent=True):
    service, gmail, clock, _ = env
    message = inbound(gmail, clock, email=email, thread=thread)
    case = service.ingest_message(message)
    case = service.select_state(case["id"], state, ACTOR)
    case = service.update_case(case["id"], {
        "email_allowed": consent, "consent_note": "직원이 수신 동의와 발송 조건 확인" if consent else "",
        "fields": fields if fields is not None else {"프로젝트명": "확인된 프로젝트", "핵심 조건": "확인된 일정", "A 프로젝트": "후보 A", "B 프로젝트": "후보 B"},
        "followup_mode": "active",
    }, ACTOR)
    if approve:
        for step in range(1, 6):
            service.approve_template(f"{state}-{step:02d}", True, ADMIN)
    return case


def first_reply(env, case, body="직원이 확인한 최종 답변입니다."):
    service = env[0]
    draft = service.generate_draft(case["id"], ACTOR)
    service.update_draft(draft["id"], body, ACTOR)
    sent = service.send_draft(draft["id"], ACTOR)
    assert sent["status"] == "sent"
    return draft, sent


def jobs_for(service, case_id):
    return sorted([job for job in service.list_jobs() if job["case_id"] == case_id], key=lambda j: (j["original_due_at"], j["template_id"]))


def sent_count(gmail):
    return sum(message["direction"] == "outbound" for message in gmail.poll())


def run_in_independent_process(db, mailbox, when):
    clock = Clock()
    clock.at(when)
    service = Service(db, MockGmail(mailbox), MockDrafter(), clock=clock)
    return service.run_due()


def test_excel_import_is_exact_and_all_35_versions_begin_pending(env):
    service = env[0]
    templates = {item["id"]: item for item in service.list_templates()}
    assert len(templates) == 35
    book = openpyxl.load_workbook(SOURCE, read_only=True, data_only=True)
    sheet = book["후속문구_S0-S6"]
    for row in list(sheet.values)[1:]:
        item = templates[row[0]]
        assert item["body"] == row[5]
        assert item["state"] == row[1]
        assert item["approval"] == "승인 대기"
        assert item["automation"] == row[6]
        assert item["conditions"] == row[7]
        assert item["note"] == (row[10] or "")
    book.close()
    service.initialize(SOURCE)
    assert len(service.list_templates()) == 35


@pytest.mark.parametrize("state,days", list(SCHEDULES.items()))
def test_exact_original_elapsed_day_offsets_for_all_states(env, state, days):
    case = prepare(env, state)
    origin = env[2]()
    first_reply(env, case)
    jobs = jobs_for(env[0], case["id"])
    assert len(jobs) == 5
    assert [job["template_id"] for job in jobs] == [f"{state}-{step:02d}" for step in range(1, 6)]
    assert [stamp(job["original_due_at"]) for job in jobs] == [origin + timedelta(days=day) for day in days]
    assert all(stamp(job["due_at"]) >= stamp(job["original_due_at"]) for job in jobs)


def test_a_staff_review_is_required_before_first_send_and_schedule(env):
    service, gmail, _, _ = env
    case = prepare(env)
    assert service.list_jobs() == []
    draft = service.generate_draft(case["id"], ACTOR)
    assert draft["body"]
    assert sent_count(gmail) == 0
    assert service.list_jobs() == []
    service.update_draft(draft["id"], "직원이 수정·확인한 내용", ACTOR)
    result = service.send_draft(draft["id"], ACTOR)
    assert result["status"] == "sent"
    assert sent_count(gmail) == 1
    assert len(jobs_for(service, case["id"])) == 5
    assert any(item.get("body") == "직원이 수정·확인한 내용" for item in service.get_case(case["id"])["history"])


def test_b_followups_use_original_anchor_and_do_not_restart_cycle(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    origin = clock()
    first_reply(env, case)
    baseline = jobs_for(service, case["id"])
    clock.at(origin + timedelta(days=2))
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "sent"
    assert sent_count(gmail) == 2
    clock.at(origin + timedelta(days=5))
    service.run_due()
    current = jobs_for(service, case["id"])
    assert current[1]["status"] == "sent"
    assert sent_count(gmail) == 3
    assert [job["id"] for job in current] == [job["id"] for job in baseline]
    assert [job["original_due_at"] for job in current] == [job["original_due_at"] for job in baseline]
    service.run_due()
    assert sent_count(gmail) == 3


def test_c_reply_cancels_old_schedule_and_same_state_can_start_fresh_cycle(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    original = jobs_for(service, case["id"])
    clock.at(original[0]["due_at"])
    service.run_due()
    clock.at(clock() + timedelta(hours=1))
    reply = inbound(gmail, clock, body="조금 더 자세히 알려주세요.")
    service.ingest_message(reply)
    old = jobs_for(service, case["id"])
    assert old[0]["status"] == "sent"
    assert all(job["status"] == "cancelled" and job["reason"] for job in old[1:])
    with pytest.raises(ValueError):
        service.generate_draft(case["id"], ACTOR)
    service.select_state(case["id"], "S2", ACTOR)
    first_reply(env, case)
    combined = jobs_for(service, case["id"])
    fresh = [job for job in combined if job["id"] not in {j["id"] for j in old}]
    assert len(fresh) == 5
    assert stamp(fresh[0]["original_due_at"]) == clock() + timedelta(days=2)


def test_d_customer_can_move_from_s1_to_s4_and_old_schedule_stays_cancelled(env):
    service, gmail, clock, _ = env
    case = prepare(env, "S1")
    first_reply(env, case)
    old_ids = {job["id"] for job in service.list_jobs()}
    clock.at(clock() + timedelta(hours=1))
    service.ingest_message(inbound(gmail, clock))
    service.select_state(case["id"], "S4", ACTOR)
    for step in range(1, 6):
        service.approve_template(f"S4-{step:02d}", True, ADMIN)
    first_reply(env, case)
    all_jobs = service.list_jobs()
    assert all(job["status"] == "cancelled" for job in all_jobs if job["id"] in old_ids)
    fresh = [job for job in all_jobs if job["id"] not in old_ids]
    assert {job["template_id"] for job in fresh} == {f"S4-{step:02d}" for step in range(1, 6)}


def test_e_s6_requires_latest_incomplete_check_and_individual_staff_approval(env):
    service, gmail, clock, _ = env
    case = prepare(env, "S6")
    first_reply(env, case)
    job = jobs_for(service, case["id"])[0]
    clock.at(job["due_at"])
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "staff_check"
    assert sent_count(gmail) == 1
    blocked = service.approve_job(job["id"], ACTOR)
    assert blocked["status"] == "staff_check"
    assert blocked["reason"]
    assert sent_count(gmail) == 1
    service.update_case(case["id"], {"payment": "incomplete"}, ACTOR)
    service.run_due()
    assert sent_count(gmail) == 1
    service.approve_job(job["id"], ACTOR)
    assert sent_count(gmail) == 2
    service.update_case(case["id"], {"payment": "complete"}, ACTOR)
    assert all(job["status"] in {"sent", "cancelled"} for job in jobs_for(service, case["id"]))


def test_f_missing_comparison_projects_blocks_s3_01(env):
    service, gmail, clock, _ = env
    case = prepare(env, "S3", fields={})
    first_reply(env, case)
    clock.at(jobs_for(service, case["id"])[0]["due_at"])
    service.run_due()
    blocked = jobs_for(service, case["id"])[0]
    assert blocked["status"] == "staff_check"
    assert blocked["reason"]
    assert sent_count(gmail) == 1


def test_g_pending_template_blocks_and_explicit_approval_unblocks_same_version(env):
    service, gmail, clock, _ = env
    case = prepare(env, approve=False)
    first_reply(env, case)
    clock.at(jobs_for(service, case["id"])[0]["due_at"])
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "staff_check"
    assert sent_count(gmail) == 1
    service.approve_template("S2-01", True, ADMIN)
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "sent"
    assert sent_count(gmail) == 2


def test_h_phone_without_email_creates_call_task_and_cannot_send_email(env):
    service, gmail, clock, _ = env
    due = clock() + timedelta(days=1)
    case = service.create_phone({"name": "전화 고객", "phone": "01000000000", "body": "전화로 안내 부탁드립니다.", "state": "S1", "followup_at": due.isoformat(), "assignee": ACTOR}, ACTOR)
    draft = service.generate_draft(case["id"], ACTOR)
    assert draft["body"]
    with pytest.raises(ValueError):
        service.send_draft(draft["id"], ACTOR)
    assert service.list_jobs() == []
    calls = service.list_calls()
    assert len(calls) == 1
    service.complete_call(calls[0]["id"], ACTOR)
    assert service.list_calls()[0]["status"] == "completed"
    assert sent_count(gmail) == 0


def test_i_duplicate_incoming_and_duplicate_send_requests_are_idempotent(env):
    service, gmail, clock, _ = env
    message = inbound(gmail, clock)
    first = service.ingest_message(message)
    second = service.ingest_message(message)
    assert first["id"] == second["id"]
    assert len(service.list_cases()) == 1
    assert len(service.get_case(first["id"])["history"]) == 1
    service.select_state(first["id"], "S0", ACTOR)
    service.update_case(first["id"], {"email_allowed": True, "consent_note": "확인", "followup_mode": "active"}, ACTOR)
    draft, sent = first_reply(env, first)
    repeat = service.send_draft(draft["id"], ACTOR)
    assert repeat["id"] == sent["id"]
    assert sent_count(gmail) == 1
    assert len(service.list_jobs()) == 5


def test_i_restart_preserves_jobs_and_concurrent_workers_send_at_most_once(env):
    service, gmail, clock, db = env
    case = prepare(env)
    first_reply(env, case)
    jobs = jobs_for(service, case["id"])
    restarted = Service(db, gmail, MockDrafter(), clock=clock)
    assert [job["id"] for job in jobs_for(restarted, case["id"])] == [job["id"] for job in jobs]
    clock.at(jobs[0]["due_at"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(worker.run_due) for worker in [service, restarted]]
        for result in results:
            result.result(timeout=20)
    assert sent_count(gmail) == 2
    assert jobs_for(service, case["id"])[0]["status"] == "sent"


def test_i_independent_processes_cannot_send_the_same_reservation_twice(env):
    service, gmail, clock, db = env
    case = prepare(env)
    first_reply(env, case)
    clock.at(jobs_for(service, case["id"])[0]["due_at"])
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = [pool.submit(run_in_independent_process, db, str(Path(db).parent / "mail.sqlite3"), clock().isoformat()) for _ in range(2)]
        for result in results:
            result.result(timeout=30)
    assert sent_count(gmail) == 2
    assert jobs_for(service, case["id"])[0]["status"] == "sent"


def test_i_mail_connection_failure_prevents_automatic_send(env, monkeypatch):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    clock.at(jobs_for(service, case["id"])[0]["due_at"])
    def unavailable(*args, **kwargs):
        raise ConnectionError("simulated connection outage")
    monkeypatch.setattr(gmail, "latest", unavailable)
    service.run_due()
    assert sent_count(gmail) == 1
    job = jobs_for(service, case["id"])[0]
    assert job["status"] == "staff_check"
    assert job["reason"]


@pytest.mark.parametrize("event", ["reply", "external_sent"])
def test_i_unpolled_message_is_detected_immediately_before_sending(env, event):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    clock.at(jobs_for(service, case["id"])[0]["due_at"])
    inbound(gmail, clock, direction="outbound" if event == "external_sent" else "inbound")
    before = sent_count(gmail)
    service.run_due()
    assert sent_count(gmail) == before
    assert all(job["status"] == "cancelled" for job in jobs_for(service, case["id"]))


def test_i_new_thread_reply_cancels_all_active_cases_for_same_customer(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    clock.at(clock() + timedelta(hours=1))
    another = service.ingest_message(inbound(gmail, clock, thread="new-topic"))
    assert another["id"] != case["id"]
    assert another["customer_id"] == case["customer_id"]
    assert all(job["status"] == "cancelled" for job in jobs_for(service, case["id"]))


def test_automated_messages_do_not_invalidate_customer_cycle(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    service.ingest_message(inbound(gmail, clock, body="자동 부재중 응답", automated=True))
    assert all(job["status"] == "pending" for job in jobs_for(service, case["id"]))


def test_edit_creates_pending_version_and_never_substitutes_into_old_schedule(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    initial_job = jobs_for(service, case["id"])[0]
    edited = service.update_template("S2-01", "변경된 문구 [프로젝트명]", ADMIN)
    assert edited["approval"] == "승인 대기"
    assert edited["version"] > initial_job["template_version"]
    clock.at(initial_job["due_at"])
    service.run_due()
    assert sent_count(gmail) == 1
    service.approve_template("S2-01", True, ADMIN)
    service.run_due()
    assert sent_count(gmail) == 1
    assert jobs_for(service, case["id"])[0]["template_version"] == initial_job["template_version"]


def test_weekend_business_window_defers_without_changing_elapsed_due(env):
    service, _, clock, _ = env
    case = prepare(env, "S0")
    origin = clock()
    first_reply(env, case)
    job = jobs_for(service, case["id"])[0]
    assert stamp(job["original_due_at"]) == origin + timedelta(days=3)
    # Saturday 10:00 KST becomes Monday 09:00 KST.
    assert stamp(job["due_at"]) == datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("change", [{"followup_mode": "paused"}, {"followup_mode": "stopped"}, {"email_allowed": False}])
def test_staff_pause_stop_or_contact_permission_revocation_prevents_send(env, change):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    due = jobs_for(service, case["id"])[0]["due_at"]
    service.update_case(case["id"], change, ACTOR)
    clock.at(due)
    service.run_due()
    assert sent_count(gmail) == 1


def test_a_new_reply_invalidates_already_generated_unsent_draft(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case["id"], ACTOR)
    clock.at(clock() + timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body="앞선 문의를 정정합니다."))
    with pytest.raises(ValueError):
        service.send_draft(draft["id"], ACTOR)
    assert sent_count(gmail) == 0


def test_uncertain_send_is_never_blindly_retried_and_can_be_reconciled(env, monkeypatch):
    from app.integrations import SendUncertain
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    job = jobs_for(service, case["id"])[0]
    clock.at(job["due_at"])
    original_send = gmail.send
    def accepted_but_response_lost(*args, **kwargs):
        original_send(*args, **kwargs)
        raise SendUncertain("simulated response lost after provider accepted mail")
    monkeypatch.setattr(gmail, "send", accepted_but_response_lost)
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "uncertain"
    assert sent_count(gmail) == 2
    monkeypatch.setattr(gmail, "send", original_send)
    service.run_due()
    assert sent_count(gmail) == 2
    uncertain = next(item for item in service.get_case(case["id"])["outbox"] if item.get("job_id") == job["id"])
    resolved = service.reconcile_send(uncertain["id"], ACTOR)
    assert resolved["status"] == "sent"
    assert jobs_for(service, case["id"])[0]["status"] == "sent"
    assert sent_count(gmail) == 2


def test_consecutive_customer_replies_do_not_generate_ai_or_schedules(env):
    service, gmail, clock, _ = env
    first = inbound(gmail, clock)
    case = service.ingest_message(first)
    for index in range(3):
        clock.at(clock() + timedelta(minutes=1))
        service.ingest_message(inbound(gmail, clock, body=f"추가 질문 {index}"))
    detail = service.get_case(case["id"])
    assert len(detail["history"]) == 4
    assert detail["drafts"] == []
    assert detail["jobs"] == []
    assert sent_count(gmail) == 0


def test_s6_old_payment_check_needs_refresh_before_staff_approval(env):
    service, gmail, clock, _ = env
    case = prepare(env, "S6")
    service.update_case(case["id"], {"payment": "incomplete"}, ACTOR)
    first_reply(env, case)
    second_job = jobs_for(service, case["id"])[1]
    clock.at(second_job["due_at"])
    service.run_due()
    blocked = service.approve_job(second_job["id"], ACTOR)
    assert blocked["status"] == "staff_check"
    assert "24" in blocked["reason"]
    assert sent_count(gmail) == 1


def test_s7_is_rejected_and_no_drafter_can_assign_a_customer_state(env):
    service, gmail, clock, _ = env
    case = service.ingest_message(inbound(gmail, clock))
    with pytest.raises(ValueError):
        service.select_state(case["id"], "S7", ACTOR)
    with pytest.raises(ValueError):
        service.generate_draft(case["id"], ACTOR)
    assert sent_count(gmail) == 0


def test_optout_message_takes_priority_over_staff_followup_settings(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    clock.at(clock() + timedelta(hours=1))
    service.ingest_message(inbound(gmail, clock, body="더 이상 연락하지 마세요. 수신 거부합니다."))
    detail = service.get_case(case["id"])
    assert detail["opt_out"]
    assert not detail["email_allowed"]
    assert all(job["status"] == "cancelled" for job in detail["jobs"])
    with pytest.raises(ValueError):
        service.update_case(case["id"], {"email_allowed": True, "consent_note": "직원 선택"}, ACTOR)
    assert sent_count(gmail) == 1


def test_revoking_template_approval_blocks_already_scheduled_job(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    first_reply(env, case)
    job = jobs_for(service, case["id"])[0]
    service.approve_template("S2-01", False, ADMIN)
    clock.at(job["due_at"])
    service.run_due()
    assert jobs_for(service, case["id"])[0]["status"] == "staff_check"
    assert sent_count(gmail) == 1


def test_uncertain_customer_cannot_bypass_by_creating_new_draft(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    gmail.fail_send_after_commit = True
    uncertain = service.send_draft(draft['id'], ACTOR)
    assert uncertain['status'] == 'uncertain'
    gmail.fail_send_after_commit = False
    service.select_state(case['id'], 'S2', ACTOR)
    another = service.generate_draft(case['id'], ACTOR)
    with pytest.raises(ValueError, match='불명확'):
        service.send_draft(another['id'], ACTOR)
    assert sent_count(gmail) == 1


def test_reconcile_after_customer_reply_preserves_state_selection(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    draft = service.generate_draft(case['id'], ACTOR)
    gmail.fail_send_after_commit = True
    uncertain = service.send_draft(draft['id'], ACTOR)
    gmail.fail_send_after_commit = False
    clock.at(clock() + timedelta(minutes=1))
    service.ingest_message(inbound(gmail, clock, body='새로운 질문입니다.'))
    assert service.reconcile_send(uncertain['id'], ACTOR)['status'] == 'sent'
    detail = service.get_case(case['id'])
    assert detail['workflow'] == 'state_selection'
    assert not detail['jobs']


def test_template_edit_cannot_remove_required_source_conditions(env):
    service, gmail, clock, _ = env
    service.update_template('S2-01', '문의하신 내용을 더 확인할까요?', ADMIN)
    case = prepare(env, fields={})
    first_reply(env, case)
    job = jobs_for(service, case['id'])[0]
    clock.at(job['due_at'])
    service.run_due()
    assert jobs_for(service, case['id'])[0]['status'] == 'staff_check'
    assert '프로젝트명' in jobs_for(service, case['id'])[0]['reason']
    assert sent_count(gmail) == 1


def test_retention_purges_closed_customer_and_prevents_message_reimport(env):
    service, gmail, clock, _ = env
    case = prepare(env)
    original = gmail.poll()[0]
    first_reply(env, case)
    service.update_case(case['id'], {'workflow': 'closed'}, ACTOR)
    clock.at(clock() + timedelta(days=366))
    assert service.purge_closed(actor=ADMIN, dry_run=True)['customer_count'] == 1
    assert len(service.list_cases()) == 1
    result = service.purge_closed(actor=ADMIN, dry_run=False)
    assert result['customer_count'] == 1
    assert result['mock_cleanup_complete']
    assert service.list_cases() == []
    assert gmail.poll() == []
    assert service.ingest_message(original) == {'ignored': True}
