#!/usr/bin/env python3
"""Bounded real-process recovery check using ONLY isolated mock adapters.

Run from the checkout with .venv/bin/python scripts/check_process_recovery.py.
No browser is opened. Only /health is requested while each server starts; inbox
collection and scheduling must happen through the server's normal 30-second loop.
The temporary test DB uses one shortened due time and all-day working hours.
Production data, real Gmail, OpenAI, deployment and long-term uptime are not tested.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import Service  # noqa: E402
from app.integrations import MockDrafter, MockGmail  # noqa: E402


def read_rows(path, sql, parameters=()):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, parameters)]


def wait_until(check, *, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f'Timed out after {seconds}s: {description}')


def operation(path, name):
    values = read_rows(path, 'SELECT * FROM operations WHERE name=?', (name,))
    return values[0] if values else {}


def start_server(folder, port, log):
    env = dict(os.environ)
    # Explicit adapters prevent inherited credentials from being used. Optional
    # Sheets credentials are removed too; no external factory is exercised.
    for name in tuple(env):
        if name.startswith(('GOOGLE_', 'GMAIL_', 'SHEETS_', 'OPENAI_', 'GAPYEAR_OPENAI_', 'APP_SECRET_')):
            env.pop(name)
    env.update(APP_ENV='development', APP_DATA_DIR=str(folder), APP_MAIL_MODE='mock',
               APP_AI_MODE='mock', ENABLE_SCHEDULER='true', COOKIE_SECURE='false',
               APP_BASE_URL='', PYTHONUNBUFFERED='1')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                '--host', '127.0.0.1', '--port', str(port), '--workers', '1',
                                '--no-access-log', '--log-level', 'warning'],
                               cwd=ROOT, env=env, stdout=log, stderr=log)
    url = f'http://127.0.0.1:{port}/health'
    def ready():
        if process.poll() is not None:
            raise AssertionError(f'Test server exited with code {process.returncode}')
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False
    try:
        wait_until(ready, seconds=8, description='loopback health endpoint')
    except Exception:
        stop_server(process)
        raise
    return process


def stop_server(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def run_check():
    started = time.monotonic()
    process = None
    with tempfile.TemporaryDirectory(prefix='gapyear-process-recovery-') as temporary:
        folder = Path(temporary)
        path = folder / 'app.sqlite3'
        mailbox = MockGmail(path)
        service = Service(path, mailbox, MockDrafter())
        service.update_settings({'business_start': 0, 'business_end': 24, 'weekdays': list(range(7))}, 'isolated-recovery-check')
        service.approve_template('S4-01', True, 'isolated-recovery-check')
        initial = mailbox.receive(email='scheduled@example.test', subject='모의 예약 복구 확인', body='일정을 상담합니다.')
        case = service.ingest_message(initial)
        service.select_state(case['id'], 'S4', 'isolated-recovery-check')
        service.update_case(case['id'], {'email_allowed': True, 'consent_note': '격리 모의 검증 데이터만 허용',
                                        'fields': {'프로젝트명': '모의 프로젝트', '핵심 조건': '모의 확인 조건'}}, 'isolated-recovery-check')
        draft = service.generate_draft(case['id'], 'isolated-recovery-check')
        staff_sent = service.send_draft(draft['id'], 'isolated-recovery-check')
        assert staff_sent['status'] == 'sent'
        scheduled = sorted(service.get_case(case['id'])['jobs'], key=lambda job: job['step'])
        assert len(scheduled) == 5
        # This only changes this temporary mock test record, never the schedules
        # module, real clock, normal delays, production DB or original_due_at.
        first_due = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
        with service.transaction() as db:
            db.execute('UPDATE jobs SET due_at=? WHERE id=?', (first_due, scheduled[0]['id']))
        second = service.ingest_message(mailbox.receive(email='draft@example.test', subject='초안 보존 확인', body='다시 확인할 초안입니다.'))
        service.select_state(second['id'], 'S4', 'isolated-recovery-check')
        editable = service.generate_draft(second['id'], 'isolated-recovery-check')
        saved_text = '직원이 저장한 모의 답변 — 서버 재시작 후 동일 본문과 버전을 유지해야 합니다.'
        editable = service.update_draft(editable['id'], saved_text, 'isolated-recovery-check', expected_version=editable['edit_version'])
        with socket.socket() as selector:
            selector.bind(('127.0.0.1', 0))
            port = selector.getsockname()[1]
        with (folder / 'server.log').open('w+') as log:
            try:
                process = start_server(folder, port, log)
                first_pid = process.pid
                wait_until(lambda: operation(path, 'scheduler').get('ok') == 1,
                           seconds=5, description='first background scheduler tick')
                injected_at = time.monotonic()
                incoming = mailbox.receive(email='background-one@example.test', subject='브라우저 없이 수신', body='백그라운드 수신 확인용 모의 문의')
                wait_until(lambda: read_rows(path, 'SELECT id FROM messages WHERE provider_id=?', (incoming['id'],)),
                           seconds=38, description='background inbox ingestion without browser')
                receive_elapsed = time.monotonic() - injected_at
                wait_until(lambda: read_rows(path, "SELECT id FROM jobs WHERE id=? AND status='sent'", (scheduled[0]['id'],)),
                           seconds=5, description='scheduled mock follow-up without browser')
                before = read_rows(path, 'SELECT id,status,sent_at,provider_id FROM outbox ORDER BY id')
                before_jobs = read_rows(path, 'SELECT id,status,cycle_id,original_due_at,due_at,sent_at FROM jobs ORDER BY id')
                assert len(before) == 2 and all(row['status'] == 'sent' for row in before)
                assert len(mailbox.sent) == 2
                first_tick = operation(path, 'scheduler')['last_success_at']
                stop_server(process)
                process = None
                restart_at = time.monotonic()
                process = start_server(folder, port, log)
                second_pid = process.pid
                wait_until(lambda: operation(path, 'scheduler').get('last_success_at', '') > first_tick,
                           seconds=5, description='scheduler restart using same persistent mock DB')
                assert process.pid != first_pid
                next_tick = operation(path, 'scheduler')['last_success_at']
                next_incoming = mailbox.receive(email='background-two@example.test', subject='재시작 후 수신', body='재시작 후에도 브라우저 없이 수신')
                wait_until(lambda: read_rows(path, 'SELECT id FROM messages WHERE provider_id=?', (next_incoming['id'],)),
                           seconds=38, description='second background ingestion after restart')
                wait_until(lambda: operation(path, 'scheduler').get('last_success_at', '') > next_tick,
                           seconds=5, description='second periodic scheduler tick after restart')
                restarted_elapsed = time.monotonic() - restart_at
                after = read_rows(path, 'SELECT id,status,sent_at,provider_id FROM outbox ORDER BY id')
                after_jobs = read_rows(path, 'SELECT id,status,cycle_id,original_due_at,due_at,sent_at FROM jobs ORDER BY id')
                restored = read_rows(path, 'SELECT body,status,edit_version FROM drafts WHERE id=?', (editable['id'],))[0]
                assert after == before, 'Restart resent or rewrote confirmed outbox records'
                assert after_jobs == before_jobs, 'Restart altered existing jobs or schedule baselines'
                assert restored == {'body': saved_text, 'status': 'draft', 'edit_version': editable['edit_version']}
                assert len(mailbox.sent) == 2, 'Duplicate outbound mock mail detected'
                assert len(read_rows(path, 'SELECT id FROM cases')) == 4
                result = {
                    'status': 'passed', 'mode': 'isolated_mock_only', 'browser_opened': False,
                    'elapsed_seconds': round(time.monotonic() - started, 2),
                    'first_background_receive_seconds': round(receive_elapsed, 2),
                    'observation_after_restart_seconds': round(restarted_elapsed, 2),
                    'server_processes': 2, 'process_ids_differed': first_pid != second_pid,
                    'background_inquiries_received': 2, 'staff_mock_sends': 1,
                    'scheduled_mock_sends': 1, 'outbox_records_before_restart': len(before),
                    'outbox_records_after_restart': len(after), 'duplicate_sends': 0,
                    'saved_draft_body_and_version_preserved': True,
                    'jobs_and_original_schedule_baselines_preserved': True,
                    'last_mail_sync_success': operation(path, 'gmail_poll')['last_success_at'],
                    'last_scheduler_success': operation(path, 'scheduler')['last_success_at'],
                    'limits': ['30-second loop observed for about two minutes or less; not a long-term uptime test',
                               'No real Gmail/OpenAI requests or production deployment were exercised',
                               'Production overdue-job startup holds require separate unit/integration coverage'],
                }
                return result
            except Exception:
                log.flush()
                log.seek(0)
                diagnostics = log.read()[-3000:]
                if diagnostics:
                    print(diagnostics, file=sys.stderr)
                raise
            finally:
                stop_server(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON observation report; never a DB or secret dump')
    args = parser.parse_args()
    result = run_check()
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n')
    print(rendered)


if __name__ == '__main__':
    main()
