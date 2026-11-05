"""Local real-adapter launcher guards; no external API requests or live sends."""
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.auth import Auth
from app.local_live import CALLBACK, LocalLive, MARKER, ROOT, SECRET_KEYS, URL


@pytest.fixture
def local(tmp_path, monkeypatch):
    # Keep launcher environment changes local to this test; no secret env dump.
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    for key in SECRET_KEYS:
        os.environ.pop(key, None)
    return LocalLive(tmp_path / 'local-live')


def initialize(local):
    return local.initialize('personal-admin', password_reader=lambda: 'distinct-local-test-passphrase')


def test_init_is_separate_private_live_data_without_customers_or_approvals(local):
    result = initialize(local)
    assert result['created']
    assert json.loads(local.config.read_text()) == {key: '' for key in SECRET_KEYS}
    for path in (local.folder, local.data, local.secrets, local.backups):
        assert path.stat().st_mode & 0o777 == 0o700
    for path in (local.config, local.db, local.marker):
        assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(local.db) as db:
        assert db.execute('SELECT role,production_ready FROM auth_users').fetchone() == ('admin', 1)
        assert db.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM template_versions WHERE approval='승인 완료'").fetchone()[0] == 0
        settings = {key: json.loads(value) for key, value in db.execute('SELECT key,value FROM app_settings')}
        assert settings['live_enabled'] is False and settings['operation_mode'] == 'verification'
    assert not (local.folder / 'app.sqlite3.mock-gmail.sqlite3').exists()


def test_repeat_init_preserves_password_and_secret_values(local):
    initialize(local)
    values = {key: 'test-value' for key in SECRET_KEYS}
    local.config.write_text(json.dumps(values))
    with sqlite3.connect(local.db) as db:
        before = db.execute('SELECT password_hash FROM auth_users').fetchone()[0]
    def must_not_prompt():
        pytest.fail('Existing account must not be silently reset')
    result = local.initialize('personal-admin', password_reader=must_not_prompt)
    assert not result['created']
    assert local.read_settings() == values
    with sqlite3.connect(local.db) as db:
        assert db.execute('SELECT password_hash FROM auth_users').fetchone()[0] == before


def test_exact_loopback_origin_and_json_credentials_win_over_shell(local):
    values = local.prepare(initialize=True)
    values['GAPYEAR_OPENAI_API_KEY'] = 'test-only-local-key'
    env = {'APP_ENV': 'production', 'APP_BASE_URL': 'https://old.example.test', 'COOKIE_SECURE': 'true',
           'APP_MAIL_MODE': 'mock', 'APP_AI_MODE': 'mock', 'GOOGLE_REFRESH_TOKEN': 'old-token',
           'OPENAI_API_KEY': 'old-api-key', 'GOOGLE_CLIENT_ID': 'old-client'}
    local.environment(values, env)
    assert env['APP_BASE_URL'] == URL == 'http://127.0.0.1:8001'
    assert CALLBACK == 'http://127.0.0.1:8001/api/connections/gmail/callback'
    assert env['APP_ENV'] == 'development' and env['COOKIE_SECURE'] == 'false'
    assert env['APP_REQUIRE_PRODUCTION_ACCOUNTS'] == 'true'
    assert env['APP_MAIL_MODE'] == env['APP_AI_MODE'] == 'live'
    assert env['APP_DATA_DIR'] == str(local.data)
    assert env['GOOGLE_TOKEN_FILE'] == str(local.token)
    assert env['GAPYEAR_OPENAI_API_KEY'] == 'test-only-local-key'
    assert 'GOOGLE_CLIENT_ID' not in env and 'GOOGLE_REFRESH_TOKEN' not in env and 'OPENAI_API_KEY' not in env


def test_config_errors_are_redacted_and_cannot_set_network_or_mode(local):
    local.prepare(initialize=True)
    local.config.write_text('{ private-secret-invalid-json')
    with pytest.raises(ValueError) as exc:
        local.read_settings()
    assert 'private-secret' not in str(exc.value)
    local.config.write_text(json.dumps({**{key: '' for key in SECRET_KEYS}, 'APP_BASE_URL': 'https://external.invalid'}))
    with pytest.raises(ValueError, match='네 항목'):
        local.read_settings()


def test_symlinks_public_paths_and_unmarked_existing_database_are_rejected(local, tmp_path):
    with pytest.raises(ValueError, match='공개'):
        LocalLive(ROOT / 'app' / 'static' / 'private-secrets')
    local.folder.mkdir()
    local.data.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='심볼릭'):
        local.prepare(initialize=True)
    local.data.unlink()
    local.data.mkdir()
    with sqlite3.connect(local.db) as db:
        db.execute('CREATE TABLE previous (value TEXT)')
    with pytest.raises(ValueError, match='기존 DB'):
        local.prepare(initialize=True)


