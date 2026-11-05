"""Render startup/storage/backup contracts without provisioning or paid resources."""
import json
from pathlib import Path
import sqlite3

import pytest

from app.core import Service
from app.integrations import MockDrafter, MockGmail
from app.render_start import backup_once, prepare_environment, runtime_lock


def test_assigned_https_origin_is_used_before_main_import(tmp_path):
    env = {'RENDER_EXTERNAL_URL': 'https://assigned-by-provider.onrender.com', 'PORT': '10000'}
    config = prepare_environment(env, mount=tmp_path, require_mount=False)
    assert env['APP_BASE_URL'] == env['RENDER_EXTERNAL_URL']
    assert env['APP_ENV'] == 'production' and env['COOKIE_SECURE'] == 'true'
    assert env['APP_MAIL_MODE'] == env['APP_AI_MODE'] == 'live'
    assert config['port'] == 10000
    assert Path(env['APP_DATA_DIR']).parent == tmp_path
    assert Path(env['GOOGLE_TOKEN_FILE']).parent == Path(env['APP_SECRET_DIR'])
    for key in ('APP_DATA_DIR', 'APP_SECRET_DIR', 'GAPYEAR_BACKUP_DIR'):
        assert Path(env[key]).stat().st_mode & 0o777 == 0o700
    assert 'APP_BOOTSTRAP_PASSWORD' not in env


def test_no_manufactured_url_or_ephemeral_storage_fallback(tmp_path):
    with pytest.raises(ValueError, match='HTTPS'):
        prepare_environment({}, mount=tmp_path, require_mount=False)
    with pytest.raises(ValueError, match='영속 디스크'):
        prepare_environment({'RENDER_EXTERNAL_URL': 'https://assigned.onrender.com'}, mount=tmp_path)
    env = {'RENDER_EXTERNAL_URL': 'https://assigned.onrender.com', 'APP_DATA_DIR': str(tmp_path.parent / 'ephemeral')}
    with pytest.raises(ValueError, match='영속 디스크'):
        prepare_environment(env, mount=tmp_path, require_mount=False)
    with pytest.raises(ValueError, match='HTTPS'):
        prepare_environment({'RENDER_EXTERNAL_URL': 'http://assigned.onrender.com'}, mount=tmp_path, require_mount=False)


def test_secrets_backups_and_data_are_separate(tmp_path):
    origin = 'https://assigned.onrender.com'
    env = {'RENDER_EXTERNAL_URL': origin, 'APP_DATA_DIR': str(tmp_path / 'app'),
           'APP_SECRET_DIR': str(tmp_path / 'app' / 'secrets')}
    with pytest.raises(ValueError, match='분리'):
        prepare_environment(env, mount=tmp_path, require_mount=False)
    env = {'RENDER_EXTERNAL_URL': origin, 'GOOGLE_TOKEN_FILE': str(tmp_path / 'app' / 'token.json')}
    with pytest.raises(ValueError, match='비공개 인증정보'):
        prepare_environment(env, mount=tmp_path, require_mount=False)


def test_duplicate_render_process_is_blocked(tmp_path):
    with runtime_lock(tmp_path):
        with pytest.raises(RuntimeError, match='이미 동작'):
            with runtime_lock(tmp_path):
                pass
    with runtime_lock(tmp_path):
        pass


def test_render_backup_publishes_complete_families_and_retains_latest(tmp_path):
    path = tmp_path / 'app' / 'app.sqlite3'
    service = Service(str(path), MockGmail(path), MockDrafter())
    service.initialize()
    service.create_phone({'name': 'backup-test', 'phone': 'phone', 'body': '문의', 'state': 'S4'}, 'tester')
    for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
        with sqlite3.connect(str(path) + suffix) as con:
            con.execute('CREATE TABLE settings (name TEXT)')
            con.execute("INSERT INTO settings VALUES ('preserve')")
    backups = tmp_path / 'backups'
    first = backup_once(service, backups, retention_count=2)
    second = backup_once(service, backups, retention_count=2)
    third = backup_once(service, backups, retention_count=2)
    assert not first.exists() and second.exists() and third.exists()
    assert not list(backups.glob('.pending-*'))
    assert json.loads((third / 'complete.json').read_text())['contains_secrets'] is False
    with sqlite3.connect(third / 'app.sqlite3') as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 1
    for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
        assert (third / ('app.sqlite3' + suffix)).is_file()
    assert service.operations_status()['backup']['ok']


def test_existing_database_is_portably_backed_up_before_loader_migrates(tmp_path):
    from app.render_start import load_application
    data = tmp_path / 'data'
    data.mkdir()
    source = data / 'app.sqlite3'
    with sqlite3.connect(source) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE old_records (body TEXT)')
        con.execute("INSERT INTO old_records VALUES ('existing consultation')")
    for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
        with sqlite3.connect(str(source) + suffix) as con:
            con.execute('CREATE TABLE original_metadata (value TEXT)')
            con.execute("INSERT INTO original_metadata VALUES ('existing metadata')")
    config = {'data_dir': data, 'backup_dir': tmp_path / 'backups', 'retention_count': 7}
    calls = []
    def migrate_and_load():
        snapshots = list(config['backup_dir'].glob('snapshot-*'))
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert json.loads((snapshot / 'complete.json').read_text())['phase'] == 'pre-start'
        # Opening the standalone copied main file is enough; WAL sidecars are
        # not required and all existing content/schema predates the migration.
        with sqlite3.connect(snapshot / 'app.sqlite3') as con:
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert con.execute('SELECT body FROM old_records').fetchone()[0] == 'existing consultation'
            assert [r[1] for r in con.execute('PRAGMA table_info(old_records)')] == ['body']
        for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
            with sqlite3.connect(snapshot / ('app.sqlite3' + suffix)) as con:
                assert con.execute('SELECT value FROM original_metadata').fetchone()[0] == 'existing metadata'
        with sqlite3.connect(source) as con:
            con.execute('ALTER TABLE old_records ADD COLUMN new_field TEXT')
        calls.append('imported')
        return 'application'
    assert load_application(config, loader=migrate_and_load) == 'application'
    assert calls == ['imported']


def test_failed_prestart_backup_prevents_application_loading(tmp_path):
    from app.render_start import load_application
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'app.sqlite3').write_bytes(b'broken sqlite file')
    config = {'data_dir': data, 'backup_dir': tmp_path / 'backups', 'retention_count': 7}
    calls = []
    with pytest.raises(sqlite3.DatabaseError):
        load_application(config, loader=lambda: calls.append('must not migrate'))
    assert calls == []
    assert not list(config['backup_dir'].glob('snapshot-*'))
    assert not list(config['backup_dir'].glob('.pending-*'))


def test_first_install_loads_once_without_prestart_backup(tmp_path):
    from app.render_start import load_application
    config = {'data_dir': tmp_path / 'data', 'backup_dir': tmp_path / 'backups', 'retention_count': 7}
    calls = []
    assert load_application(config, loader=lambda: calls.append('imported')) is None
    assert calls == ['imported']
    assert not config['backup_dir'].exists()
