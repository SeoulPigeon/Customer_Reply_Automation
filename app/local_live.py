"""Run the existing staff website with real adapters on this computer only.

Secrets and data are isolated from app.demo. No API call, message, customer seed,
configuration publication or cloud deployment is performed by initialization.
"""
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import importlib
import json
import os
from pathlib import Path
import sqlite3
import warnings

from app.auth import Auth
from app.operations import dated_backup

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = ROOT / 'var' / 'local-live'
URL = 'http://127.0.0.1:8001'
CALLBACK = URL + '/api/connections/gmail/callback'
MARKER = 'gapyear-local-live-v1'
SECRET_KEYS = ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GAPYEAR_OPENAI_API_KEY', 'OPENAI_MODEL')


class LocalLive:
    def __init__(self, folder=LOCAL_DIR):
        original = Path(folder).absolute()
        if original.is_symlink():
            raise ValueError('로컬 실제 연결 폴더는 심볼릭 링크일 수 없습니다.')
        self.folder = original.resolve()
        if self.folder in ((ROOT / 'var').resolve(), (ROOT / 'var' / 'demo').resolve()):
            raise ValueError('기존 모의·일반 데이터 폴더를 실제 연결용으로 변경하지 않습니다.')
        public = (ROOT / 'app' / 'static').resolve()
        if self.folder == public or public in self.folder.parents:
            raise ValueError('데이터와 비밀정보를 공개 파일 경로에 저장할 수 없습니다.')
        self.data = self.folder / 'data'
        self.secrets = self.folder / 'secrets'
        self.backups = self.folder / 'backups'
        self.config = self.secrets / 'settings.json'
        self.token = self.secrets / 'gmail-token.json'
        self.db = self.data / 'app.sqlite3'
        self.marker = self.folder / '.local-live-only'
        self.lock_path = self.folder / '.runtime.lock'

    def _private_paths(self):
        for path in (self.folder, self.data, self.secrets, self.backups, self.config,
                     self.token, self.db, self.marker, self.lock_path):
            if path.is_symlink():
                raise ValueError('로컬 실제 연결 파일과 폴더에 심볼릭 링크를 사용할 수 없습니다.')
        for suffix in ('.gmail-state.sqlite3', '.ai-usage.sqlite3'):
            if Path(str(self.db) + suffix).is_symlink():
                raise ValueError('연결 설정 데이터베이스에 심볼릭 링크를 사용할 수 없습니다.')

    def prepare(self, *, initialize=False):
        self._private_paths()
        if not self.marker.exists():
            if self.db.exists() or (self.folder / 'app.sqlite3').exists():
                raise ValueError('기존 DB를 실제 연결용으로 가져오지 않습니다. 원래 폴더를 보존하고 별도 초기화하세요.')
            if not initialize:
                raise ValueError('먼저 python -m app.local_live init --username 개인관리자ID 를 실행하세요.')
        elif self.marker.read_text() != MARKER:
            raise ValueError('로컬 실제 연결 폴더 식별자가 올바르지 않습니다.')
        for directory in (self.folder, self.data, self.secrets, self.backups):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        if not self.marker.exists():
            self._write_new(self.marker, MARKER)
        if not self.config.exists():
            if not initialize:
                raise ValueError('비밀 설정 파일이 없습니다. init을 다시 실행해 빈 설정 파일을 준비하세요.')
            self._write_new(self.config, json.dumps({key: '' for key in SECRET_KEYS}, ensure_ascii=False, indent=2) + '\n')
        for path in (self.marker, self.config, self.token, self.db):
            if path.exists():
                path.chmod(0o600)
        self._check_database()
        return self.read_settings()

    @staticmethod
    def _write_new(path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)

    def read_settings(self):
        self._private_paths()
        try:
            values = json.loads(self.config.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError('settings.json을 읽을 수 없습니다. JSON 형식과 파일 권한을 확인하세요. 실제 값은 로그에 출력하지 않습니다.') from exc
        if not isinstance(values, dict) or set(values) != set(SECRET_KEYS):
            raise ValueError('settings.json에는 GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GAPYEAR_OPENAI_API_KEY, OPENAI_MODEL 네 항목만 있어야 합니다.')
        if any(not isinstance(v, str) or '\n' in v or '\r' in v for v in values.values()):
            raise ValueError('설정값은 줄바꿈 없는 문자열로 입력하세요.')
        return {key: values[key].strip() for key in SECRET_KEYS}

    def environment(self, values, environ=None):
        env = os.environ if environ is None else environ
        # The explicit local file is the sole credential input for this launcher.
        # Old shell bindings cannot silently select another account or API key.
        for key in ('GOOGLE_ACCESS_TOKEN', 'GOOGLE_REFRESH_TOKEN', 'OPENAI_API_KEY', 'GMAIL_ADDRESS', 'GMAIL_ALIASES'):
            env.pop(key, None)
        for key in SECRET_KEYS:
            if values[key]:
                env[key] = values[key]
            else:
                env.pop(key, None)
        env.update(APP_ENV='development', APP_BASE_URL=URL, APP_DATA_DIR=str(self.data),
                   APP_SECRET_DIR=str(self.secrets), GOOGLE_TOKEN_FILE=str(self.token),
                   APP_MAIL_MODE='live', APP_AI_MODE='live', ENABLE_SCHEDULER='true',
                   COOKIE_SECURE='false', APP_REQUIRE_PRODUCTION_ACCOUNTS='true')
        return env

    def _check_database(self):
        if not self.db.exists():
            return
        with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'demo_meta' in tables:
                raise ValueError('모의 검수 데이터베이스를 실제 연결에 사용할 수 없습니다.')
            if 'cases' in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(cases)')}
                if 'data_mode' not in columns:
                    mixed = db.execute('SELECT 1 FROM cases LIMIT 1').fetchone()
                else:
                    mixed = db.execute("SELECT 1 FROM cases WHERE data_mode!='live' LIMIT 1").fetchone()
                if mixed:
                    raise ValueError('출처가 모의 또는 미확인인 상담이 있습니다. 모의 데이터를 실제 대상으로 전환하지 않습니다.')

    @contextmanager
    def locked(self):
        self._private_paths()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('로컬 실제 연결 프로그램이 이미 실행 중입니다. 해당 터미널에서 Ctrl+C로 종료한 뒤 다시 실행하세요.') from exc
            yield
        finally:
            os.close(fd)

    def backup_existing(self):
        if self.db.exists():
            return dated_backup(self.db, self.backups)
        return None

    def initialize(self, username, password_reader=None):
        if not isinstance(username, str) or not username.strip() or len(username) > 80:
            raise ValueError('사용할 개인 관리자 아이디를 입력하세요.')
        username = username.strip()
        values = self.prepare(initialize=True)
        with self.locked():
            self.environment(values)
            # An explicit repeated init never changes an existing user's password.
            if self.db.exists():
                with sqlite3.connect(self.db) as db:
                    table = db.execute("SELECT 1 FROM sqlite_master WHERE name='auth_users'").fetchone()
                    existing = db.execute('SELECT username FROM auth_users WHERE username=?', (username,)).fetchone() if table else None
                if existing:
                    return {'created': False, 'username': username, 'settings_file': str(self.config)}
            password = (password_reader or read_password)()
            Auth.validate_password(password, production=True)
            self.backup_existing()
            from app.core import Service
            from app.integrations import gmail_from_env, drafter_from_env
            service = Service(str(self.db), gmail_from_env(str(self.db)), drafter_from_env(str(self.db)))
            service.initialize(str(ROOT / 'data' / 'followup_templates.xlsx'))
            Auth(self.db, production=True).create_user(username, password, 'admin', production=True)
            self.db.chmod(0o600)
            return {'created': True, 'username': username, 'settings_file': str(self.config)}

    def configure_openai(self, key_reader=None, model_reader=None):
        values = self.prepare()
        with self.locked():
            key = (key_reader or read_openai_key)()
            model = (model_reader or (lambda: input('실제로 사용할 수 있는 OpenAI 모델 ID: ')))()
            if (not isinstance(key, str) or not isinstance(model, str)
                    or not key.strip() or not model.strip()):
                raise ValueError('키 또는 모델이 비어 있어 저장하지 않았습니다. 기존 설정을 유지합니다.')
            if any(char in value for value in (key, model) for char in ('\r', '\n')):
                raise ValueError('키와 모델에는 줄바꿈을 넣을 수 없습니다. 기존 설정을 유지합니다.')
            updated = {**values, 'GAPYEAR_OPENAI_API_KEY': key.strip(), 'OPENAI_MODEL': model.strip()}
            from app.integrations import write_secret_json
            try:
                write_secret_json(self.config, updated)
            except (OSError, RuntimeError) as exc:
                raise ValueError('OpenAI 설정을 저장하지 못했습니다. 파일 경로와 권한을 확인하세요.') from exc
            return {'settings_file': str(self.config),
                    'configured': {'GAPYEAR_OPENAI_API_KEY': True, 'OPENAI_MODEL': True}}

    def status(self):
        self._private_paths()
        values = self.read_settings() if self.config.exists() else {key: '' for key in SECRET_KEYS}
        running = False
        if self.marker.exists():
            try:
                with self.locked():
                    pass
            except ValueError:
                running = True
        observations = {}
        if self.db.is_file():
            with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone():
                    for name, success, ok, error in db.execute('SELECT name,last_success_at,ok,error FROM operations'):
                        if name in ('gmail_connection', 'gmail_poll', 'openai_connection', 'openai_draft', 'scheduler'):
                            observations[name] = {'last_success_at': success, 'last_attempt_ok': bool(ok), 'error': error}
        return {'url': URL, 'oauth_callback': CALLBACK, 'settings_file': str(self.config),
                'configured': {key: bool(values[key]) for key in SECRET_KEYS},
                'oauth_token_file_exists': self.token.is_file(), 'launcher_process_running': running,
                'observations': observations,
                'note': '설정값 존재와 실제 연결 성공은 다릅니다. 이 컴퓨터의 프로그램이 종료되거나 잠자기 상태면 수집·예약도 멈춥니다.'}

    def start(self):
        values = self.prepare()
        with self.locked():
            self.environment(values)
            if not self.db.is_file():
                raise ValueError('먼저 init으로 새 개인 관리자 계정을 만드세요.')
            with sqlite3.connect(self.db) as db:
                columns = {r[1] for r in db.execute('PRAGMA table_info(auth_users)')}
                ready = ('production_ready' in columns and db.execute("SELECT 1 FROM auth_users WHERE role='admin' AND active=1 AND production_ready=1 LIMIT 1").fetchone())
            if not ready:
                raise ValueError('새 개인 관리자 계정을 먼저 준비해야 합니다.')
            self.backup_existing()
            # app.main's global application initializes/migrates only after backup.
            app = importlib.import_module('app.main').app
            import uvicorn
            print(f'로컬 실제 연결 사이트: {URL}')
            print('실제 Gmail·OpenAI 연결은 관리자 설정에서 각각 확인하세요. 승인 전에는 발송되지 않습니다.')
            print('이 터미널과 맥북을 켜 두세요. 종료: Ctrl+C')
            uvicorn.run(app, host='127.0.0.1', port=8001, workers=1, proxy_headers=False, access_log=False)



def read_openai_key():
    # getpass normally warns and falls back to visible input without a terminal.
    # For API keys that fallback is deliberately unavailable.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass('새 OpenAI API 키 (입력 내용은 표시되지 않습니다): ')
        except getpass.GetPassWarning as exc:
            raise ValueError('키를 숨겨 입력할 수 있는 터미널에서 다시 실행하세요.') from exc


def read_password():
    value = getpass.getpass('새 개인 관리자 비밀번호 (12자 이상, 공유 테스트 비밀번호 재사용 금지): ')
    if value != getpass.getpass('비밀번호 다시 입력: '):
        raise ValueError('두 비밀번호가 일치하지 않습니다.')
    return value


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='기존 직원 사이트의 맥북 로컬 실제 Gmail·OpenAI 연결')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='별도 실제 연결 폴더와 새 관리자 준비')
    init.add_argument('--username', help='생략하면 개인 관리자 아이디를 물어봅니다.')
    sub.add_parser('start', help='127.0.0.1:8001에서 실행')
    sub.add_parser('status', help='비밀값 없이 설정 여부와 마지막 성공 기록 확인')
    sub.add_parser('configure-openai', help='키를 화면에 표시하지 않고 OpenAI 설정 저장')
    args = parser.parse_args()
    try:
        local = LocalLive()
        if args.command == 'init':
            username = args.username or input('새 개인 관리자 아이디: ').strip()
            result = local.initialize(username)
            print('새 개인 관리자 계정을 만들었습니다.' if result['created'] else '기존 계정과 비밀번호를 유지합니다.')
            print(f'비밀 설정 파일: {result["settings_file"]}')
            print(f'시작 후 접속 주소: {URL}')
            print(f'Google 인증 반환 주소: {CALLBACK}')
            print('설정 후 python -m app.local_live start 를 실행하세요.')
        elif args.command == 'status':
            print(json.dumps(local.status(), ensure_ascii=False, indent=2))
        elif args.command == 'configure-openai':
            result = local.configure_openai()
            print(f'비밀 설정 파일: {result["settings_file"]}')
            print('API 키 설정됨: true / 모델 설정됨: true (실제 연결 확인 전)')
            print('다시 시작: .venv/bin/python -m app.local_live start')
        else:
            local.start()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'실행하지 못했습니다: {exc}\n')


if __name__ == '__main__':
    main()
