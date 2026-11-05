"""Offline operational backup and restore helpers; never contact external APIs."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SIDECARS = ('.gmail-state.sqlite3', '.ai-usage.sqlite3')


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def backup_database(source, destination):
    """Use SQLite's snapshot API, including committed WAL data, to a new private file."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('초기화된 데이터베이스가 없습니다.')
    if source == destination or destination.exists():
        raise ValueError('백업 경로는 아직 존재하지 않는 별도 파일이어야 합니다.')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('백업 무결성 검사에 실패했습니다.')
            tables = _tables(dst)
            for table in ('auth_sessions', 'auth_nonces', 'auth_oauth_states', 'oauth_states'):
                if table in tables:
                    dst.execute(f'DELETE FROM {table}')
            dst.commit()
            # A backup must be portable as a standalone file, even when the live
            # source uses WAL. Do not depend on Python GC closing connections.
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            dst.execute('PRAGMA journal_mode=DELETE')
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore_database(source, data_dir):
    """Restore only into an empty directory, with outbound work held for review."""
    folder = Path(data_dir).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('복구 대상은 비어 있는 새 디렉터리여야 합니다. 기존 데이터는 덮어쓰지 않습니다.')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(source).resolve()
    target = backup_database(source, folder / 'app.sqlite3')
    restored = [target]
    try:
        with closing(sqlite3.connect(target)) as db:
            required = {'app_settings', 'cases', 'template_versions', 'outbox'}
            if not required.issubset(_tables(db)):
                raise ValueError('한국갭이어 상담 데이터베이스 백업이 아닙니다.')
            changes = {'live_enabled': False, 'connectivity_verified': False,
                       'auto_followups_paused': True, 'operation_mode': 'verification'}
            for name, value in changes.items():
                db.execute('INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)',
                           (name, json.dumps(value)))
            db.execute("UPDATE cases SET followup_mode='paused' WHERE workflow!='closed'")
            tables = _tables(db)
            for table in ('runtime_status', 'operations'):
                if table in tables:
                    db.execute(f'DELETE FROM {table}')
            if 'jobs' in tables:
                columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
                if 'activation_review_required' in columns:
                    db.execute("UPDATE jobs SET activation_review_required=1 WHERE status IN ('pending','staff_check')")
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('복구 무결성 검사에 실패했습니다.')
        for suffix in SIDECARS:
            saved = Path(str(source) + suffix)
            if saved.is_file():
                copied = backup_database(saved, Path(str(target) + suffix))
                restored.append(copied)
        return target
    except Exception:
        for path in restored:
            path.unlink(missing_ok=True)
        raise


def backup_family(source, destination):
    """Snapshot main state and non-secret scope/usage databases, never OAuth tokens."""
    source, destination = Path(source), Path(destination)
    paths = [(source, destination)] + [
        (Path(str(source) + suffix), Path(str(destination) + suffix))
        for suffix in SIDECARS if Path(str(source) + suffix).is_file()
    ]
    if any(target.exists() for _, target in paths):
        raise ValueError('백업 파일이 이미 있습니다. 새 백업 이름을 지정하세요.')
    created = []
    try:
        for src, dst in paths:
            created.append(backup_database(src, dst))
        return created[0]
    except Exception:
        for target in created:
            target.unlink(missing_ok=True)
        raise


def dated_backup(source, directory):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return backup_family(source, Path(directory) / f'gapyear-{stamp}.sqlite3')
