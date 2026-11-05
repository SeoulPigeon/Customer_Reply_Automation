"""Durable, conservative OpenAI request budget; contains no prompts or secrets."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from zoneinfo import ZoneInfo


class AIUsage:
    def __init__(self, path=None):
        self.path = str(path) if path else ':memory:'
        self.lock = threading.RLock()
        if path:
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.memory = sqlite3.connect(':memory:', check_same_thread=False) if not path else None
        with self.connection() as con:
            con.executescript('''CREATE TABLE IF NOT EXISTS ai_limits (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_requests (
                id TEXT PRIMARY KEY,day TEXT NOT NULL,status TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL,tokens INTEGER,request_id TEXT,model TEXT,created_at TEXT NOT NULL);''')
            for key, default, env in [('daily_requests', 100, 'OPENAI_DAILY_REQUEST_LIMIT'),
                                      ('daily_tokens', 250000, 'OPENAI_DAILY_TOKEN_LIMIT'),
                                      ('alert_percent', 80, 'OPENAI_USAGE_ALERT_PERCENT')]:
                value = int(os.getenv(env, str(default)))
                con.execute('INSERT OR IGNORE INTO ai_limits VALUES (?,?)', (key, value))
        if path:
            os.chmod(path, 0o600)
        self._validate(self.limits())

    @contextmanager
    def connection(self):
        with self.lock:
            con = self.memory or sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                if not self.memory:
                    con.close()

    def _day(self):
        return datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()

    def _validate(self, values):
        if any(type(values.get(k)) is not int for k in ('daily_requests', 'daily_tokens', 'alert_percent')):
            raise ValueError('OpenAI usage limits must be integers')
        if not (1 <= values['daily_requests'] <= 10000 and 1000 <= values['daily_tokens'] <= 100000000 and 1 <= values['alert_percent'] <= 100):
            raise ValueError('OpenAI usage limits are outside the supported range')

    def limits(self):
        with self.connection() as con:
            return dict(con.execute('SELECT key,value FROM ai_limits').fetchall())

    def configure(self, values):
        combined = {**self.limits(), **values}
        if set(combined) != {'daily_requests', 'daily_tokens', 'alert_percent'}:
            raise ValueError('Unknown OpenAI usage limit')
        self._validate(combined)
        with self.connection() as con:
            for k, v in combined.items():
                con.execute('UPDATE ai_limits SET value=? WHERE key=?', (v, k))
        return self.status()

    def _today(self, con):
        r = con.execute('SELECT COUNT(*) AS requests,COALESCE(SUM(COALESCE(tokens,reserved_tokens)),0) AS tokens FROM ai_requests WHERE day=?', (self._day(),)).fetchone()
        return dict(r)

    def reserve(self, tokens, model):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            limits = dict(con.execute('SELECT key,value FROM ai_limits').fetchall())
            used = self._today(con)
            if used['requests'] >= limits['daily_requests'] or used['tokens'] + tokens > limits['daily_tokens']:
                raise ValueError('OpenAI daily usage limit reached; an administrator must review the budget')
            key = uuid.uuid4().hex
            con.execute('INSERT INTO ai_requests VALUES (?,?,?,?,?,?,?,?)',
                (key, self._day(), 'pending', tokens, None, None, model, datetime.now(ZoneInfo('UTC')).isoformat()))
            return key

    def finish(self, key, *, tokens=None, request_id=None, model=None, status='success'):
        with self.connection() as con:
            con.execute('UPDATE ai_requests SET status=?,tokens=?,request_id=?,model=COALESCE(?,model) WHERE id=?',
                        (status, tokens, request_id, model, key))

    def status(self):
        with self.connection() as con:
            used = self._today(con)
            limits = dict(con.execute('SELECT key,value FROM ai_limits').fetchall())
            last = con.execute('SELECT status,request_id,model,created_at FROM ai_requests ORDER BY created_at DESC LIMIT 1').fetchone()
        percent = max(used['requests'] / limits['daily_requests'], used['tokens'] / limits['daily_tokens']) * 100
        return {'day': self._day(), 'timezone': 'Asia/Seoul', **limits,
                'requests_used': used['requests'], 'tokens_used_or_reserved': used['tokens'],
                'alert': percent >= limits['alert_percent'], 'last_request': dict(last) if last else None,
                'budget_note': '토큰 수가 확인되지 않은 실패 요청은 최대 예상량으로 보수적으로 계산합니다. 비용 기준 한도는 OpenAI 프로젝트에서도 설정하세요.'}
