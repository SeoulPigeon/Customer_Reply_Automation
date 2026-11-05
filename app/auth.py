"""Local staff accounts, expiring server sessions, and persistent login throttling."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path


class Auth:
    def __init__(self, path, production=False):
        self.path = str(path)
        self.production = production
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS auth_users (
              username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, salt TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','staff')), active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_failures (username TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS auth_nonces (
              nonce_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL,
              purpose TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auth_users)')}
            if 'production_ready' not in columns:
                db.execute('ALTER TABLE auth_users ADD COLUMN production_ready INTEGER NOT NULL DEFAULT 0')
            if production:
                db.execute('DELETE FROM auth_sessions WHERE username IN (SELECT username FROM auth_users WHERE production_ready=0)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    @staticmethod
    def validate_password(password, production=False):
        if not isinstance(password, str) or len(password) < 12:
            raise ValueError('비밀번호는 12자 이상이어야 합니다.')
        if production and any(word in password.lower() for word in ('password', '123456', 'qwerty', 'letmein')):
            raise ValueError('운영 계정에는 추측하기 쉬운 문자열이 없는 새로운 비밀번호를 사용하세요.')

    def create_user(self, username, password, role='staff', production=False):
        if not username.strip() or len(username) > 80 or role not in ('admin', 'staff'):
            raise ValueError('사용자 이름 또는 권한이 올바르지 않습니다.')
        production = production or self.production
        self.validate_password(password, production)
        salt = secrets.token_hex(16)
        with self.connect() as db:
            db.execute('INSERT INTO auth_users(username,password_hash,salt,role,production_ready) VALUES(?,?,?,?,?)',
                       (username, self.password_hash(password, salt), salt, role, int(production)))

    def reset_password(self, username, password, production=False):
        production = production or self.production
        self.validate_password(password, production)
        with self.connect() as db:
            row = db.execute('SELECT * FROM auth_users WHERE username=?', (username,)).fetchone()
            if not row:
                raise ValueError('직원 계정을 찾을 수 없습니다.')
            if hmac.compare_digest(self.password_hash(password, row['salt']), row['password_hash']):
                raise ValueError('기존 비밀번호와 다른 새로운 비밀번호를 사용하세요.')
            salt = secrets.token_hex(16)
            db.execute('UPDATE auth_users SET password_hash=?,salt=?,production_ready=?,active=1 WHERE username=?',
                       (self.password_hash(password, salt), salt, int(production), username))
            db.execute('DELETE FROM auth_sessions WHERE username=?', (username,))
            db.execute('DELETE FROM auth_failures WHERE username=?', (username,))

    def disable_user(self, username):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM auth_users WHERE username=?', (username,)).fetchone():
                raise ValueError('직원 계정을 찾을 수 없습니다.')
            db.execute('UPDATE auth_users SET active=0 WHERE username=?', (username,))
            db.execute('DELETE FROM auth_sessions WHERE username=?', (username,))

    def revoke_sessions(self, username=None):
        with self.connect() as db:
            if username is None:
                db.execute('DELETE FROM auth_sessions')
                db.execute('DELETE FROM auth_nonces')
            else:
                db.execute('DELETE FROM auth_sessions WHERE username=?', (username,))

    def login(self, username, password):
        now = time.time()
        with self.connect() as db:
            db.execute('DELETE FROM auth_failures WHERE at<?', (now - 900,))
            if db.execute('SELECT COUNT(*) FROM auth_failures WHERE username=?', (username,)).fetchone()[0] >= 5:
                raise ValueError('로그인 시도가 너무 많습니다. 15분 후 다시 시도해주세요.')
            row = db.execute('SELECT * FROM auth_users WHERE username=? AND active=1 AND (?=0 OR production_ready=1)',
                             (username, int(self.production))).fetchone()
            salt = row['salt'] if row else '0' * 32
            candidate = self.password_hash(password, salt)
            valid = row and hmac.compare_digest(candidate, row['password_hash'])
            if not valid:
                db.execute('INSERT INTO auth_failures VALUES(?,?)', (username, now))
            else:
                db.execute('DELETE FROM auth_failures WHERE username=?', (username,))
                db.execute('DELETE FROM auth_sessions WHERE expires<?', (now,))
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)',
                           (hashlib.sha256(token.encode()).hexdigest(), username, csrf, now + 43200))
        if not valid:
            raise ValueError('아이디 또는 비밀번호를 확인해주세요.')
        return token, {'username': username, 'role': row['role'], 'csrf_token': csrf}

    def session(self, token):
        if not token:
            return None
        with self.connect() as db:
            row = db.execute('''SELECT u.username,u.role,s.csrf AS csrf_token FROM auth_sessions s
                JOIN auth_users u ON u.username=s.username
                WHERE token_hash=? AND expires>? AND u.active=1 AND (?=0 OR u.production_ready=1)''',
                (hashlib.sha256(token.encode()).hexdigest(), time.time(), int(self.production))).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM auth_sessions WHERE token_hash=?',
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def issue_nonce(self, token, purpose, payload, ttl=600):
        """Short-lived server-side state bound to the exact initiating login session."""
        if not self.session(token):
            raise ValueError('로그인이 만료되었습니다.')
        nonce = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM auth_nonces WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO auth_nonces VALUES(?,?,?,?,?)',
                       (hashlib.sha256(nonce.encode()).hexdigest(), hashlib.sha256(token.encode()).hexdigest(),
                        purpose, json.dumps(payload), time.time() + ttl))
        return nonce

    def consume_nonce(self, token, purpose, nonce):
        if not token or not nonce or not self.session(token):
            raise ValueError('확인이 만료되었습니다. 다시 시작하세요.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM auth_nonces WHERE nonce_hash=?',
                             (hashlib.sha256(nonce.encode()).hexdigest(),)).fetchone()
            if (not row or row['expires'] <= time.time() or row['purpose'] != purpose or
                    not hmac.compare_digest(row['session_hash'], hashlib.sha256(token.encode()).hexdigest())):
                raise ValueError('확인 요청이 일치하지 않거나 만료되었습니다. 다시 시작하세요.')
            db.execute('DELETE FROM auth_nonces WHERE nonce_hash=?', (row['nonce_hash'],))
            return json.loads(row['payload'])
