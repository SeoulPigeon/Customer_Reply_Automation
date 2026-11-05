"""Operator commands. Run from the checkout: python -m app.cli --help."""
import argparse
import getpass
import os
from pathlib import Path

from app.auth import Auth
from app.operations import backup_family, dated_backup, restore_database

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='한국갭이어 직원 관리 및 백업')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'create-user'):
        command = commands.add_parser(name)
        command.add_argument('--username', required=True)
        command.add_argument('--role', choices=['admin', 'staff'], default='admin' if name == 'init' else 'staff')
    for name in ('reset-password', 'disable-user'):
        command = commands.add_parser(name)
        command.add_argument('--username', required=True)
    command = commands.add_parser('revoke-sessions')
    command.add_argument('--username')
    command = commands.add_parser('backup')
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument('--output')
    target.add_argument('--directory')
    command = commands.add_parser('restore')
    command.add_argument('--input', required=True)
    command.add_argument('--data-dir', required=True, help='비어 있는 새 복구 디렉터리')
    command = commands.add_parser('purge')
    command.add_argument('--days', type=int, default=None)
    command.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    folder = Path(os.getenv('APP_DATA_DIR', str(ROOT / 'var')))
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / 'app.sqlite3'
    production = os.getenv('APP_ENV') == 'production'
    if args.command in ('init', 'create-user'):
        if args.command == 'init':
            from app.core import Service
            from app.integrations import gmail_from_env, drafter_from_env
            Service(str(path), gmail_from_env(str(path)), drafter_from_env()).initialize(str(ROOT / 'data' / 'followup_templates.xlsx'))
        password = read_password()
        Auth(path, production=production).create_user(args.username, password, args.role, production=production)
        path.chmod(0o600)
        print(f'직원 계정을 생성했습니다: {args.username} ({args.role})')
    elif args.command == 'reset-password':
        Auth(path, production=production).reset_password(args.username, read_password(), production=production)
        print('비밀번호를 변경하고 해당 직원의 기존 세션을 모두 무효화했습니다.')
    elif args.command == 'disable-user':
        Auth(path, production=production).disable_user(args.username)
        print('직원 계정을 비활성화하고 기존 세션을 무효화했습니다.')
    elif args.command == 'revoke-sessions':
        Auth(path, production=production).revoke_sessions(args.username)
        print('지정한 범위의 로그인 세션을 무효화했습니다.')
    elif args.command == 'backup':
        output = dated_backup(path, args.directory) if args.directory else backup_family(path, args.output)
        print(f'백업·무결성 검사 완료: {output} (로그인 세션 제외)')
    elif args.command == 'restore':
        output = restore_database(args.input, args.data_dir)
        print(f'새 디렉터리 복구 완료: {output}')
        print('실제 발송과 자동 후속은 중지되어 있습니다. 연결·최신 메일·상담별 예약을 검토한 뒤 재개하세요.')
    elif args.command == 'purge':
        from app.core import Service
        from app.integrations import gmail_from_env, drafter_from_env
        service = Service(str(path), gmail_from_env(str(path)), drafter_from_env())
        if args.days is not None:
            service.update_settings({'retention_days': args.days}, 'cli')
        result = service.purge_closed(actor='cli', dry_run=not args.confirm)
        print(result)


def read_password():
    password = os.getenv('APP_BOOTSTRAP_PASSWORD')
    if password:
        return password
    password = getpass.getpass('새 개인 비밀번호 (12자 이상, 공유 테스트 비밀번호 사용 금지): ')
    if password != getpass.getpass('비밀번호 다시 입력: '):
        raise ValueError('두 비밀번호가 일치하지 않습니다.')
    return password


if __name__ == '__main__':
    main()
