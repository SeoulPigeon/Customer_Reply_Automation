"""Isolated staff test drive: persistent virtual clock and exclusively local adapters."""
import argparse
from datetime import timedelta
import getpass
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from app.auth import Auth
from app.core import Service, dt, iso
from app.integrations import MockGmail, MockDrafter

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / 'var' / 'demo'
INQUIRY = '몰타 어학연수를 1월에 3주 정도 가려고 합니다. 참가 가능한가요? 비용도 궁금합니다.'
DAYS = [1, 3, 7, 14, 30]
INITIAL_TIME = '2026-11-05T10:00:00+09:00'


class DemoClock:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS demo_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO demo_meta VALUES(?,?)', ('now', iso(INITIAL_TIME)))
            db.execute('CREATE TABLE IF NOT EXISTS demo_cases(id TEXT PRIMARY KEY)')

    def __call__(self):
        with sqlite3.connect(self.path) as db:
            return dt(db.execute("SELECT value FROM demo_meta WHERE key='now'").fetchone()[0])

    def move(self, value):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE demo_meta SET value=? WHERE key='now'", (iso(value),))


class DemoMailbox(MockGmail):
    def __init__(self, path, clock):
        self.clock = clock
        super().__init__(path)

    def inject(self, message):
        return super().inject({**message, 'received_at': iso(self.clock())})


class DemoDrafter(MockDrafter):
    def generate(self, context):
        if '몰타' not in context.get('latest_inquiry', ''):
            return super().generate(context)
        return {
            'provider': 'mock',
            'body': '안녕하세요, 한국갭이어입니다. 몰타에서 1월에 약 3주간 어학연수를 계획하고 계시는군요.\n\n'
                    '참가 가능 여부와 비용은 희망하는 연도와 시작일, 어학 과정 및 숙소 조건을 확인해야 정확히 안내할 수 있습니다. '
                    '현재 문의만으로 가능 여부나 금액을 확정하기는 어렵습니다.\n\n'
                    '희망하시는 연도와 출발 가능한 날짜, 숙소 포함 여부를 알려주시면 해당 조건의 운영 여부와 비용을 확인해 안내드리겠습니다.',
            'checks': ['모의 응답 · 실제 GPT가 생성한 답변이 아닙니다. 검수용 고정 예시입니다.',
                       '1월의 연도는 미확정입니다. 직원이 확인해야 합니다.',
                       '실제 과정 운영 여부·잔여석·참가 자격·비용·포함 내역은 확인되지 않았습니다.'],
        }