def test_copied_demo_database_never_becomes_real_data(local):
    local.prepare(initialize=True)
    with sqlite3.connect(local.db) as db:
        db.execute('CREATE TABLE demo_meta (key TEXT,value TEXT)')
    with pytest.raises(ValueError, match='모의 검수'):
        local.prepare()


def test_running_lock_prevents_second_init_and_start(local):
    initialize(local)
    with local.locked():
        assert local.status()['launcher_process_running'] is True
        with pytest.raises(ValueError, match='이미 실행'):
            local.initialize('another-admin', password_reader=lambda: 'unused-safe-passphrase')
        with pytest.raises(ValueError, match='이미 실행'):
            local.start()
    assert local.status()['launcher_process_running'] is False


def test_start_backs_up_before_import_and_runs_only_fixed_loopback(local, monkeypatch):
    initialize(local)
    called = []
    fake_app = object()
    def load(name):
        assert name == 'app.main'
        backups = [path for path in local.backups.glob('gapyear-*.sqlite3') if '.sqlite3.' not in path.name]
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as db:
            assert db.execute('SELECT production_ready FROM auth_users').fetchone()[0] == 1
        assert os.environ['APP_BASE_URL'] == URL
        called.append('import')
        return SimpleNamespace(app=fake_app)
    monkeypatch.setattr('app.local_live.importlib.import_module', load)
    import uvicorn
    def run(app, **kwargs):
        assert app is fake_app
        assert kwargs == {'host': '127.0.0.1', 'port': 8001, 'workers': 1, 'proxy_headers': False, 'access_log': False}
        called.append('serve')
    monkeypatch.setattr(uvicorn, 'run', run)
    local.start()
    assert called == ['import', 'serve']


def test_status_only_discloses_presence_not_secrets_or_fake_success(local):
    initialize(local)
    local.config.write_text(json.dumps({key: 'private-local-value' for key in SECRET_KEYS}))
    result = local.status()
    assert all(result['configured'].values())
    assert 'private-local-value' not in json.dumps(result)
    assert result['observations'] == {}
    assert result['launcher_process_running'] is False
    assert result['oauth_token_file_exists'] is False
    assert result['url'] == URL


def test_configure_openai_uses_hidden_key_prompt_preserves_google_and_private_file(local, monkeypatch, capsys):
    local.prepare(initialize=True)
    before = {key: '' for key in SECRET_KEYS}
    before.update(GOOGLE_CLIENT_ID='google-client-test', GOOGLE_CLIENT_SECRET='google-secret-test')
    local.config.write_text(json.dumps(before))
    local.config.chmod(0o644)
    prompts = []
    def hidden(prompt):
        prompts.append(prompt)
        return 'test-only-replacement-key'
    monkeypatch.setattr('app.local_live.getpass.getpass', hidden)
    monkeypatch.setattr('builtins.input', lambda prompt: 'accessible-model-test')
    result = local.configure_openai()
    stored = local.read_settings()
    assert prompts and 'OpenAI API 키' in prompts[0]
    assert stored['GOOGLE_CLIENT_ID'] == before['GOOGLE_CLIENT_ID']
    assert stored['GOOGLE_CLIENT_SECRET'] == before['GOOGLE_CLIENT_SECRET']
    assert stored['GAPYEAR_OPENAI_API_KEY'] == 'test-only-replacement-key'
    assert stored['OPENAI_MODEL'] == 'accessible-model-test'
    assert local.config.stat().st_mode & 0o777 == 0o600
    assert all(result['configured'].values())
    assert 'test-only-replacement-key' not in json.dumps(result) + capsys.readouterr().out
    assert not list(local.secrets.glob('.oauth-*'))


def test_configure_openai_empty_value_keeps_existing_file(local, monkeypatch):
    local.prepare(initialize=True)
    original = local.config.read_bytes()
    for key, model in (('', 'test-model'), ('test-key', '   '), ('line\nbreak', 'test-model')):
        with pytest.raises(ValueError):
            local.configure_openai(key_reader=lambda: key, model_reader=lambda: model)
        assert local.config.read_bytes() == original
    import getpass
    import warnings
    def visible_fallback(_):
        warnings.warn('Password input may be echoed.', getpass.GetPassWarning)
    monkeypatch.setattr('app.local_live.getpass.getpass', visible_fallback)
    with pytest.raises(ValueError, match='숨겨 입력'):
        local.configure_openai(model_reader=lambda: 'test-model')
    assert local.config.read_bytes() == original


def test_configure_openai_requires_stopped_process_before_reading_key(local):
    local.prepare(initialize=True)
    original = local.config.read_bytes()
    def must_not_read():
        pytest.fail('Do not prompt or change keys while the server is running')
    with local.locked():
        with pytest.raises(ValueError, match='이미 실행'):
            local.configure_openai(key_reader=must_not_read, model_reader=must_not_read)
    assert local.config.read_bytes() == original
