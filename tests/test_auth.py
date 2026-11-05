import sqlite3

import pytest

from app.auth import Auth


def test_passwords_sessions_and_logout(tmp_path):
    auth = Auth(tmp_path / 'auth.sqlite3')
    auth.create_user('admin', 'a-very-long-test-password', 'admin')
    token, user = auth.login('admin', 'a-very-long-test-password')
    assert user['role'] == 'admin'
    assert len(user['csrf_token']) > 30
    assert auth.session(token)['username'] == 'admin'
    with auth.connect() as db:
        assert db.execute('SELECT password_hash FROM auth_users').fetchone()[0] != 'a-very-long-test-password'
        assert db.execute('SELECT token_hash FROM auth_sessions').fetchone()[0] != token
    auth.logout(token)
    assert auth.session(token) is None


def test_throttling_and_inactive_accounts(tmp_path):
    auth = Auth(tmp_path / 'auth.sqlite3')
    auth.create_user('staff', 'a-very-long-test-password')
    for _ in range(5):
        with pytest.raises(ValueError):
            auth.login('staff', 'incorrect')
    with pytest.raises(ValueError, match='15분'):
        auth.login('staff', 'a-very-long-test-password')
    with auth.connect() as db:
        db.execute('DELETE FROM auth_failures')
    token, _ = auth.login('staff', 'a-very-long-test-password')
    with auth.connect() as db:
        db.execute("UPDATE auth_users SET active=0 WHERE username='staff'")
    assert auth.session(token) is None


def test_reject_weak_password_and_duplicate_user(tmp_path):
    auth = Auth(tmp_path / 'auth.sqlite3')
    with pytest.raises(ValueError):
        auth.create_user('admin', 'short')
    auth.create_user('admin', 'a-very-long-test-password')
    with pytest.raises(sqlite3.IntegrityError):
        auth.create_user('admin', 'another-test-password')


def test_production_requires_new_provisioning_and_invalidates_shared_sessions(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    development = Auth(path)
    development.create_user('shared-test', 'demo-long-credential', 'admin')
    old_token, _ = development.login('shared-test', 'demo-long-credential')
    production = Auth(path, production=True)
    assert production.session(old_token) is None
    with pytest.raises(ValueError):
        production.login('shared-test', 'demo-long-credential')
    with pytest.raises(ValueError, match='다른'):
        production.reset_password('shared-test', 'demo-long-credential', production=True)
    production.reset_password('shared-test', 'fresh-unique-credential', production=True)
    new_token, _ = production.login('shared-test', 'fresh-unique-credential')
    assert production.session(new_token)
    production.revoke_sessions('shared-test')
    assert production.session(new_token) is None
    new_token, _ = production.login('shared-test', 'fresh-unique-credential')
    production.disable_user('shared-test')
    assert production.session(new_token) is None


def test_oauth_nonce_is_session_bound_single_use_and_invalidated_on_logout(tmp_path):
    auth = Auth(tmp_path / 'auth.sqlite3')
    auth.create_user('admin', 'a-very-long-test-password', 'admin')
    first, _ = auth.login('admin', 'a-very-long-test-password')
    second, _ = auth.login('admin', 'a-very-long-test-password')
    nonce = auth.issue_nonce(first, 'oauth', {'verifier': 'temporary-pkce-value'})
    with pytest.raises(ValueError):
        auth.consume_nonce(second, 'oauth', nonce)
    assert auth.consume_nonce(first, 'oauth', nonce) == {'verifier': 'temporary-pkce-value'}
    with pytest.raises(ValueError):
        auth.consume_nonce(first, 'oauth', nonce)
    expired = auth.issue_nonce(first, 'oauth', {}, ttl=-1)
    with pytest.raises(ValueError):
        auth.consume_nonce(first, 'oauth', expired)
    pending = auth.issue_nonce(first, 'oauth', {})
    auth.logout(first)
    with pytest.raises(ValueError):
        auth.consume_nonce(first, 'oauth', pending)
