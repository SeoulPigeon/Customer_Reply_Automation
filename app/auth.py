"""Local staff accounts, expiring server sessions, and persistent login throttling."""
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path


class Auth:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS auth_users (
              username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, salt TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','staff')), active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS auth_sessions (
              token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_failures (username TEXT, at REAL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()

    def create_user(self, username, password, role='staff'):
        if not username.strip() or len(username) > 80 or role not in ('admin', 'staff'):
            raise ValueError('사용자 이름 또는 권한이 올바르지 않습니다.')
        if len(password) < 12:
            raise ValueError('비밀번호는 12자 이상이어야 합니다.')
        salt = secrets.token_hex(16)
        with self.connect() as db:
            db.execute('INSERT INTO auth_users(username,password_hash,salt,role) VALUES(?,?,?,?)',
                       (username, self.password_hash(password, salt), salt, role))

    def login(self, username, password):
        now = time.time()
        with self.connect() as db:
            db.execute('DELETE FROM auth_failures WHERE at<?', (now - 900,))
            if db.execute('SELECT COUNT(*) FROM auth_failures WHERE username=?', (username,)).fetchone()[0] >= 5:
                raise ValueError('로그인 시도가 너무 많습니다. 15분 후 다시 시도해주세요.')
            row = db.execute('SELECT * FROM auth_users WHERE username=? AND active=1', (username,)).fetchone()
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
                WHERE token_hash=? AND expires>? AND u.active=1''',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM auth_sessions WHERE token_hash=?',
                       (hashlib.sha256(token.encode()).hexdigest(),))
