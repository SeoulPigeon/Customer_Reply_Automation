import json
import os
import sqlite3
import shutil
from pathlib import Path

import pytest

from app.auth import Auth
from app.core import Service
from app.integrations import MockDrafter, MockGmail
from app.operations import backup_database, backup_family, restore_database


def database(tmp_path):
    source = tmp_path / 'source.sqlite3'
    service = Service(str(source), MockGmail(str(tmp_path / 'mock.sqlite3')), MockDrafter())
    service.initialize()
    auth = Auth(source)
    auth.create_user('backup-admin', 'unique-backup-test-password', 'admin')
    token, _ = auth.login('backup-admin', 'unique-backup-test-password')
    auth.issue_nonce(token, 'gmail_oauth', {'verifier': 'test-only-private-proof'})
    case = service.create_phone({'name': '복구 검증', 'phone': '010-test', 'body': '상담 보존', 'state': 'S4'}, 'backup-admin')
    return source, service, case


def test_snapshot_preserves_wal_and_templates_but_excludes_sessions(tmp_path):
    source, service, _ = database(tmp_path)
    service.approve_template('S4-01', True, 'backup-admin')
    target = backup_database(source, tmp_path / 'backups' / 'copy.sqlite3')
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM auth_nonces').fetchone()[0] == 0
        assert db.execute("SELECT approval FROM template_versions WHERE id='S4-01'").fetchone()[0] == '승인 완료'
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600
    portable = tmp_path / 'portable-copy.sqlite3'
    shutil.copyfile(target, portable)
    with sqlite3.connect(portable) as db:
        assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM auth_nonces').fetchone()[0] == 0
    assert not Path(str(target) + '-wal').exists()
    with pytest.raises(ValueError):
        backup_database(source, target)


def test_restore_only_new_directory_and_pauses_all_outbound_work(tmp_path):
    source, _, _ = database(tmp_path)
    target = restore_database(source, tmp_path / 'restored')
    with sqlite3.connect(target) as db:
        values = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM app_settings')}
        assert values['live_enabled'] is False
        assert values['auto_followups_paused'] is True
        assert values['connectivity_verified'] is False
        assert db.execute('SELECT followup_mode FROM cases').fetchone()[0] == 'paused'
        assert db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 0
    with pytest.raises(ValueError):
        restore_database(source, tmp_path / 'restored')
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 1


def test_restore_rejects_unrelated_database_without_overwriting_source(tmp_path):
    source = tmp_path / 'unrelated.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE other (id INTEGER)')
    with pytest.raises(ValueError):
        restore_database(source, tmp_path / 'restored')
    assert not (tmp_path / 'restored' / 'app.sqlite3').exists()
    assert source.is_file()


def test_backup_family_keeps_usage_and_scope_without_tokens(tmp_path):
    source, _, _ = database(tmp_path)
    for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
        with sqlite3.connect(str(source) + suffix) as db:
            db.execute('CREATE TABLE metadata (value TEXT)')
            db.execute("INSERT INTO metadata VALUES('preserved')")
    token = tmp_path / 'gmail-token.json'
    token.write_text('not-part-of-database-backups')
    output = backup_family(source, tmp_path / 'backups' / 'snapshot.sqlite3')
    restored = restore_database(output, tmp_path / 'restored')
    for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
        with sqlite3.connect(str(restored) + suffix) as db:
            assert db.execute('SELECT value FROM metadata').fetchone()[0] == 'preserved'
    assert not (tmp_path / 'backups' / 'gmail-token.json').exists()
