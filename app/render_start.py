"""Single-process Render entry point with private persistent storage and backups.

No network requests or deployment purchases happen on importing this module.
Render's public URL is available only after a service has actually been created.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import urlparse

from app.operations import backup_family

LOG = logging.getLogger(__name__)
MOUNT = Path('/var/data')


def prepare_environment(environ=None, *, mount=MOUNT, require_mount=True):
    env = os.environ if environ is None else environ
    origin = (env.get('APP_BASE_URL') or env.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    parsed = urlparse(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Render가 발급한 RENDER_EXTERNAL_URL 또는 실제 HTTPS APP_BASE_URL이 필요합니다.')
    mount = Path(mount).resolve()
    if require_mount and not os.path.ismount(mount):
        raise ValueError('/var/data 영속 디스크 마운트가 없습니다. 임시 디스크로 운영하지 않습니다.')
    env['APP_BASE_URL'] = origin
    env['APP_ENV'] = 'production'
    env['COOKIE_SECURE'] = 'true'
    env.setdefault('APP_MAIL_MODE', 'live')
    env.setdefault('APP_AI_MODE', 'live')
    env.setdefault('ENABLE_SCHEDULER', 'true')
    if env['APP_MAIL_MODE'] != 'live' or env['APP_AI_MODE'] != 'live':
        raise ValueError('Render 운영 서비스에는 Gmail·OpenAI 실제 어댑터 설정이 필요합니다. 발송 활성화는 별도로 승인합니다.')
    defaults = {'APP_DATA_DIR': mount / 'app', 'APP_SECRET_DIR': mount / 'secrets',
                'GAPYEAR_BACKUP_DIR': mount / 'backups'}
    paths = {}
    for key, default in defaults.items():
        path = Path(env.get(key, str(default))).resolve()
        if mount not in path.parents:
            raise ValueError(f'{key}는 영속 디스크 하위의 별도 비공개 디렉터리여야 합니다.')
        env[key] = str(path)
        paths[key] = path
    if len(set(paths.values())) != 3 or any(a in b.parents for a in paths.values() for b in paths.values() if a != b):
        raise ValueError('상담 데이터·인증정보·백업 디렉터리를 분리해야 합니다.')
    token = Path(env.get('GOOGLE_TOKEN_FILE', str(paths['APP_SECRET_DIR'] / 'gmail-token.json'))).resolve()
    if paths['APP_SECRET_DIR'] not in token.parents:
        raise ValueError('Google 토큰 파일은 비공개 인증정보 디렉터리에 저장해야 합니다.')
    env['GOOGLE_TOKEN_FILE'] = str(token)
    try:
        port = int(env.get('PORT', '10000'))
        hours = int(env.get('GAPYEAR_BACKUP_INTERVAL_HOURS', '24'))
        retained = int(env.get('GAPYEAR_BACKUP_RETENTION_COUNT', '7'))
    except ValueError as exc:
        raise ValueError('PORT와 백업 간격·보관 수는 정수여야 합니다.') from exc
    if not 1024 <= port <= 65535 or not 1 <= hours <= 168 or not 2 <= retained <= 90:
        raise ValueError('PORT 또는 백업 설정이 허용 범위를 벗어났습니다.')
    for path in paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return {'origin': origin, 'port': port, 'mount': mount, 'data_dir': paths['APP_DATA_DIR'],
            'backup_dir': paths['GAPYEAR_BACKUP_DIR'], 'backup_hours': hours, 'retention_count': retained}


@contextmanager
def runtime_lock(mount):
    """A second app process must not start a second polling/backup loop."""
    path = Path(mount) / '.gapyear-runtime.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('이 영속 디스크에서 사이트 실행 프로세스가 이미 동작 중입니다.') from exc
        yield
    finally:
        os.close(fd)


def snapshot_family(source, directory, retention_count=7, *, phase='running'):
    """Snapshot raw SQLite files without constructing a Service or migrating it."""
    if type(retention_count) is not int or not 2 <= retention_count <= 90:
        raise ValueError('백업 보관 수는 2~90개여야 합니다.')
    folder = Path(directory)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.pending-', dir=folder))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = folder / ('snapshot-' + stamp)
    try:
        backup_family(source, staging / 'app.sqlite3')
        manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'format': 1,
                    'contains_secrets': False, 'main_file': 'app.sqlite3', 'phase': phase}
        manifest_path = staging / 'complete.json'
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o600)
        staging.rename(target)
        completed = sorted(path for path in folder.glob('snapshot-*')
                           if path.is_dir() and not path.is_symlink() and (path / 'complete.json').is_file())
        for expired in completed[:-retention_count]:
            shutil.rmtree(expired)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def backup_once(service, directory, retention_count=7):
    """Record the outcome of a complete operational backup family."""
    try:
        target = snapshot_family(service.db_path, directory, retention_count)
        service.record_operation('backup', True, {'snapshot': target.name, 'retention_count': retention_count,
                                                  'offsite_copy_configured': False})
        return target
    except Exception as exc:
        service.record_operation('backup', False, error=type(exc).__name__)
        raise


def load_application(config, *, loader=None):
    """A verified pre-start backup must precede any import-time DB migration."""
    source = Path(config['data_dir']) / 'app.sqlite3'
    if source.exists():
        # This raw snapshot path deliberately does not import app.main or create
        # Service. Any error propagates and stops startup before schema changes.
        target = snapshot_family(source, config['backup_dir'], config['retention_count'], phase='pre-start')
        LOG.info('Pre-start database snapshot completed: %s', target.name)
    if loader is not None:
        return loader()
    from app.main import app
    return app


class BackupLoop:
    def __init__(self, service, directory, hours=24, retained=7):
        self.service, self.directory = service, directory
        self.interval = hours * 3600
        self.retained = retained
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='private-sqlite-backup', daemon=True)

    def run(self):
        # Startup snapshot, then one per interval; restarts never postpone the
        # next backup indefinitely and the bounded history limits disk growth.
        while not self.stop.is_set():
            try:
                backup_once(self.service, self.directory, self.retained)
            except Exception as exc:
                LOG.error('Private database backup failed: %s', type(exc).__name__)
            self.stop.wait(self.interval)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=20)


def main():
    os.umask(0o077)
    config = prepare_environment()
    with runtime_lock(config['mount']):
        # Back up existing schema/data before the sole application import runs
        # migrations. A missing fresh database is initialized by that import.
        app = load_application(config)
        import uvicorn
        backup = BackupLoop(app.state.service, config['backup_dir'], config['backup_hours'], config['retention_count'])
        backup.start()
        try:
            # HTTPS and the public origin are explicit application settings. We
            # do not trust arbitrary X-Forwarded-* headers or expose client IPs.
            uvicorn.run(app, host='0.0.0.0', port=config['port'], workers=1,
                        proxy_headers=False, access_log=False, timeout_graceful_shutdown=45)
        finally:
            backup.close()


if __name__ == '__main__':
    main()
