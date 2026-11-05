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