class Demo:
    def __init__(self, folder=DEMO_DIR):
        folder = Path(folder).resolve()
        if folder == (ROOT / 'var').resolve():
            raise ValueError('운영 데이터 폴더는 검수용으로 사용할 수 없습니다.')
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / '.demo-only'
        path = folder / 'app.sqlite3'
        if path.exists() and not marker.exists():
            raise ValueError('기존 데이터베이스를 검수 데이터로 변경하지 않습니다. 새 폴더를 사용하세요.')
        if marker.exists() and marker.read_text() != 'gapyear-test-drive-v1':
            raise ValueError('검수 폴더 식별자가 올바르지 않습니다.')
        marker.write_text('gapyear-test-drive-v1')
        self.folder, self.path = folder, path
        self.lock = threading.RLock()
        self.clock = DemoClock(path)
        self.service = Service(path, DemoMailbox(path, self.clock), DemoDrafter(), clock=self.clock)
        self.auth = Auth(path)
        self.service.update_settings({'live_enabled': False, 'connectivity_verified': False}, 'demo-setup')
        # Only first-time setup approves these five copies. Later staff changes survive restart.
        with sqlite3.connect(path) as db:
            seeded = db.execute("SELECT value FROM demo_meta WHERE key='seeded'").fetchone()
        if not seeded:
            for step in range(1, 6):
                self.service.approve_template(f'S4-{step:02d}', True, 'demo-setup')
            self.scenario('demo-setup')
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO demo_meta VALUES('seeded','true')")
        path.chmod(0o600)

    def _case(self, case_id=None):
        with sqlite3.connect(self.path) as db:
            if case_id is None:
                row = db.execute("SELECT value FROM demo_meta WHERE key='case_id'").fetchone()
                case_id = row[0] if row else None
            if not db.execute('SELECT 1 FROM demo_cases WHERE id=?', (case_id,)).fetchone():
                raise ValueError('검수용 문의를 선택해주세요.')
        return self.service.get_case(case_id)

    def info(self, case_id=None):
        if case_id:
            with sqlite3.connect(self.path) as db:
                if not db.execute('SELECT 1 FROM demo_cases WHERE id=?', (case_id,)).fetchone():
                    case_id = None
        case = self._case(case_id)
        replies = [o for o in case['outbox'] if o['draft_id'] and o['status'] == 'sent']
        base = max((o['sent_at'] for o in replies), default=None)
        return {'enabled': True, 'now': iso(self.clock()), 'case_id': case['id'],
                'base_sent_at': base, 'days': DAYS, 'scenario': 'original',
                'approved_templates': [t['id'] for t in self.service.list_templates() if t['approval'] == '승인 완료'],
                'weekdays': self.service.settings()['weekdays'], 'actual_sending': False}

    def scenario(self, actor):
        with self.lock:
            token = uuid.uuid4().hex[:12]
            message = self.service.gmail.receive(email=f'malta-{token}@example.test', name='몰타 문의 · 가상 고객',
                subject='[검수] 몰타 1월 3주 어학연수 문의', body=INQUIRY)
            case = self.service.ingest_message(message)
            self.service.update_case(case['id'], {
                'email_allowed': True, 'consent_note': '직접 검수용 가상 고객: 로컬 모의 발송만 허용',
                'owner': actor, 'followup_mode': 'active',
                'fields': {'핵심 조건': '몰타 어학연수 1월 약 3주 참가 가능 여부와 비용'},
                'notes': '검수용 문의. 1월의 연도·실제 참가 가능 여부·프로젝트 운영 여부·비용은 미확정입니다.'
            }, actor)
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT INTO demo_cases VALUES(?)', (case['id'],))
                db.execute("INSERT OR REPLACE INTO demo_meta VALUES('case_id',?)", (case['id'],))
            return {'case_id': case['id'], 'inquiry': INQUIRY}

    def advance(self, day, case_id, actor):
        if type(day) is not int or day not in DAYS:
            raise ValueError('검수 시간 이동은 1·3·7·14·30일 중 선택해주세요.')
        with self.lock:
            case = self._case(case_id)
            if case['state'] != 'S4':
                raise ValueError('먼저 직원이 S4를 선택하고 답변을 승인·모의 발송해주세요.')
            with self.service.connect() as db:
                cycle = db.execute('SELECT * FROM cycles WHERE case_id=? AND active=1 ORDER BY base_sent_at DESC LIMIT 1',
                                   (case['id'],)).fetchone()
            if not cycle:
                raise ValueError('진행 중인 후속 주기가 없습니다. 상태 선택과 새 답변 승인·모의 발송을 완료해주세요.')
            target = dt(cycle['base_sent_at']) + timedelta(days=day)
            effective = self.service._next_allowed(target)
            now = self.clock()
            if effective < now:
                raise ValueError('검수 시간을 과거로 돌릴 수 없습니다. 새 검수 문의로 다른 흐름을 시험해주세요.')
            # Simulate intervening scheduled moments rather than delivering a backlog at once.
            moments = {effective}
            for job in self.service.list_jobs():
                moment = dt(job['due_at'])
                if job['status'] in ('pending', 'staff_check') and now <= moment <= effective:
                    moments.add(moment)
            results = []
            for moment in sorted(moments):
                self.clock.move(moment)
                self.service.poll_mail()
                results.extend(self.service.run_due()['results'])
            with self.service.transaction() as db:
                self.service._audit(db, actor, 'demo.clock_advanced', case['id'], {'day': day, 'now': iso(self.clock())})
            return {**self.info(case['id']), 'target_at': iso(target), 'effective_at': iso(effective),
                    'processed': len(results), 'results': results}

    def reply(self, case_id, body, actor):
        with self.lock:
            case = self._case(case_id)
            message = self.service.gmail.receive(email=case['email'], name=case['name'], thread_id=case['thread_id'],
                subject=case['subject'], body=body or '안내 감사합니다. 1월 둘째 주 시작도 가능한지 확인 부탁드립니다.')
            result = self.service.ingest_message(message)
            return {'case_id': result['id'], 'workflow': result['workflow'], 'now': iso(self.clock())}


def main():
    parser = argparse.ArgumentParser(description='가상 고객 전용 직접 검수 서버 (실제 외부 연결 차단)')
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    # Pin process defaults before importing main's ordinary application object.
    os.environ.update(APP_MAIL_MODE='mock', APP_AI_MODE='mock', APP_DATA_DIR=str(DEMO_DIR),
                      ENABLE_SCHEDULER='false', COOKIE_SECURE='false')
    demo = Demo()
    with demo.auth.connect() as db:
        users = db.execute('SELECT username FROM auth_users WHERE active=1 ORDER BY username').fetchall()
    if not users:
        username = input('새 검수 계정 아이디 [reviewer]: ').strip() or 'reviewer'
        password = getpass.getpass('새 검수 비밀번호 (12자 이상): ')
        confirmation = getpass.getpass('비밀번호 다시 입력: ')
        if password != confirmation:
            raise SystemExit('비밀번호가 일치하지 않습니다. 다시 실행해주세요.')
        demo.auth.create_user(username, password, 'admin')
        print(f'검수 계정이 생성되었습니다: {username}')
    else:
        print('기존 검수 계정 사용: ' + ', '.join(row['username'] for row in users))
    from app.main import create_app
    import uvicorn
    print('실제 Gmail·OpenAI·Sheets 연결이 차단된 로컬 검수 모드입니다.')
    print(f'서버 기동 성공 후 이 컴퓨터의 브라우저에서 http://127.0.0.1:{args.port} 에 접속하세요.')
    uvicorn.run(create_app(demo.service, demo.auth, scheduler=False, demo=demo), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
