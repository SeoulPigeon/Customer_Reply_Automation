"""Operator commands. Run from the checkout: python -m app.cli --help."""
import argparse
import getpass
import os
import sqlite3
from pathlib import Path

from app.auth import Auth

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='한국갭이어 직원 관리 및 백업')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'create-user'):
        command = commands.add_parser(name)
        command.add_argument('--username', required=True)
        command.add_argument('--role', choices=['admin', 'staff'], default='admin' if name == 'init' else 'staff')
    command = commands.add_parser('backup')
    command.add_argument('--output', required=True)
    command = commands.add_parser('purge')
    command.add_argument('--days', type=int, default=None)
    command.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    folder = Path(os.getenv('APP_DATA_DIR', str(ROOT / 'var')))
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / 'app.sqlite3'
    if args.command in ('init', 'create-user'):
        if args.command == 'init':
            from app.core import Service
            from app.integrations import gmail_from_env, drafter_from_env
            Service(str(path), gmail_from_env(str(path)), drafter_from_env()).initialize(str(ROOT / 'data' / 'followup_templates.xlsx'))
        password = os.getenv('APP_BOOTSTRAP_PASSWORD') or getpass.getpass('비밀번호 (12자 이상): ')
        Auth(path).create_user(args.username, password, args.role)
        path.chmod(0o600)
        print(f'직원 계정을 생성했습니다: {args.username} ({args.role})')
    elif args.command == 'backup':
        if not path.is_file():
            parser.error('초기화된 데이터베이스가 없습니다.')
        output = Path(args.output).resolve()
        if output == path.resolve() or output.exists():
            parser.error('백업 경로는 아직 존재하지 않는 별도 파일이어야 합니다.')
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as source, sqlite3.connect(output) as destination:
            source.backup(destination)
            assert destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            destination.execute('DELETE FROM auth_sessions')
        print('백업과 무결성 검사를 완료했습니다. 복구본의 기존 로그인 세션은 제거됩니다.')
    elif args.command == 'purge':
        from app.core import Service
        from app.integrations import gmail_from_env, drafter_from_env
        service = Service(str(path), gmail_from_env(str(path)), drafter_from_env())
        if args.days is not None:
            service.update_settings({'retention_days': args.days}, 'cli')
        result = service.purge_closed(actor='cli', dry_run=not args.confirm)
        print(result)


if __name__ == '__main__':
    main()
