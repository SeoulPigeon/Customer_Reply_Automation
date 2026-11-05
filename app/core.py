"""Durable, fail-closed consultation and follow-up workflow.

SQLite is authoritative. Provider calls never determine customer stages. Every
outbound attempt gets a durable outbox reservation before network transmission.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
SEOUL = ZoneInfo('Asia/Seoul')
STATES = {f'S{i}' for i in range(7)}
SCHEDULES = {'S0':[3,7,14,30,60], 'S1':[3,7,14,30,60],
             'S2':[2,5,10,21,45], 'S3':[2,5,10,21,45],
             'S4':[1,3,7,14,30], 'S5':[1,3,7,14,30], 'S6':[1,2,5,7,14]}
TERMINAL = ('sent', 'cancelled', 'uncertain', 'sending')
DEFAULT_SETTINGS = {'business_start':9, 'business_end':18, 'weekdays':[0,1,2,3,4],
                    'timezone':'Asia/Seoul', 'live_enabled':False,
                    'legal_review_confirmed':False, 'connectivity_verified':False,
                    'retention_days':365, 'auto_followups_paused':False,
                    'operation_mode':'verification', 'verification_recipient':''}


def uid():
    return uuid.uuid4().hex


def dt(value):
    if isinstance(value, datetime):
        result = value
    elif value:
        result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    else:
        raise ValueError('날짜와 시간이 필요합니다.')
    if result.tzinfo is None:
        result = result.replace(tzinfo=SEOUL)
    return result.astimezone(UTC)


def iso(value):
    return dt(value).isoformat(timespec='microseconds')


def rowdict(row):
    return dict(row) if row else None


SCHEMA = '''
CREATE TABLE IF NOT EXISTS customers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, phone TEXT,
 email_allowed INTEGER NOT NULL DEFAULT 0, opt_out INTEGER NOT NULL DEFAULT 0,
 consent_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id), channel TEXT NOT NULL,
 subject TEXT NOT NULL, thread_id TEXT UNIQUE, state TEXT, state_revision INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0, selected_generation INTEGER NOT NULL DEFAULT -1,
 workflow TEXT NOT NULL DEFAULT 'state_selection', followup_mode TEXT NOT NULL DEFAULT 'active',
 owner TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '',
 payment TEXT NOT NULL DEFAULT 'unknown', payment_checked_at TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, last_inbound_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), provider_id TEXT UNIQUE,
 message_id TEXT, thread_id TEXT, direction TEXT NOT NULL, source TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_history (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), state TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), revision INTEGER NOT NULL,
 generation INTEGER NOT NULL, body TEXT NOT NULL, checks TEXT NOT NULL, provider TEXT NOT NULL,
 status TEXT NOT NULL, created_by TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS template_versions (
 id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
 days INTEGER NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, automation TEXT NOT NULL,
 conditions TEXT NOT NULL, approval TEXT NOT NULL, channel TEXT NOT NULL, note TEXT NOT NULL,
 original_hash TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, approved_by TEXT,
 approved_at TEXT, PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS cycles (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), state TEXT NOT NULL,
 revision INTEGER NOT NULL, generation INTEGER NOT NULL, base_sent_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, closed_reason TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), cycle_id TEXT NOT NULL REFERENCES cycles(id),
 template_id TEXT NOT NULL, template_version INTEGER NOT NULL, step INTEGER NOT NULL,
 original_due_at TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 sent_at TEXT, approved_by TEXT, cancelled_at TEXT, UNIQUE(cycle_id,step));
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,due_at);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), draft_id TEXT UNIQUE,
 job_id TEXT UNIQUE, status TEXT NOT NULL, body TEXT NOT NULL, recipient TEXT NOT NULL,
 subject TEXT NOT NULL, thread_id TEXT, in_reply_to TEXT, references_header TEXT,
 created_at TEXT NOT NULL, sent_at TEXT, actor TEXT NOT NULL, provider_id TEXT UNIQUE,
 provider_message_id TEXT, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', completed_at TEXT, completed_by TEXT);
CREATE TABLE IF NOT EXISTS audit (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT,
 details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deleted_messages (provider_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS operations (name TEXT PRIMARY KEY,last_attempt_at TEXT,last_success_at TEXT,ok INTEGER,error TEXT,details TEXT NOT NULL DEFAULT '{}');
'''


class DraftConflict(ValueError):
    """The caller's displayed revision is no longer the authoritative revision."""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Service:
    def __init__(self, db_path, gmail, drafter, clock=None):
        self.db_path = str(db_path)
        self.gmail, self.drafter = gmail, drafter
        self.clock = clock or (lambda: datetime.now(UTC))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def now(self):
        return dt(self.clock())

    def connect(self):
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA secure_delete=ON')
        c.execute('PRAGMA busy_timeout=30000')
        return c

    @contextlib.contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def initialize(self, template_path=None):
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)
        with self.transaction() as c:
            self._migrate(c)
            for k,v in DEFAULT_SETTINGS.items():
                c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',(k,json.dumps(v)))
            if not c.execute('SELECT 1 FROM template_versions LIMIT 1').fetchone():
                self._import_templates(c, template_path or Path(__file__).parent.parent/'data/followup_templates.xlsx')
        return {'templates':len(self.list_templates())}

    def _migrate(self,c):
        # Additive migrations preserve all text, versions, approvals and histories.
        columns={
            'cases':{'data_mode':"TEXT NOT NULL DEFAULT 'mock'",'mailbox_email':"TEXT NOT NULL DEFAULT ''",'purpose':"TEXT NOT NULL DEFAULT 'operational'",'company_owes_info':'INTEGER NOT NULL DEFAULT 0','pilot_enrolled':'INTEGER NOT NULL DEFAULT 0','pilot_followups_enabled':'INTEGER NOT NULL DEFAULT 0','pilot_enrolled_at':'TEXT','pilot_enrolled_by':'TEXT','pilot_recipient':"TEXT NOT NULL DEFAULT ''"},
            'drafts':{'edit_version':'INTEGER NOT NULL DEFAULT 1','generation_metadata':"TEXT NOT NULL DEFAULT '{}'",'data_mode':"TEXT NOT NULL DEFAULT 'mock'",'supersedes_id':'TEXT'},
            'cycles':{'data_mode':"TEXT NOT NULL DEFAULT 'mock'",'purpose':"TEXT NOT NULL DEFAULT 'operational'"},
            'jobs':{'data_mode':"TEXT NOT NULL DEFAULT 'mock'",'purpose':"TEXT NOT NULL DEFAULT 'operational'",'activation_review_required':'INTEGER NOT NULL DEFAULT 0'},
            'outbox':{'data_mode':"TEXT NOT NULL DEFAULT 'mock'",'approval_context':"TEXT NOT NULL DEFAULT '{}'"},
        }
        for table,fields in columns.items():
            present={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            for name,declaration in fields.items():
                if name not in present:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

    def _data_mode(self):
        return 'mock' if getattr(self.gmail,'is_mock',False) else 'live'

    def _mailbox_email(self):
        return str(getattr(self.gmail,'account_email','') or getattr(self.gmail,'email','') or '').lower()

    def record_operation(self,name,ok,details=None,error=None):
        with self.transaction() as c:
            self._record_operation(c,name,ok,details,error)

    def _record_operation(self,c,name,ok,details=None,error=None):
        now=iso(self.now())
        c.execute("""INSERT INTO operations(name,last_attempt_at,last_success_at,ok,error,details)
         VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
         last_success_at=CASE WHEN excluded.ok=1 THEN excluded.last_success_at ELSE operations.last_success_at END,
         ok=excluded.ok,error=excluded.error,details=excluded.details""",
         (name,now,now if ok else None,int(bool(ok)),error,json.dumps(details or {},ensure_ascii=False)))

    def operations_status(self):
        with self.connect() as c:
            records={r['name']:{**dict(r),'ok':bool(r['ok']),'details':json.loads(r['details'])} for r in c.execute('SELECT * FROM operations')}
        return records

    def _import_templates(self,c,path):
        import openpyxl
        workbook=openpyxl.load_workbook(path,read_only=True,data_only=True)
        try:
            rows=list(workbook['후속문구_S0-S6'].iter_rows(values_only=True))
            expected=['문구 ID','고객 상태','고객 단계','발송 시점','후속 역할','개정 문구(전문)','자동화 분류','필수 발송 조건','승인 상태','발송 채널','추가 참고']
            if list(rows[0]) != expected:
                raise ValueError('후속 문구 Excel 열이 기준과 다릅니다.')
            data=[r for r in rows[1:] if r[0]]
            if len(data)!=35 or {r[0] for r in data}!={f'S{s}-{n:02d}' for s in range(7) for n in range(1,6)}:
                raise ValueError('S0~S6 문구 35개가 정확하게 필요합니다.')
            for r in data:
                tid,state,stage,timing,role,body,automation,conditions,approval,channel,note=r
                days=int(re.fullmatch(r'(\d+)일 후',timing).group(1))
                if state not in STATES or days!=SCHEDULES[state][int(tid[-2:])-1] or approval!='승인 대기':
                    raise ValueError('Excel 상태·일정·초기 승인 상태가 기준과 다릅니다.')
                if automation!=('직원 확인 후 발송' if state=='S6' else '조건부 자동'):
                    raise ValueError('Excel 자동화 분류가 기준과 다릅니다.')
                c.execute('INSERT INTO template_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (tid,1,state,stage,days,role,body,automation,conditions,'승인 대기',channel,note or '',
                           hashlib.sha256(body.encode()).hexdigest(),'excel-import',iso(self.now()),None,None))
        finally:
            workbook.close()

    def _audit(self,c,actor,action,object_id=None,details=None):
        c.execute('INSERT INTO audit VALUES (?,?,?,?,?,?)',
                  (uid(),actor,action,object_id,json.dumps(details or {},ensure_ascii=False),iso(self.now())))

    def settings(self):
        with self.connect() as c:
            return {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM app_settings')}

    def update_settings(self,data,actor):
        values=self.settings()
        original_settings=dict(values)
        unknown=set(data)-set(DEFAULT_SETTINGS)
        if unknown: raise ValueError('알 수 없는 설정: '+', '.join(sorted(unknown)))
        values.update(data)
        if not isinstance(values['business_start'],int) or not isinstance(values['business_end'],int) or not 0<=values['business_start']<values['business_end']<=24:
            raise ValueError('영업시간 범위를 확인하세요.')
        if not values['weekdays'] or any(type(x) is not int or x not in range(7) for x in values['weekdays']):
            raise ValueError('발송 가능 요일을 선택하세요.')
        if any(type(values[k]) is not bool for k in ('live_enabled','legal_review_confirmed','connectivity_verified','auto_followups_paused')):raise ValueError('설정 확인 값은 true 또는 false여야 합니다.')
        if values['operation_mode'] not in ('verification','limited','production'):raise ValueError('운영 모드를 확인하세요.')
        recipient=str(values['verification_recipient']).strip().lower()
        if recipient and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',recipient):raise ValueError('확인용 수신 주소를 확인하세요.')
        values['verification_recipient']=recipient
        if values['timezone']!='Asia/Seoul': raise ValueError('시간대는 Asia/Seoul입니다.')
        if type(values['retention_days']) is not int or values['retention_days']<1: raise ValueError('보관 기간은 1일 이상입니다.')
        if values['live_enabled'] and not (values['legal_review_confirmed'] and values['connectivity_verified']):
            raise ValueError('실제 발송 전 연결 검증과 적법한 연락 조건의 관리자 확인이 필요합니다.')
        if self._data_mode()=='live' and values['operation_mode'] in ('limited','production') and (original_settings['operation_mode']!=values['operation_mode'] or (values['live_enabled'] and not original_settings['live_enabled'])):
            status=self.activation_status(values)
            if not status['ready']:raise ValueError('실제 운영 활성화 전 제한된 연결 검증이 필요합니다: '+', '.join(status['missing']))
        with self.transaction() as c:
            previous={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM app_settings')}
            if previous.get('verification_recipient')!=values['verification_recipient']:
                c.execute("DELETE FROM operations WHERE name LIKE 'verification_%'")
            if previous.get('operation_mode')!='limited' and values['operation_mode']=='limited':
                c.execute("UPDATE cases SET pilot_enrolled=0,pilot_followups_enabled=0 WHERE data_mode='live'")
                c.execute("UPDATE jobs SET status='staff_check',reason='제한 운영 시작: 관리자가 상담별 등록·후속 허용을 확인해야 합니다.',updated_at=? WHERE data_mode='live' AND purpose='operational' AND status IN ('pending','staff_check')",(iso(self.now()),))
            if (not previous.get('live_enabled') and values['live_enabled']) or (previous.get('operation_mode')!=values['operation_mode'] and values['operation_mode'] in ('limited','production')) or (previous.get('auto_followups_paused') and not values['auto_followups_paused']):
                c.execute("UPDATE jobs SET activation_review_required=1,status='staff_check',reason='운영 활성화 이전의 경과 예약: 직원 개별 검토 필요',updated_at=? WHERE data_mode='live' AND purpose='operational' AND status IN ('pending','staff_check') AND due_at<=?",(iso(self.now()),iso(self.now())))
            for k,v in values.items(): c.execute('UPDATE app_settings SET value=? WHERE key=?',(json.dumps(v),k))
            self._audit(c,actor,'settings.updated',details=data)
        return values

    def _next_allowed(self,value,settings=None):
        value=dt(value)
        s=settings or self.settings()
        local=value.astimezone(SEOUL)
        for _ in range(8):
            if local.weekday() in s['weekdays']:
                start=local.replace(hour=s['business_start'],minute=0,second=0,microsecond=0)
                end=(local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(hours=s['business_end']))
                if local<start: return start.astimezone(UTC)
                if local<end: return local.astimezone(UTC)
            local=(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        raise ValueError('허용 발송 시간을 계산할 수 없습니다.')

    def _case(self,c,case_id):
        result=c.execute('SELECT ca.*, cu.name,cu.email,cu.phone,cu.email_allowed,cu.opt_out,cu.consent_note FROM cases ca JOIN customers cu ON ca.customer_id=cu.id WHERE ca.id=?',(case_id,)).fetchone()
        if result is None: raise ValueError('상담을 찾을 수 없습니다.')
        return dict(result)

    def _cancel(self,c,customer_id,reason,case_id=None):
        clause='case_id IN (SELECT id FROM cases WHERE customer_id=?)'
        args=[customer_id]
        if case_id:
            clause='case_id=?';args=[case_id]
        now=iso(self.now())
        c.execute(f"UPDATE jobs SET status='cancelled',reason=?,cancelled_at=?,updated_at=? WHERE {clause} AND status IN ('pending','staff_check')",[reason,now,now]+args)
        c.execute(f'UPDATE cycles SET active=0,closed_reason=?,closed_at=? WHERE {clause} AND active=1',[reason,now]+args)

    QUEUES=('new_inquiries','state_selection','draft_review','new_replies','today_followups','sent_followups','staff_check','errors')

    def list_cases(self,queue=None):
        if queue is not None and queue not in self.QUEUES:raise ValueError('알 수 없는 대시보드 목록입니다.')
        with self.connect() as c:
            c.execute('BEGIN')
            rows=c.execute("""SELECT ca.*,cu.name,cu.email,cu.phone,cu.email_allowed,cu.opt_out,
            (SELECT min(due_at) FROM jobs j WHERE j.case_id=ca.id AND j.status IN ('pending','staff_check')) next_due
            FROM cases ca JOIN customers cu ON ca.customer_id=cu.id ORDER BY ca.updated_at DESC""").fetchall()
            jobs={};drafts={};attempts={}
            for table,target in [('jobs',jobs),('drafts',drafts),('outbox',attempts)]:
                for r in c.execute(f'SELECT * FROM {table}'):target.setdefault(r['case_id'],[]).append(dict(r))
            result=[]
            for row in rows:
                case=self._decode_case(dict(row))
                case['queues']=self._queues(case,jobs.get(case['id'],[]),drafts.get(case['id'],[]),attempts.get(case['id'],[]))
                if queue is None or queue in case['queues']:result.append(case)
            return result

    def _queues(self,case,jobs,drafts,attempts):
        active=case['workflow']!='closed'
        waiting=active and case['selected_generation']!=case['generation']
        today=self.now().astimezone(SEOUL).date()
        predicates={
            'new_inquiries':active and case['generation']==1 and case['state'] is None,
            'state_selection':waiting,
            'draft_review':active and any(d['status']=='draft' and d['revision']==case['state_revision'] and d['generation']==case['generation'] for d in drafts),
            'new_replies':waiting and case['generation']>1,
            'today_followups':any(j['status'] in ('pending','staff_check') and dt(j['due_at']).astimezone(SEOUL).date()==today for j in jobs),
            'sent_followups':any(j['status']=='sent' for j in jobs),
            'staff_check':active and (case['workflow']=='staff_check' or case['company_owes_info'] or any(j['status']=='staff_check' for j in jobs)),
            'errors':any(o['status'] in ('uncertain','sending') or (o['status']=='cancelled' and o['error']) for o in attempts) or any(j['status']=='uncertain' or ('Gmail 연결 오류' in j['reason'] and j['status']=='staff_check') for j in jobs),
        }
        return [q for q in self.QUEUES if predicates[q]]

    def _decode_case(self,r):
        r['fields']=json.loads(r.get('fields','{}'))
        for key in ('email_allowed','opt_out','company_owes_info','pilot_enrolled','pilot_followups_enabled'):
            if key in r:r[key]=bool(r[key])
        return r

    def get_case(self,case_id):
        with self.connect() as c:
            result=self._decode_case(self._case(c,case_id))
            result['customer']=dict(c.execute('SELECT * FROM customers WHERE id=?',(result['customer_id'],)).fetchone())
            result['history']=[dict(r) for r in c.execute('SELECT * FROM messages WHERE case_id=? ORDER BY received_at,created_at',(case_id,))]
            result['drafts']=[self._decode_draft(dict(r)) for r in c.execute('SELECT * FROM drafts WHERE case_id=? ORDER BY created_at DESC,rowid DESC',(case_id,))]
            result['jobs']=[dict(r) for r in c.execute('SELECT * FROM jobs WHERE case_id=? ORDER BY created_at,step',(case_id,))]
            result['state_history']=[dict(r) for r in c.execute('SELECT * FROM state_history WHERE case_id=? ORDER BY created_at',(case_id,))]
            result['calls']=[dict(r) for r in c.execute('SELECT * FROM calls WHERE case_id=? ORDER BY due_at',(case_id,))]
            result['outbox']=[dict(r) for r in c.execute('SELECT * FROM outbox WHERE case_id=? ORDER BY created_at',(case_id,))]
            result['latest_inquiry']=next((m for m in reversed(result['history']) if m['direction']=='inbound'),None)
            result['approval_context']={k:result[k] for k in ('email','subject','generation','state_revision')}
            result['next_due']=min((j['due_at'] for j in result['jobs'] if j['status'] in ('pending','staff_check')),default=None)
            latest=result['latest_inquiry']
            result['latest_reception_latency_seconds']=max(0,(dt(latest['created_at'])-dt(latest['received_at'])).total_seconds()) if latest and latest['source']=='gmail' else None
            result['operational_status']=self._operational_status(c,result)
            return result

    def _customer(self,c,email,name,phone=None):
        email=(email or '').strip().lower() or None
        if email and (not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email) or '\n' in email):
            raise ValueError('올바른 이메일 주소를 입력하세요.')
        existing=c.execute('SELECT * FROM customers WHERE email=?',(email,)).fetchone() if email else None
        if existing:return dict(existing)
        cid=uid();now=iso(self.now())
        c.execute('INSERT INTO customers(id,name,email,phone,created_at,updated_at) VALUES (?,?,?,?,?,?)',(cid,name or email or '고객',email,phone,now,now))
        return dict(c.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone())

    def ingest_message(self,message):
        with self.transaction() as c:
            result=self._ingest(c,message)
        return self.get_case(result) if result else {'ignored':True}

    def _ingest(self,c,m):
        pid=m.get('id') or m.get('provider_id')
        if not pid:raise ValueError('Gmail 메시지 ID가 필요합니다.')
        if c.execute('SELECT 1 FROM deleted_messages WHERE provider_id=?',(pid,)).fetchone():return None
        existing=c.execute('SELECT case_id FROM messages WHERE provider_id=?',(pid,)).fetchone()
        if existing:return existing['case_id']
        if m.get('is_automated'): return None
        direction=m.get('direction','inbound')
        if direction=='outbound' and c.execute('SELECT 1 FROM outbox WHERE (provider_id=? OR provider_message_id=?) AND recipient=?',(pid,m.get('message_id'),(m.get('email') or '').strip().lower())).fetchone():return None
        if direction not in ('inbound','outbound'):return None
        email=(m.get('email') or '').strip().lower()
        if not email: return None
        customer=self._customer(c,email,m.get('name'))
        thread=m.get('thread_id')
        case=c.execute('SELECT * FROM cases WHERE thread_id=?',(thread,)).fetchone() if thread else None
        if case and case['customer_id']!=customer['id']:
            # Group-mail threads are ambiguous: never attach a second identity.
            self._cancel(c,customer['id'],'여러 고객이 포함된 스레드: 직원 확인 필요')
            self._audit(c,'gmail','mail.ambiguous',details={'provider_id':pid})
            return None
        now=iso(self.now());received=iso(m.get('received_at') or now)
        if not case:
            case_id=uid()
            c.execute('INSERT INTO cases(id,customer_id,channel,subject,thread_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                      (case_id,customer['id'],'gmail',m.get('subject') or '(제목 없음)',thread,now,now))
            c.execute('UPDATE cases SET data_mode=?,mailbox_email=? WHERE id=?',(self._data_mode(),m.get('account_email') or self._mailbox_email(),case_id))
        else:
            case_id=case['id']
            if case['data_mode']!=self._data_mode():raise ValueError('모의 데이터와 실제 수신 데이터는 같은 상담에 연결할 수 없습니다.')
        c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),case_id,pid,m.get('message_id'),thread,direction,
                  'gmail_external' if direction=='outbound' else 'gmail',m.get('subject') or '',m.get('body') or '',received,now))
        reason='고객 새 답장 수신' if direction=='inbound' else '직원이 Gmail에서 직접 발송: 후속관리 재확인 필요'
        verification_cancelled=[r['id'] for r in c.execute("SELECT id FROM jobs WHERE case_id=? AND purpose='verification' AND status IN ('pending','staff_check')",(case_id,))]
        self._cancel(c,customer['id'],reason,case_id)
        c.execute("UPDATE drafts SET status='stale',edit_version=edit_version+1,updated_at=? WHERE case_id=? AND status='draft'",(now,case_id))
        # Ordinary replies affect the matched account/thread consultation only.
        # Contact refusal and uncertain transmission remain customer-wide gates.
        c.execute('UPDATE cases SET selected_generation=-1,pilot_followups_enabled=0,updated_at=? WHERE id=?',(now,case_id))
        if direction=='inbound':
            c.execute("UPDATE cases SET generation=generation+1,last_inbound_at=?,workflow='state_selection',updated_at=? WHERE id=?",(received,now,case_id))
            body=m.get('body') or ''
            if re.search(r'수신\s*거부|연락\s*(?:하지\s*마|중단)|메일\s*(?:보내지|그만)|unsubscribe|do not (?:email|contact)',body,re.I):
                c.execute('UPDATE customers SET opt_out=1,email_allowed=0,updated_at=? WHERE id=?',(now,customer['id']))
                self._cancel(c,customer['id'],'고객 연락 거부 요청')
                c.execute("UPDATE cases SET workflow='staff_check',followup_mode='stopped' WHERE customer_id=?",(customer['id'],))
        else:
            c.execute("UPDATE cases SET workflow='staff_check',followup_mode='paused' WHERE id=?",(case_id,))
        verification_case=self._case(c,case_id)
        if direction=='inbound' and verification_case['data_mode']=='live' and verification_case['purpose']=='verification':
            self._verification_event(c,'verification_gmail_received',verification_case,{'provider_id':pid})
            if verification_cancelled and c.execute("SELECT 1 FROM outbox WHERE case_id=? AND draft_id IS NOT NULL AND status='sent' AND sent_at<?",(case_id,received)).fetchone():
                self._verification_event(c,'verification_reply_received',verification_case,{'provider_id':pid,'cancelled_job_ids':verification_cancelled,'cancelled_job_count':len(verification_cancelled)})
        self._audit(c,'gmail','mail.received',case_id,{'provider_id':pid,'direction':direction})
        return case_id

    def poll_mail(self):
        try:
            messages=self.gmail.poll()
        except Exception as exc:
            with self.transaction() as c:
                c.execute("UPDATE jobs SET status='staff_check',reason='Gmail 연결 오류: 새 답장 확인 불가',updated_at=? WHERE status IN ('pending','staff_check')",(iso(self.now()),))
                self._record_operation(c,'gmail_poll',False,error='Gmail 연결 오류: '+type(exc).__name__)
                self._audit(c,'scheduler','gmail.poll_failed',details={'type':type(exc).__name__})
            return {'ok':False,'count':0,'error':'Gmail 연결 오류: 새 답장 확인 불가'}
        ids=[];latencies=[];new_messages=0;duplicates=0
        with self.transaction() as c:
            for m in sorted(messages,key=lambda x:x.get('received_at','')):
                pid=m.get('id') or m.get('provider_id')
                existed=c.execute('SELECT 1 FROM messages WHERE provider_id=?',(pid,)).fetchone()
                result=self._ingest(c,m)
                if result:ids.append(result)
                if existed:duplicates+=1
                elif result:
                    registered=c.execute('SELECT direction,source,received_at,created_at FROM messages WHERE provider_id=?',(pid,)).fetchone()
                    if registered:
                        new_messages+=1
                        if registered['direction']=='inbound' and registered['source']=='gmail':latencies.append(max(0,(dt(registered['created_at'])-dt(registered['received_at'])).total_seconds()))
            metrics={'count':len(ids),'new_messages':new_messages,'duplicate_messages':duplicates,'last_reception_latency_seconds':latencies[-1] if latencies else None,'max_reception_latency_seconds':max(latencies) if latencies else None}
            self._record_operation(c,'gmail_poll',True,metrics)
        return {'ok':True,**metrics,'case_ids':list(dict.fromkeys(ids))}

    def create_phone(self,data,actor):
        if not (data.get('body') or '').strip():raise ValueError('전화 상담 내용을 입력하세요.')
        if data.get('state') not in STATES:raise ValueError('S0~S6 상태를 선택하세요.')
        consulted=iso(data.get('consulted_at') or self.now())
        with self.transaction() as c:
            customer=self._customer(c,data.get('email'),data.get('name'),data.get('phone'))
            self._cancel(c,customer['id'],'새 전화 상담: 기존 무응답 예약 취소')
            case_id=uid();now=iso(self.now())
            c.execute('''INSERT INTO cases(id,customer_id,channel,subject,state,state_revision,generation,selected_generation,workflow,followup_mode,owner,created_at,updated_at,last_inbound_at)
             VALUES (?,?,?,?,?,1,1,1,'consulting','paused',?,?,?,?)''',
             (case_id,customer['id'],'phone',data.get('subject') or '전화 상담',data['state'],data.get('owner') or actor,now,now,consulted))
            c.execute('UPDATE cases SET data_mode=?,mailbox_email=? WHERE id=?',(self._data_mode(),self._mailbox_email(),case_id))
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),case_id,None,None,None,'inbound','phone','전화 상담',data['body'],consulted,now))
            c.execute('INSERT INTO state_history VALUES (?,?,?,?,?,?)',(uid(),case_id,data['state'],actor,now,1))
            if data.get('followup_at'):
                c.execute('INSERT INTO calls(id,case_id,due_at) VALUES (?,?,?)',(uid(),case_id,iso(data['followup_at'])))
            if data.get('email_allowed'):
                if not data.get('consent_note'):raise ValueError('이메일 연락 허용 근거를 입력하세요.')
                if customer['opt_out']:raise ValueError('연락 거부 고객입니다. 별도 재동의 확인이 필요합니다.')
                c.execute('UPDATE customers SET email_allowed=1,consent_note=?,updated_at=? WHERE id=?',(data['consent_note'],now,customer['id']))
            self._audit(c,actor,'phone.created',case_id)
        return self.get_case(case_id)

    def preview_state_change(self,case_id,state):
        if state not in STATES:raise ValueError('S0~S6 상태를 선택하세요.')
        with self.connect() as c:
            case=self._case(c,case_id)
            changed=case['state']!=state
            current=case['selected_generation']==case['generation']
            affected=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE case_id=? AND status IN ('pending','staff_check') ORDER BY due_at",(case_id,))] if changed else []
            return {'changed':changed,'requires_confirmation':bool(affected),'affected_jobs':affected,
                    'reason':'실제 상태 변경으로 기존 상태의 미발송 예약을 취소합니다.' if changed else ('같은 상태 저장: 초안·예약·기준 시각을 유지합니다.' if current else '새 문의를 확인한 뒤 같은 상태로 상담을 이어갑니다.'),
                    'current_state':case['state'],'requested_state':state,'state_revision':case['state_revision'],'generation':case['generation']}

    def select_state(self,case_id,state,actor,expected_revision=None,expected_generation=None,confirm_change=None):
        if state not in STATES:raise ValueError('S0~S6 상태를 선택하세요.')
        with self.transaction() as c:
            case=self._case(c,case_id)
            if expected_revision is not None and expected_revision!=case['state_revision']:raise DraftConflict('다른 직원이 고객 상태를 수정했습니다. 최신 상태를 확인하세요.')
            if expected_generation is not None and expected_generation!=case['generation']:raise DraftConflict('새 고객 문의가 도착했습니다. 최신 문의를 먼저 확인하세요.')
            if confirm_change is False and case['state']!=state and c.execute("SELECT 1 FROM jobs WHERE case_id=? AND status IN ('pending','staff_check')",(case_id,)).fetchone():raise DraftConflict('상태 변경으로 영향을 받는 예약을 먼저 확인하고 취소에 동의하세요.')
            if case['state']==state and case['selected_generation']==case['generation']:
                return self.get_case(case_id)
            self._cancel(c,case['customer_id'],'직원 고객 상태 변경' if state!=case['state'] else '새 문의 상태 확인',case_id)
            now=iso(self.now());revision=case['state_revision']+1
            c.execute("UPDATE cases SET state=?,state_revision=?,selected_generation=generation,pilot_followups_enabled=0,workflow='consulting',updated_at=? WHERE id=?",(state,revision,now,case_id))
            c.execute("UPDATE drafts SET status='stale',edit_version=edit_version+1,updated_at=? WHERE case_id=? AND status='draft'",(now,case_id))
            c.execute('INSERT INTO state_history VALUES (?,?,?,?,?,?)',(uid(),case_id,state,actor,now,revision))
            self._audit(c,actor,'state.selected',case_id,{'state':state,'revision':revision})
        return self.get_case(case_id)

    def update_case(self,case_id,data,actor):
        allowed={'email_allowed','consent_note','opt_out','followup_mode','workflow','owner','fields','notes','payment','name','phone','email','followup_at','company_owes_info'}
        if set(data)-allowed:raise ValueError('지원하지 않는 상담 수정 항목입니다.')
        with self.transaction() as c:
            case=self._case(c,case_id);now=iso(self.now())
            if any(type(data[k]) is not bool for k in ('email_allowed','opt_out','company_owes_info') if k in data):raise ValueError('연락 허용 값은 true 또는 false여야 합니다.')
            if data.get('followup_mode') not in (None,'active','paused','stopped'):raise ValueError('후속관리 상태를 확인하세요.')
            if data.get('workflow') not in (None,'consulting','staff_check','paused','closed'):raise ValueError('업무 상태를 확인하세요.')
            if data.get('payment') not in (None,'unknown','incomplete','complete','stopped'):raise ValueError('신청·결제 상태를 확인하세요.')
            if 'fields' in data:
                if not isinstance(data['fields'],dict) or any(not isinstance(v,(str,int,float,bool)) for v in data['fields'].values()):raise ValueError('확인된 조건은 이름과 값으로 입력하세요.')
            if data.get('email_allowed') and not (data.get('consent_note') or case['consent_note']).strip():raise ValueError('이메일 연락 허용 근거를 입력하세요.')
            if data.get('opt_out') is False and case['opt_out']:
                if not (data.get('consent_note') or '').strip():raise ValueError('연락 재동의의 확인 근거를 입력하세요.')
                data=dict(data);data['email_allowed']=False
            if data.get('email_allowed') and data.get('opt_out',case['opt_out']):raise ValueError('연락 거부 고객에게는 이메일을 허용할 수 없습니다.')
            for k in ('followup_mode','workflow','owner','notes','fields','payment','company_owes_info'):
                if k in data:
                    value=json.dumps(data[k],ensure_ascii=False) if k=='fields' else data[k]
                    c.execute(f'UPDATE cases SET {k}=?,updated_at=? WHERE id=?',(value,now,case_id))
            if 'payment' in data:c.execute('UPDATE cases SET payment_checked_at=? WHERE id=?',(now,case_id))
            for k in ('email_allowed','consent_note','opt_out','name','phone','email'):
                if k in data:
                    value=data[k]
                    if k=='email':
                        value=(value or '').strip().lower() or None
                        if value and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',value):raise ValueError('이메일 주소를 확인하세요.')
                    c.execute(f'UPDATE customers SET {k}=?,updated_at=? WHERE id=?',(value,now,case['customer_id']))
            if data.get('followup_at'):
                c.execute('INSERT INTO calls(id,case_id,due_at) VALUES (?,?,?)',(uid(),case_id,iso(data['followup_at'])))
            if data.get('opt_out') or data.get('email_allowed') is False:
                self._cancel(c,case['customer_id'],'연락 거부 또는 이메일 허용 해제')
            if data.get('workflow')=='closed' or data.get('followup_mode')=='stopped' or data.get('payment') in ('complete','stopped'):
                self._cancel(c,case['customer_id'],'상담 종료·후속관리 중단·신청/결제 완료 또는 중단',case_id)
            if data.get('followup_mode')=='paused' or data.get('workflow')=='paused':
                c.execute("UPDATE jobs SET status='staff_check',reason='직원이 후속관리를 보류했습니다.',updated_at=? WHERE case_id=? AND status='pending'",(now,case_id))
            if case['data_mode']=='live' and data.get('followup_mode')=='active' and case['followup_mode']!='active':
                c.execute("UPDATE jobs SET activation_review_required=1,status='staff_check',reason='후속관리 재개 이전의 경과 예약: 직원 개별 검토 필요',updated_at=? WHERE case_id=? AND status IN ('pending','staff_check') AND due_at<=?",(now,case_id,now))
            if data.get('company_owes_info'):
                self._cancel(c,case['customer_id'],'회사 운영정보 확인 후 답변 필요: 고객 무응답 후속 중지',case_id)
                c.execute("UPDATE cases SET workflow='staff_check' WHERE id=?",(case_id,))
            self._audit(c,actor,'case.updated',case_id,{'fields':list(data)})
        return self.get_case(case_id)

    def _decode_draft(self,r):
        r['checks']=json.loads(r['checks'])
        r['generation_metadata']=json.loads(r.get('generation_metadata','{}'))
        return r

    def _draft_scope_reason(self,case):
        if getattr(self.drafter,'is_mock',False):return None
        mode=self.settings()['operation_mode']
        if mode not in ('verification','limited'):return None
        if case['data_mode']!='live' or case['mailbox_email']!=self._mailbox_email() or not self._mailbox_email():
            return '실제 OpenAI에는 현재 연결된 Gmail 계정의 확인용·제한 운영 상담만 전달할 수 있습니다.'
        if mode=='verification':
            recipient=self.settings()['verification_recipient']
            if case['purpose']!='verification' or not recipient or case['email']!=recipient:
                return '실제 OpenAI 답변 생성 전 관리자가 지정한 수신자의 확인용 상담으로 등록하세요.'
        elif case['purpose']!='operational' or not case['pilot_enrolled'] or case['email']!=case['pilot_recipient']:
            return '실제 OpenAI 답변 생성 전 관리자가 이 상담과 현재 수신자를 제한 운영 대상으로 개별 등록해야 합니다.'
        return None

    def generate_draft(self,case_id,actor,regenerate=False,expected_version=None):
        with self.transaction() as c:
            case=self._case(c,case_id)
            if not case['state'] or case['selected_generation']!=case['generation']:raise ValueError('직원이 최신 문의의 S0~S6 상태를 먼저 선택해야 합니다.')
            previous=c.execute("SELECT * FROM drafts WHERE case_id=? AND revision=? AND generation=? AND status='draft' ORDER BY created_at DESC LIMIT 1",(case_id,case['state_revision'],case['generation'])).fetchone()
            if previous and not regenerate:return self._decode_draft(dict(previous))
            if previous and regenerate and expected_version!=previous['edit_version']:raise DraftConflict('재생성 전에 최신 직원 수정본을 확인하세요.')
            if not previous and regenerate and expected_version is not None:raise DraftConflict('초안이 변경되었습니다. 최신 상담 화면을 확인하세요.')
            scope_reason=self._draft_scope_reason(case)
            if scope_reason:raise ValueError(scope_reason)
            if not getattr(self.drafter,'is_mock',False) and self.settings()['operation_mode'] in ('verification','limited'):
                history=[dict(r) for r in c.execute('SELECT direction,body,subject,received_at,source FROM messages WHERE case_id=? ORDER BY received_at DESC LIMIT 30',(case_id,))][::-1]
            else:
                history=[dict(r) for r in c.execute('SELECT direction,body,subject,received_at,source FROM messages WHERE case_id IN (SELECT id FROM cases WHERE customer_id=? AND data_mode=? AND mailbox_email=?) ORDER BY received_at DESC LIMIT 30',(case['customer_id'],case['data_mode'],case['mailbox_email']))][::-1]
            latest=c.execute("SELECT body FROM messages WHERE case_id=? AND direction='inbound' ORDER BY received_at DESC,created_at DESC LIMIT 1",(case_id,)).fetchone()
            context={'state':case['state'],'customer_name':case['name'],'subject':case['subject'],'history':history,
                     'latest_inquiry':latest['body'] if latest else '',
                     'fields':json.loads(case['fields']),'notes':case['notes'],'channel':case['channel']}
            if case['data_mode']!=self._data_mode():raise ValueError('모의 상담은 실제 모드로 전환할 수 없습니다. 별도 운영 상담을 사용하세요.')
            result=self.drafter.generate(context)
            if not result.get('body','').strip():raise ValueError('답변 초안 생성 결과가 비어 있습니다.')
            draft_id=uid();now=iso(self.now())
            if previous:
                c.execute("UPDATE drafts SET status='superseded',edit_version=edit_version+1,updated_at=? WHERE id=?",(now,previous['id']))
            c.execute('INSERT INTO drafts(id,case_id,revision,generation,body,checks,provider,status,created_by,approved_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(draft_id,case_id,case['state_revision'],case['generation'],result['body'],json.dumps(result.get('checks',[]),ensure_ascii=False),result.get('provider','unknown'),'draft',actor,None,now,now))
            metadata={k:result[k] for k in ('model','generated_at','request_id','usage') if k in result}
            metadata.setdefault('generated_at',now)
            draft_mode='live' if case['data_mode']=='live' and str(result.get('provider','')).lower()=='openai' else 'mock'
            c.execute('UPDATE drafts SET generation_metadata=?,data_mode=?,supersedes_id=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),draft_mode,previous['id'] if previous else None,draft_id))
            c.execute("UPDATE cases SET workflow='draft_review',updated_at=? WHERE id=?",(now,case_id))
            if case['data_mode']=='live' and case['purpose']=='verification' and str(result.get('provider','')).lower()=='openai':self._verification_event(c,'verification_openai_generated',case,metadata)
            self._record_operation(c,'openai_draft',True,{**metadata,'provider':result.get('provider','unknown'),'draft_id':draft_id,'case_id':case_id,'data_mode':draft_mode})
            self._audit(c,actor,'draft.generated',draft_id)
            return self._decode_draft(dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()))

    def update_draft(self,draft_id,body,actor,expected_version=None):
        if not body.strip():raise ValueError('발송 본문을 입력하세요.')
        with self.transaction() as c:
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft:raise ValueError('초안을 찾을 수 없습니다.')
            if expected_version is not None and expected_version!=draft['edit_version']:raise DraftConflict('다른 직원 또는 창에서 수정한 최신 초안이 있습니다. 작성 내용을 보존한 뒤 비교하세요.')
            if draft['status']!='draft':raise DraftConflict('새 문의 또는 초안 재생성으로 이전 초안의 수정이 중단되었습니다. 작성 내용을 보존하세요.')
            if body==draft['body']:return self._decode_draft(dict(draft))
            c.execute('UPDATE drafts SET body=?,edit_version=edit_version+1,updated_at=? WHERE id=?',(body,iso(self.now()),draft_id))
            self._audit(c,actor,'draft.edited',draft_id)
            return self._decode_draft(dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()))

    VERIFICATION_REQUIRED=('verification_gmail_received','verification_openai_generated','verification_staff_sent','verification_reply_received','verification_followup_sent')

    def _verification_event(self,c,name,case,details=None):
        self._record_operation(c,name,True,{**(details or {}),'case_id':case['id'],'mailbox_email':case['mailbox_email'],'recipient':case['email']})

    def activation_status(self,settings=None):
        values=settings or self.settings()
        records=self.operations_status();mailbox=self._mailbox_email();recipient=values['verification_recipient']
        missing=[]
        for name in self.VERIFICATION_REQUIRED:
            record=records.get(name,{})
            detail=record.get('details',{})
            if not record.get('ok') or not mailbox or not recipient or detail.get('mailbox_email')!=mailbox or detail.get('recipient')!=recipient:missing.append(name)
        return {'ready':not missing,'required':list(self.VERIFICATION_REQUIRED),'missing':missing,
                'evidence':{k:records[k] for k in self.VERIFICATION_REQUIRED if k in records}}

    def mark_verification_case(self,case_id,actor):
        settings=self.settings()
        with self.transaction() as c:
            case=self._case(c,case_id)
            if self._data_mode()!='live' or case['data_mode']!='live':raise ValueError('실제 Gmail로 수신한 별도 상담만 확인용으로 지정할 수 있습니다.')
            if not settings['verification_recipient'] or case['email']!=settings['verification_recipient']:raise ValueError('관리자가 지정한 확인용 수신자와 일치하지 않습니다.')
            if settings['operation_mode']!='verification':raise ValueError('제한된 확인 모드에서만 사용할 수 있습니다.')
            if c.execute('SELECT 1 FROM outbox WHERE case_id=?',(case_id,)).fetchone():raise ValueError('발송 이력이 없는 별도 확인용 상담을 사용하세요.')
            self._cancel(c,case['customer_id'],'확인용 상담 분리',case_id)
            c.execute("UPDATE cases SET purpose='verification',updated_at=? WHERE id=?",(iso(self.now()),case_id))
            case['purpose']='verification'
            latest=c.execute("SELECT provider_id FROM messages WHERE case_id=? AND direction='inbound' AND source='gmail' ORDER BY received_at DESC LIMIT 1",(case_id,)).fetchone()
            if latest:self._verification_event(c,'verification_gmail_received',case,{'provider_id':latest['provider_id']})
            self._audit(c,actor,'verification.case_selected',case_id)
        return self.get_case(case_id)

    def schedule_verification_followup(self,case_id,template_id,due_at,actor):
        target=dt(due_at)
        if target<self.now() or target>self.now()+timedelta(days=7):raise ValueError('확인용 예약은 현재 이후 7일 이내로 지정하세요.')
        with self.transaction() as c:
            case=self._case(c,case_id)
            if case['purpose']!='verification' or case['data_mode']!='live':raise ValueError('별도 실제 확인용 상담만 사용할 수 있습니다.')
            reason=self._contact_reason(case)
            if reason:raise ValueError(reason)
            if case['company_owes_info']:raise ValueError('회사 확인 업무가 남아 있습니다.')
            if c.execute("SELECT 1 FROM jobs WHERE case_id=? AND purpose='verification' AND status NOT IN ('cancelled')",(case_id,)).fetchone():raise ValueError('확인용 후속 예약은 상담별 한 건만 허용됩니다.')
            sent=c.execute("SELECT o.* FROM outbox o JOIN drafts d ON o.draft_id=d.id WHERE o.case_id=? AND o.status='sent' AND o.data_mode='live' AND o.recipient=? AND d.revision=? AND d.generation=? ORDER BY o.sent_at DESC LIMIT 1",(case_id,case['email'],case['state_revision'],case['generation'])).fetchone()
            if not sent:raise ValueError('최신 문의에 실제 직원 승인 답장이 정상 발송된 뒤 예약하세요.')
            template=c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC LIMIT 1',(template_id,)).fetchone()
            if not template or template['state']!=case['state'] or template['approval']!='승인 완료':raise ValueError('현재 고객 상태에 맞는 관리자 승인 문구만 검증할 수 있습니다.')
            cycle_id=uid();job_id=uid();now=iso(self.now())
            c.execute("INSERT INTO cycles(id,case_id,state,revision,generation,base_sent_at,data_mode,purpose) VALUES(?,?,?,?,?,?,'live','verification')",(cycle_id,case_id,case['state'],case['state_revision'],case['generation'],sent['sent_at']))
            c.execute("""INSERT INTO jobs(id,case_id,cycle_id,template_id,template_version,step,original_due_at,due_at,created_at,updated_at,data_mode,purpose)
             VALUES(?,?,?,?,?,1,?,?,?,?,'live','verification')""",(job_id,case_id,cycle_id,template_id,template['version'],iso(target),iso(self._next_allowed(target)),now,now))
            self._audit(c,actor,'verification.followup_scheduled',job_id,{'template_id':template_id,'due_at':iso(target),'normal_schedule_unchanged':True})
            return dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())

    def pilot_status(self):
        with self.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT ca.id,ca.pilot_enrolled_at,ca.pilot_enrolled_by,ca.pilot_followups_enabled,cu.name,cu.email FROM cases ca JOIN customers cu ON ca.customer_id=cu.id WHERE ca.pilot_enrolled=1 ORDER BY ca.pilot_enrolled_at")]
        return {'maximum':5,'enrolled_count':len(rows),'cases':rows}

    def enroll_pilot_case(self,case_id,actor,enabled=True):
        if type(enabled) is not bool:raise ValueError('제한 운영 등록 여부는 true 또는 false여야 합니다.')
        settings=self.settings()
        if enabled:
            if self._data_mode()!='live' or settings['operation_mode']!='limited':raise ValueError('실제 제한 운영 모드에서 상담을 개별 등록하세요.')
            if not self.activation_status()['ready']:raise ValueError('현재 Gmail 계정의 실제 연결·발송·답장 취소 검증을 먼저 완료하세요.')
        with self.transaction() as c:
            case=self._case(c,case_id)
            if enabled:
                if case['data_mode']!='live' or case['purpose']!='operational' or case['mailbox_email']!=self._mailbox_email():raise ValueError('현재 연결된 계정의 실제 운영 상담만 등록할 수 있습니다.')
                if not case['email']:raise ValueError('제한 운영 이메일 수신 주소가 필요합니다.')
                if case['opt_out'] or case['workflow']=='closed' or case['payment'] in ('complete','stopped'):raise ValueError('연락 거부·종료·신청 완료 상담은 등록할 수 없습니다.')
                count=c.execute('SELECT count(*) FROM cases WHERE pilot_enrolled=1 AND id!=?',(case_id,)).fetchone()[0]
                if count>=5:raise ValueError('제한 운영 대상은 최대 5건입니다. 기존 대상을 해제한 뒤 등록하세요.')
                if not case['pilot_enrolled'] or case['pilot_recipient']!=case['email']:
                    c.execute('UPDATE cases SET pilot_enrolled=1,pilot_followups_enabled=0,pilot_recipient=?,pilot_enrolled_at=?,pilot_enrolled_by=? WHERE id=?',(case['email'],iso(self.now()),actor,case_id))
                    c.execute("UPDATE jobs SET status='staff_check',reason='제한 운영 후속 연락 개별 활성화 필요',updated_at=? WHERE case_id=? AND status IN ('pending','staff_check')",(iso(self.now()),case_id))
                    self._audit(c,actor,'pilot.enrolled',case_id)
            else:
                c.execute('UPDATE cases SET pilot_enrolled=0,pilot_followups_enabled=0 WHERE id=?',(case_id,))
                c.execute("UPDATE jobs SET status='staff_check',reason='관리자가 제한 운영 등록을 해제했습니다.',updated_at=? WHERE case_id=? AND status IN ('pending','staff_check')",(iso(self.now()),case_id))
                self._audit(c,actor,'pilot.removed',case_id)
        return self.get_case(case_id)

    def _template_readiness(self,c,case):
        templates=[dict(r) for r in c.execute('SELECT t.* FROM template_versions t WHERE state=? AND version=(SELECT max(version) FROM template_versions x WHERE x.id=t.id) ORDER BY id',(case['state'],))]
        fields=json.loads(case['fields']) if isinstance(case['fields'],str) else case['fields']
        approved=[t for t in templates if t['approval']=='승인 완료']
        eligible=[]
        for template in approved:
            needed=re.findall(r'\[([^\]]+)\]',template['body']+' '+template['conditions'])
            if any(not str(fields.get(key,'')).strip() for key in needed):continue
            if template['id']=='S3-01' and (not fields.get('A 프로젝트') or fields.get('A 프로젝트')==fields.get('B 프로젝트')):continue
            eligible.append(template)
        return templates,approved,eligible

    def set_pilot_followups(self,case_id,enabled,actor):
        if type(enabled) is not bool:raise ValueError('후속 연락 활성화 여부는 true 또는 false여야 합니다.')
        settings=self.settings()
        with self.transaction() as c:
            case=self._case(c,case_id)
            if not enabled:
                c.execute('UPDATE cases SET pilot_followups_enabled=0 WHERE id=?',(case_id,))
                c.execute("UPDATE jobs SET status='staff_check',reason='관리자가 이 상담의 제한 운영 후속 연락을 중지했습니다.',updated_at=? WHERE case_id=? AND status IN ('pending','staff_check')",(iso(self.now()),case_id))
                self._audit(c,actor,'pilot.followups_paused',case_id)
            else:
                if self._data_mode()!='live' or settings['operation_mode']!='limited' or not case['pilot_enrolled']:raise ValueError('먼저 실제 제한 운영 대상으로 등록하세요.')
                reason=self._contact_reason(case)
                if reason:raise ValueError(reason)
                if case['company_owes_info']:raise ValueError('회사가 운영정보를 확인해 답변해야 하는 상담입니다.')
                if c.execute("SELECT 1 FROM outbox WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND status IN ('sending','uncertain')",(case['customer_id'],)).fetchone():raise ValueError('이 고객의 불명확한 발송 기록을 먼저 확인하세요.')
                templates,approved,eligible=self._template_readiness(c,case)
                if not eligible:raise ValueError('현재 상태에 맞는 승인 문구와 필요한 치환정보를 한 건 이상 확인하세요. 미승인 문구는 자동 승인되지 않습니다.')
                sent=c.execute("SELECT o.* FROM outbox o JOIN drafts d ON o.draft_id=d.id WHERE o.case_id=? AND o.status='sent' AND o.data_mode='live' AND o.recipient=? AND d.revision=? AND d.generation=? ORDER BY o.sent_at DESC LIMIT 1",(case_id,case['email'],case['state_revision'],case['generation'])).fetchone()
                if not sent:raise ValueError('최신 문의에 대한 직원 승인 답장이 실제 정상 발송된 뒤 후속 연락을 활성화하세요.')
                cycle=c.execute('SELECT * FROM cycles WHERE case_id=? AND active=1 AND state=? AND revision=? AND generation=? AND base_sent_at=?',(case_id,case['state'],case['state_revision'],case['generation'],sent['sent_at'])).fetchone()
                if not cycle:
                    self._cancel(c,case['customer_id'],'제한 운영의 현재 답장 기준 후속 시작',case_id)
                    self._create_cycle(c,case,dt(sent['sent_at']))
                was_enabled=bool(case['pilot_followups_enabled'])
                c.execute("UPDATE cases SET pilot_followups_enabled=1,followup_mode='active' WHERE id=?",(case_id,))
                if not was_enabled:
                    c.execute("UPDATE jobs SET activation_review_required=1,status='staff_check',reason='개별 활성화 이전의 경과 예약: 직원 개별 검토 필요',updated_at=? WHERE case_id=? AND status IN ('pending','staff_check') AND due_at<=?",(iso(self.now()),case_id,iso(self.now())))
                self._audit(c,actor,'pilot.followups_enabled',case_id,{'approved_templates':len(approved),'total_templates':len(templates),'base_sent_at':sent['sent_at']})
        return self.get_case(case_id)

    def hold_overdue_after_restart(self,actor='startup'):
        with self.transaction() as c:
            rows=[dict(r) for r in c.execute("SELECT id,case_id,due_at FROM jobs WHERE data_mode='live' AND status IN ('pending','staff_check') AND due_at<=?",(iso(self.now()),))]
            if rows:
                c.execute("UPDATE jobs SET activation_review_required=1,status='staff_check',reason='서버 중단·재시작 중 경과 예약: 직원 개별 검토 필요',updated_at=? WHERE data_mode='live' AND status IN ('pending','staff_check') AND due_at<=?",(iso(self.now()),iso(self.now())))
            self._record_operation(c,'scheduler_startup',True,{'held_jobs':len(rows)})
            if rows:self._audit(c,actor,'scheduler.restart_overdue_held',details={'job_count':len(rows)})
        return {'held_count':len(rows),'jobs':rows}

    def _operational_status(self,c,case):
        settings=self.settings()
        contact=self._contact_reason(case)
        generation_reason=self._draft_scope_reason(case)
        if case['data_mode']!=self._data_mode():generation_reason='모의 상담은 실제 모드로 전환할 수 없습니다. 별도 운영 상담을 사용하세요.'
        if not case['state'] or case['selected_generation']!=case['generation']:generation_reason='최신 고객 문의를 확인하고 S0~S6 상태를 먼저 저장하세요.'
        unresolved=c.execute("SELECT 1 FROM outbox WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND status IN ('sending','uncertain')",(case['customer_id'],)).fetchone()
        draft=next((d for d in case.get('drafts',[]) if d['status']=='draft' and d['revision']==case['state_revision'] and d['generation']==case['generation']),None)
        templates,approved,eligible=self._template_readiness(c,case)
        manual=[contact] if contact else []
        if unresolved:manual.append('이 고객의 이전 발송 결과가 불명확합니다. Gmail 전송 기록을 확인하세요.')
        followup=list(manual)
        limited=case['data_mode']=='live' and settings['operation_mode']=='limited'
        if case['company_owes_info']:followup.append('회사가 운영정보를 확인해 답변해야 합니다.')
        if settings['auto_followups_paused']:followup.append('관리자가 전체 자동 후속 발송을 중지했습니다.')
        if case['followup_mode']!='active':followup.append('이 상담의 후속 연락이 보류 또는 중단 상태입니다.')
        if limited and not case['pilot_followups_enabled']:followup.append('이 상담의 후속 연락을 개별 활성화해야 합니다.')
        if case['state'] and not eligible:followup.append('현재 상태의 승인 문구 또는 필수 치환정보를 확인해야 합니다.')
        held=[j for j in case.get('jobs',[]) if j['status'] in ('pending','staff_check') and j['activation_review_required']]
        if held:followup.append('경과 예약은 직원의 개별 검토·승인이 필요합니다.')
        if case['state']=='S6':followup.append('S6은 최신 신청·결제 상태 확인과 건별 직원 승인이 필요합니다.')
        if case['workflow']=='closed':action='종료된 상담의 이력을 확인하세요.'
        elif case['opt_out']:action='연락 거부 상태를 유지하고 필요한 내부 확인만 진행하세요.'
        elif unresolved:action='불명확한 발송을 Gmail 전송 기록과 대조하세요.'
        elif case['company_owes_info']:action='회사 운영정보를 확인한 뒤 고객에게 답변하세요.'
        elif case['selected_generation']!=case['generation'] or not case['state']:action='최신 문의를 읽고 고객 상태를 저장하세요.'
        elif limited and not case['pilot_enrolled']:action='관리자가 이 상담을 제한 운영 대상으로 개별 등록하세요.'
        elif draft:action='저장된 초안을 검토하고 수신자·본문을 확인한 뒤 발송하세요.'
        elif limited and not case['pilot_followups_enabled'] and case['workflow']=='waiting_reply':action='연락 조건과 승인 문구를 확인한 뒤 이 상담의 후속 연락을 개별 활성화하세요.'
        elif held:action='중단 중 경과한 예약을 각각 검토해 발송 여부를 결정하세요.'
        elif case.get('next_due'):action='다음 후속 예정 시점까지 고객 답장을 확인하세요.'
        else:action='직원이 선택한 상태를 바탕으로 답변 초안을 생성하세요.'
        return {'blockers':list(dict.fromkeys(manual+followup)),'next_action':action,'generation_blocked':bool(generation_reason),'generation_block_reason':generation_reason,'manual_send_blocked':bool(manual) or draft is None,'followup_blocked':bool(followup),'pilot_enrolled':bool(case['pilot_enrolled']),'pilot_followups_enabled':bool(case['pilot_followups_enabled']),'approved_template_count':len(approved),'eligible_template_count':len(eligible),'total_template_count':len(templates),'held_job_count':len(held)}

    def list_templates(self):
        with self.connect() as c:
            result=[dict(r) for r in c.execute('SELECT t.* FROM template_versions t WHERE version=(SELECT max(version) FROM template_versions x WHERE x.id=t.id) ORDER BY id')]
            for r in result:r['history']=[dict(x) for x in c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC',(r['id'],))]
            return result

    def update_template(self,template_id,body,actor):
        if not body.strip():raise ValueError('문구 본문을 입력하세요.')
        unknown=set(re.findall(r'\[([^\]]+)\]',body))-{'프로젝트명','A 프로젝트','B 프로젝트','핵심 조건'}
        if unknown:raise ValueError('지원하지 않는 치환값: '+', '.join(unknown))
        with self.transaction() as c:
            old=c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC LIMIT 1',(template_id,)).fetchone()
            if not old:raise ValueError('문구를 찾을 수 없습니다.')
            r=dict(old)
            if r['body']==body:return r
            c.execute("UPDATE template_versions SET approval='승인 취소',approved_by=NULL,approved_at=NULL WHERE id=? AND approval='승인 완료'",(template_id,))
            r.update(version=r['version']+1,body=body,approval='승인 대기',actor=actor,created_at=iso(self.now()),approved_by=None,approved_at=None)
            keys=list(r)
            c.execute(f"INSERT INTO template_versions ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",[r[k] for k in keys])
            c.execute("UPDATE jobs SET status='staff_check',reason='문구 수정: 예약 버전 재승인 또는 새 관리 주기 필요',updated_at=? WHERE template_id=? AND status IN ('pending','staff_check')",(iso(self.now()),template_id))
            self._audit(c,actor,'template.edited',template_id,{'version':r['version']})
        return r

    def approve_template(self,template_id,approved,actor):
        with self.transaction() as c:
            r=c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC LIMIT 1',(template_id,)).fetchone()
            if not r:raise ValueError('문구를 찾을 수 없습니다.')
            approval='승인 완료' if approved else '승인 취소'
            c.execute('UPDATE template_versions SET approval=?,approved_by=?,approved_at=? WHERE id=? AND version=?',(approval,actor,iso(self.now()),template_id,r['version']))
            if approved and r['approval']!='승인 완료':
                c.execute("UPDATE jobs SET activation_review_required=1,status='staff_check',reason='문구 승인 전에 경과한 예약: 직원 개별 검토 필요',updated_at=? WHERE template_id=? AND template_version=? AND data_mode='live' AND status IN ('pending','staff_check') AND due_at<=?",(iso(self.now()),template_id,r['version'],iso(self.now())))
            if not approved:
                c.execute("UPDATE template_versions SET approval='승인 취소' WHERE id=?",(template_id,))
                c.execute("UPDATE jobs SET status='staff_check',reason='문구 승인 취소',updated_at=? WHERE template_id=? AND status IN ('pending','staff_check')",(iso(self.now()),template_id))
            self._audit(c,actor,'template.approved' if approved else 'template.revoked',template_id,{'version':r['version']})
            return dict(c.execute('SELECT * FROM template_versions WHERE id=? AND version=?',(template_id,r['version'])).fetchone())

    def _contact_reason(self,case):
        mode=self._data_mode()
        if case['data_mode']!=mode:return '모의 데이터와 실제 운영 데이터의 발송 경로가 다릅니다. 자동 전환할 수 없습니다.'
        if mode=='live':
            settings=self.settings()
            mailbox=self._mailbox_email()
            if not mailbox or case['mailbox_email']!=mailbox:return '이 상담의 Gmail 계정과 현재 연결된 계정이 다릅니다.'
            if settings['operation_mode']=='verification':
                if case['purpose']!='verification' or not settings['verification_recipient'] or case['email']!=settings['verification_recipient']:return '제한된 실제 확인 모드입니다. 지정한 확인용 상담·수신자만 발송할 수 있습니다.'
            elif case['purpose']=='verification':return '확인용 상담은 운영 발송 대상으로 전환되지 않습니다.'
            elif settings['operation_mode']=='limited':
                if not case['pilot_enrolled']:return '관리자가 이 상담을 최대 5건의 제한 운영 대상으로 개별 등록해야 합니다.'
                if case['email']!=case['pilot_recipient']:return '제한 운영 등록 이후 수신자가 변경되었습니다. 관리자의 재등록이 필요합니다.'
        if not case['email']: return '이메일 주소가 없습니다.'
        if case['opt_out']: return '고객이 연락을 거부했습니다.'
        if not case['email_allowed'] or not case['consent_note'].strip(): return '직원이 이메일 연락 허용 및 근거를 확인해야 합니다.'
        if case['workflow'] in ('closed','paused'): return '상담이 종료 또는 보류 상태입니다.'
        if case['payment'] in ('complete','stopped'): return '신청·결제가 완료 또는 중단되었습니다.'
        if not case['state'] or case['selected_generation']!=case['generation']: return '직원이 최신 고객 문의의 상태를 다시 선택해야 합니다.'
        if not getattr(self.gmail,'is_mock',self.gmail.__class__.__name__.startswith('Mock')) and not self.settings()['live_enabled']:
            return '실제 Gmail 발송은 비활성화되어 있습니다.'
        return None

    def _job_reason(self,c,job,case,manual=False):
        if job['status'] in TERMINAL:return '이미 완료·취소·처리 중이거나 발송 결과가 불명확한 예약입니다.'
        if dt(job['due_at'])>self.now(): return '예약 시점이 아직 도래하지 않았습니다.'
        if self._next_allowed(self.now())>self.now(): return '영업시간 외입니다.'
        if not manual and self.settings()['auto_followups_paused']:return '관리자가 전체 자동 후속 발송을 중지했습니다.'
        if case['data_mode']=='live' and self.settings()['operation_mode']=='limited' and not case['pilot_followups_enabled']:return '이 상담의 제한 운영 후속 연락을 관리자가 개별 활성화해야 합니다.'
        if case['company_owes_info']:return '회사가 운영정보를 확인해 답변해야 합니다. 직원 확인 업무입니다.'
        if job['data_mode']!=self._data_mode():return '모의 예약은 실제 발송 대상으로 전환되지 않습니다.'
        if job['activation_review_required'] and not manual:return '운영 활성화 이전의 경과 예약: 직원 개별 검토 필요'
        cycle=c.execute('SELECT * FROM cycles WHERE id=?',(job['cycle_id'],)).fetchone()
        if cycle and cycle['data_mode']!=self._data_mode():return '예약 주기의 모의/실제 데이터 출처가 다릅니다.'
        if not cycle or not cycle['active']:return '무응답 후속관리 주기가 취소되었습니다.'
        if cycle['state']!=case['state'] or cycle['revision']!=case['state_revision'] or cycle['generation']!=case['generation']:
            return '고객 답장 또는 상태 재선택으로 이전 일정이 무효입니다.'
        reason=self._contact_reason(case)
        if reason:return reason
        if c.execute("SELECT 1 FROM outbox WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND status IN ('sending','uncertain') AND (job_id IS NULL OR job_id!=?)",(case['customer_id'],job['id'])).fetchone():
            return '이 고객의 이전 발송 결과가 불명확합니다. Gmail 전송 기록 확인이 필요합니다.'
        if case['followup_mode']!='active':return '직원이 후속관리를 보류 또는 중단했습니다.'
        t=c.execute('SELECT * FROM template_versions WHERE id=? AND version=?',(job['template_id'],job['template_version'])).fetchone()
        if not t or t['approval']!='승인 완료':return '예약에 지정된 문구 버전이 승인 대기 또는 승인 취소 상태입니다.'
        if t['state']!=case['state']:return '문구와 고객 상태가 다릅니다.'
        if t['automation'] not in ('조건부 자동','직원 확인 후 발송'):return '알 수 없는 자동화 분류: 직원 확인이 필요합니다.'
        if t['state']=='S6' or t['automation']=='직원 확인 후 발송':
            if case['payment']!='incomplete':return 'S6 신청·결제 미완료 여부를 직원이 확인해야 합니다.'
            if not case['payment_checked_at'] or self.now()-dt(case['payment_checked_at'])>timedelta(hours=24):return 'S6 신청·결제 상태의 최신 확인(24시간 이내)이 필요합니다.'
            if not manual:return 'S6 문구는 매 발송마다 직원 검토·승인이 필요합니다.'
        values=json.loads(case['fields'])
        for key in re.findall(r'\[([^\]]+)\]',t['body']+' '+t['conditions']):
            if not str(values.get(key,'')).strip():return f'필수 확인 정보 누락: [{key}]'
        if t['id']=='S3-01' and (not values.get('A 프로젝트') or not values.get('B 프로젝트') or values['A 프로젝트']==values['B 프로젝트']):
            return '실제 비교 중인 서로 다른 A·B 프로젝트를 확인해야 합니다.'
        if c.execute("SELECT 1 FROM outbox WHERE job_id=? AND status IN ('sending','sent','uncertain')",(job['id'],)).fetchone():return '이미 발송 처리된 예약입니다.'
        # Do not dump multiple overdue stages after an outage. One per customer/day.
        if c.execute("SELECT 1 FROM outbox WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND job_id IS NOT NULL AND status='sent' AND sent_at>=?",(case['customer_id'],iso(self.now()-timedelta(hours=24)))).fetchone():
            return '이전 후속 연락 후 24시간이 지나지 않았습니다. 누적 일정은 유지됩니다.'
        return None

    def _preflight(self,case_id):
        with self.connect() as c:case=self._case(c,case_id)
        if case['data_mode']!=self._data_mode():raise ValueError('모의/실제 데이터 출처가 다른 상담의 Gmail을 조회할 수 없습니다.')
        if not case['email']:return
        messages=self.gmail.latest(case['email'])
        with self.transaction() as c:
            for m in sorted(messages,key=lambda x:x.get('received_at','')):self._ingest(c,m)

    def _reserve(self,c,case,actor,draft=None,job=None):
        source_id=draft['id'] if draft else job['id']
        column='draft_id' if draft else 'job_id'
        existing=c.execute(f'SELECT * FROM outbox WHERE {column}=?',(source_id,)).fetchone()
        if existing:return dict(existing)
        last=c.execute("SELECT * FROM messages WHERE case_id=? AND direction='inbound' AND message_id IS NOT NULL ORDER BY received_at DESC LIMIT 1",(case['id'],)).fetchone()
        if draft:body=draft['body']
        else:
            template=c.execute('SELECT * FROM template_versions WHERE id=? AND version=?',(job['template_id'],job['template_version'])).fetchone()
            body=template['body'];fields=json.loads(case['fields'])
            for key in set(re.findall(r'\[([^\]]+)\]',body)):body=body.replace('['+key+']',str(fields[key]))
        out_id=uid();subject=case['subject']
        if not subject.lower().startswith('re:'):subject='Re: '+subject
        refs=[r['message_id'] for r in c.execute('SELECT message_id FROM messages WHERE case_id=? AND message_id IS NOT NULL ORDER BY received_at',(case['id'],))]
        c.execute('''INSERT INTO outbox(id,case_id,draft_id,job_id,status,body,recipient,subject,thread_id,in_reply_to,references_header,created_at,actor)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',(out_id,case['id'],draft['id'] if draft else None,job['id'] if job else None,'sending',body,case['email'],subject,case['thread_id'],last['message_id'] if last else None,' '.join(refs[-20:]),iso(self.now()),actor))
        context={k:case[k] for k in ('email','subject','generation','state_revision')}
        c.execute('UPDATE outbox SET data_mode=?,approval_context=? WHERE id=?',(case['data_mode'],json.dumps(context,ensure_ascii=False),out_id))
        expected_message_id='<gapyear-'+hashlib.sha256(out_id.encode()).hexdigest()+'@automation.gapyear.local>'
        c.execute('UPDATE outbox SET provider_message_id=? WHERE id=?',(expected_message_id,out_id))
        if draft:c.execute("UPDATE drafts SET status='sending',approved_by=?,updated_at=? WHERE id=?",(actor,iso(self.now()),draft['id']))
        else:c.execute("UPDATE jobs SET status='sending',approved_by=?,updated_at=? WHERE id=?",(actor if actor!='scheduler' else None,iso(self.now()),job['id']))
        self._audit(c,actor,'send.reserved',out_id,{'draft_id':draft['id'] if draft else None,'job_id':job['id'] if job else None})
        return dict(c.execute('SELECT * FROM outbox WHERE id=?',(out_id,)).fetchone())

    def send_draft(self,draft_id,actor,expected_version=None,expected_context=None):
        with self.connect() as c:
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft:raise ValueError('초안을 찾을 수 없습니다.')
            if expected_version is not None and expected_version!=draft['edit_version']:raise DraftConflict('승인 화면 이후 초안이 변경되었습니다. 최신 본문을 다시 확인하세요.')
            prior=c.execute('SELECT * FROM outbox WHERE draft_id=?',(draft_id,)).fetchone()
            if prior:return dict(prior)
            case_id=draft['case_id']
        try:self._preflight(case_id)
        except Exception as exc:raise ValueError('Gmail 최신 답장 확인 실패: 발송하지 않았습니다.') from exc
        with self.transaction() as c:
            case=self._case(c,case_id);draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            prior=c.execute('SELECT * FROM outbox WHERE draft_id=?',(draft_id,)).fetchone()
            if prior:return dict(prior)
            if expected_version is not None and expected_version!=draft['edit_version']:raise DraftConflict('승인 화면 이후 초안이 변경되었습니다. 최신 본문을 다시 확인하세요.')
            if expected_context is not None and any(expected_context.get(k)!=case[k] for k in ('email','subject','generation','state_revision')):raise DraftConflict('수신자·제목·고객 문의 또는 상태가 변경되었습니다. 발송 전 다시 확인하세요.')
            if draft['data_mode']!=self._data_mode():raise ValueError('모의 생성 초안을 실제 이메일로 발송할 수 없습니다. 실제 OpenAI로 새 초안을 생성하세요.')
            if draft['status']!='draft' or draft['revision']!=case['state_revision'] or draft['generation']!=case['generation']:
                raise ValueError('고객 문의나 상태가 변경되었습니다. 상태를 재선택하고 새 초안을 작성하세요.')
            reason=self._contact_reason(case)
            if reason:raise ValueError(reason)
            if c.execute("SELECT 1 FROM outbox WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND status IN ('sending','uncertain')",(case['customer_id'],)).fetchone():
                raise ValueError('이 고객의 이전 발송 결과가 불명확합니다. Gmail 전송 기록 확인 후 진행하세요.')
            out=self._reserve(c,case,actor,draft=draft)
        return self._deliver(out['id'])

    def _deliver(self,outbox_id):
        # A crash after reserve leaves sending forever until explicit reconciliation;
        # no lease expiry can accidentally retransmit a provider-accepted message.
        with self.connect() as c:
            out=dict(c.execute('SELECT * FROM outbox WHERE id=?',(outbox_id,)).fetchone())
        try:
            self._preflight(out['case_id'])
        except Exception:
            with self.transaction() as c:self._fail_reserved(c,out,'Gmail 최신 답장 확인 실패: 예약 후 발송 중단',uncertain=False)
            return self._outbox(outbox_id)
        with self.transaction() as c:
            current=c.execute('SELECT * FROM outbox WHERE id=?',(outbox_id,)).fetchone()
            if not current or current['status']!='sending':return dict(current) if current else {}
            case=self._case(c,out['case_id'])
            reason=self._contact_reason(case)
            approval_context=json.loads(out.get('approval_context','{}'))
            if approval_context and any(approval_context.get(k)!=case[k] for k in ('email','subject','generation','state_revision')):reason='발송 승인 이후 수신자·제목·문의·상태 변경 감지'
            if out['data_mode']!=self._data_mode():reason='모의 발송 기록을 실제 발송으로 전환할 수 없습니다.'
            if out['draft_id']:
                draft=c.execute('SELECT * FROM drafts WHERE id=?',(out['draft_id'],)).fetchone()
                if draft['revision']!=case['state_revision'] or draft['generation']!=case['generation']:reason='고객 답장 또는 상태 변경 감지'
            else:
                job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(out['job_id'],)).fetchone())
                # Remove only this attempt's claim when rechecking all other gates.
                job['status']='pending'
                c.execute("UPDATE outbox SET status='checking' WHERE id=?",(outbox_id,))
                reason=reason or self._job_reason(c,job,case,manual=out['actor']!='scheduler')
                c.execute("UPDATE outbox SET status='sending' WHERE id=?",(outbox_id,))
            if reason:
                self._fail_reserved(c,out,reason,uncertain=False)
            else:
                try:
                    result=self.gmail.send(to=out['recipient'],subject=out['subject'],body=out['body'],thread_id=out['thread_id'],in_reply_to=out['in_reply_to'],references=out['references_header'],idempotency_key=outbox_id)
                    if not result or not result.get('id'):raise ValueError('Gmail did not confirm a message identifier')
                except Exception as exc:
                    self._fail_reserved(c,out,'발송 결과 불명확: '+type(exc).__name__,uncertain=True)
                else:
                    self._finish_sent(c,out,result,self.now())
        return self._outbox(outbox_id)

    def _fail_reserved(self,c,out,reason,uncertain):
        status='uncertain' if uncertain else 'cancelled'
        c.execute('UPDATE outbox SET status=?,error=? WHERE id=?',(status,reason,out['id']))
        if out['job_id']:c.execute('UPDATE jobs SET status=?,reason=?,updated_at=? WHERE id=?',(status,reason,iso(self.now()),out['job_id']))
        if out['draft_id']:c.execute('UPDATE drafts SET status=?,updated_at=? WHERE id=?',(status,iso(self.now()),out['draft_id']))
        c.execute("UPDATE cases SET workflow='staff_check',updated_at=? WHERE id=?",(iso(self.now()),out['case_id']))
        self._audit(c,'scheduler','send.'+status,out['id'],{'reason':reason})

    def _finish_sent(self,c,out,result,sent_time):
        now=iso(sent_time)
        c.execute("UPDATE outbox SET status='sent',sent_at=?,provider_id=?,provider_message_id=?,error='' WHERE id=?",(now,result['id'],result.get('message_id'),out['id']))
        case=self._case(c,out['case_id'])
        thread=result.get('thread_id') or case['thread_id']
        if thread and not case['thread_id']:c.execute('UPDATE cases SET thread_id=? WHERE id=?',(thread,case['id']))
        c.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),case['id'],result['id'],result.get('message_id'),thread,'outbound','staff_approved' if out['draft_id'] else 'followup',out['subject'],out['body'],now,iso(self.now())))
        if out['draft_id']:
            c.execute("UPDATE drafts SET status='sent',updated_at=? WHERE id=?",(now,out['draft_id']))
            # Reconciliation after a reply must not overwrite the new workflow
            # or cancel a newer cycle. Only the still-current approved draft acts.
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(out['draft_id'],)).fetchone()
            fresh=(draft['revision']==case['state_revision'] and draft['generation']==case['generation'] and case['selected_generation']==case['generation'])
            if fresh:
                c.execute('UPDATE cases SET workflow=?,updated_at=? WHERE id=?',('staff_check' if case['company_owes_info'] else 'waiting_reply',now,case['id']))
                self._cancel(c,case['customer_id'],'새 직원 승인 답변으로 관리 주기 교체',case['id'])
                limited=(case['data_mode']=='live' and self.settings()['operation_mode']=='limited')
                if limited:c.execute('UPDATE cases SET pilot_followups_enabled=0 WHERE id=?',(case['id'],))
                if not limited and case['followup_mode']=='active' and not case['company_owes_info'] and case['purpose']!='verification' and not self._contact_reason(case):
                    self._create_cycle(c,case,sent_time)
        else:
            c.execute("UPDATE jobs SET status='sent',sent_at=?,reason='',updated_at=? WHERE id=?",(now,now,out['job_id']))
            job=c.execute('SELECT * FROM jobs WHERE id=?',(out['job_id'],)).fetchone()
            if job['step']==5:
                c.execute("UPDATE cycles SET active=0,closed_reason='마지막 기본 활성 연락 완료',closed_at=? WHERE id=?",(now,job['cycle_id']))
        if case['data_mode']=='live' and case['purpose']=='verification':
            self._verification_event(c,'verification_staff_sent' if out['draft_id'] else 'verification_followup_sent',case,{'provider_id':result['id']})
        self._record_operation(c,'staff_send' if out['draft_id'] else 'automatic_send',True,{'outbox_id':out['id'],'case_id':case['id'],'provider_id':result['id'],'sent_at':now,'data_mode':out['data_mode'],'manual_approval':out['actor']!='scheduler'})
        self._audit(c,out['actor'],'send.sent',out['id'],{'provider_id':result['id']})

    def _create_cycle(self,c,case,sent_time):
        cycle_id=uid();now=iso(self.now())
        c.execute('INSERT INTO cycles(id,case_id,state,revision,generation,base_sent_at) VALUES (?,?,?,?,?,?)',(cycle_id,case['id'],case['state'],case['state_revision'],case['generation'],iso(sent_time)))
        c.execute('UPDATE cycles SET data_mode=?,purpose=? WHERE id=?',(case['data_mode'],case['purpose'],cycle_id))
        for step,days in enumerate(SCHEDULES[case['state']],1):
            tid=f"{case['state']}-{step:02d}"
            t=c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC LIMIT 1',(tid,)).fetchone()
            original=dt(sent_time)+timedelta(days=days)
            c.execute('INSERT INTO jobs(id,case_id,cycle_id,template_id,template_version,step,original_due_at,due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (uid(),case['id'],cycle_id,tid,t['version'],step,iso(original),iso(self._next_allowed(original)),now,now))
        c.execute('UPDATE jobs SET data_mode=?,purpose=? WHERE cycle_id=?',(case['data_mode'],case['purpose'],cycle_id))

    def _outbox(self,outbox_id):
        with self.connect() as c:
            return dict(c.execute('SELECT * FROM outbox WHERE id=?',(outbox_id,)).fetchone())

    def _process_job(self,job_id,actor='scheduler',manual=False):
        with self.connect() as c:
            job=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('예약을 찾을 수 없습니다.')
            prior=c.execute('SELECT * FROM outbox WHERE job_id=?',(job_id,)).fetchone()
            if prior:return dict(prior)
            case_id=job['case_id']
        try:self._preflight(case_id)
        except Exception:
            with self.transaction() as c:
                c.execute("UPDATE jobs SET status='staff_check',reason='Gmail 연결 오류: 새 답장 확인 불가',updated_at=? WHERE id=? AND status IN ('pending','staff_check')",(iso(self.now()),job_id))
            return {'id':job_id,'status':'staff_check','reason':'Gmail 연결 오류: 새 답장 확인 불가'}
        with self.transaction() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            prior=c.execute('SELECT * FROM outbox WHERE job_id=?',(job_id,)).fetchone()
            if prior:return dict(prior)
            case=self._case(c,case_id)
            reason=self._job_reason(c,job,case,manual)
            if reason:
                if job['status'] not in TERMINAL:
                    due=job['due_at']
                    if reason=='영업시간 외입니다.':due=iso(self._next_allowed(self.now()))
                    c.execute("UPDATE jobs SET status='staff_check',reason=?,due_at=?,updated_at=? WHERE id=?",(reason,due,iso(self.now()),job_id))
                return {'id':job_id,'status':job['status'] if job['status'] in TERMINAL else 'staff_check','reason':reason}
            if manual and job['activation_review_required']:c.execute('UPDATE jobs SET activation_review_required=0 WHERE id=?',(job_id,))
            out=self._reserve(c,case,actor,job=job)
        return self._deliver(out['id'])

    def run_due(self):
        with self.connect() as c:
            ids=[r['id'] for r in c.execute("SELECT id FROM jobs WHERE status IN ('pending','staff_check') AND due_at<=? ORDER BY due_at,step",(iso(self.now()),))]
        results=[]
        try:
            for job_id in ids:results.append(self._process_job(job_id))
        except Exception as exc:
            self.record_operation('scheduler',False,{'processed':len(results)},type(exc).__name__)
            raise
        self.record_operation('scheduler',True,{'processed':len(results),'sent':sum(x.get('status')=='sent' for x in results),'blocked':sum(x.get('status')=='staff_check' for x in results),'uncertain':sum(x.get('status')=='uncertain' for x in results)})
        return {'processed':len(results),'results':results}

    def approve_job(self,job_id,actor):
        return self._process_job(job_id,actor,manual=True)

    def reconcile_send(self,outbox_id,actor):
        with self.connect() as c:
            out=c.execute('SELECT * FROM outbox WHERE id=?',(outbox_id,)).fetchone()
            if not out:raise ValueError('발송 기록을 찾을 수 없습니다.')
            out=dict(out)
        if out['status']=='sent':return out
        if out['status'] not in ('sending','uncertain'):raise ValueError('전송 확인 대상이 아닙니다.')
        try:result=self.gmail.reconcile(outbox_id)
        except Exception as exc:raise ValueError('Gmail 전송 기록 조회에 실패했습니다. 재발송하지 않았습니다.') from exc
        if not result:
            with self.transaction() as c:
                c.execute("UPDATE outbox SET status='uncertain',error='Gmail 전송 기록 미확인: 자동 재발송 금지' WHERE id=?",(outbox_id,))
                if out['job_id']:c.execute("UPDATE jobs SET status='uncertain',reason='Gmail 전송 기록 미확인: 자동 재발송 금지' WHERE id=?",(out['job_id'],))
                self._audit(c,actor,'send.reconcile_unresolved',outbox_id)
            return self._outbox(outbox_id)
        # Refresh replies first. A resolved send never overrides a newer inbound.
        self._preflight(out['case_id'])
        sent_time=result.get('sent_at') or result.get('received_at')
        if not sent_time:
            # Unknown actual send time cannot safely establish elapsed-day schedule.
            with self.transaction() as c:
                c.execute("UPDATE outbox SET error='전송은 확인되었으나 실제 발송 시각 미확인: 직원 확인 필요' WHERE id=?",(outbox_id,))
                self._audit(c,actor,'send.reconcile_time_unknown',outbox_id)
            return self._outbox(outbox_id)
        with self.transaction() as c:
            if c.execute('SELECT status FROM outbox WHERE id=?',(outbox_id,)).fetchone()['status']=='sent':return self._outbox(outbox_id)
            self._finish_sent(c,out,result,dt(sent_time))
            self._audit(c,actor,'send.reconciled',outbox_id)
        return self._outbox(outbox_id)

    def list_jobs(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT j.*,cu.name,cu.email,ca.state,ca.payment FROM jobs j JOIN cases ca ON j.case_id=ca.id JOIN customers cu ON ca.customer_id=cu.id ORDER BY j.due_at,j.step')]

    def list_calls(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT cl.*,cu.name,cu.phone,ca.state,ca.owner FROM calls cl JOIN cases ca ON cl.case_id=ca.id JOIN customers cu ON ca.customer_id=cu.id ORDER BY cl.due_at')]

    def complete_call(self,call_id,actor):
        with self.transaction() as c:
            call=c.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
            if not call:raise ValueError('전화 업무를 찾을 수 없습니다.')
            if call['status']=='pending':
                c.execute("UPDATE calls SET status='completed',completed_at=?,completed_by=? WHERE id=?",(iso(self.now()),actor,call_id))
                self._audit(c,actor,'call.completed',call_id)
            return dict(c.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone())

    def dashboard(self):
        cases=self.list_cases()
        return {**{queue:sum(queue in case['queues'] for case in cases) for queue in self.QUEUES},'cases':cases}

    def export_rows(self):
        # A one-way reporting projection, not a source for execution decisions.
        rows=[['상담 ID','고객 ID','이름','이메일','전화번호','고객 상태','업무 상태','담당자','다음 연락일','이메일 허용','연락 거부','상담 내용','마지막 발송 결과']]
        for case in self.list_cases():
            details=self.get_case(case['id'])
            rows.append([case['id'],case['customer_id'],case['name'],case['email'] or '',case['phone'] or '',case['state'] or '',case['workflow'],case['owner'],case['next_due'] or '',str(case['email_allowed']),str(case['opt_out']),
                         '\n'.join(f"{m['received_at']} {m['direction']}: {m['body']}" for m in details['history']),details['outbox'][-1]['status'] if details['outbox'] else ''])
        return rows

    def audit_log(self):
        with self.connect() as c:
            result=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY created_at DESC LIMIT 500')]
            for r in result:r['details']=json.loads(r['details'])
            return result

    def backup(self,destination,actor='admin'):
        destination=Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as src, sqlite3.connect(destination) as dst:src.backup(dst)
        destination.chmod(0o600)
        with self.transaction() as c:self._audit(c,actor,'database.backup',details={'file':destination.name})
        return {'path':str(destination)}

    def purge_closed(self,days=None,actor='admin',confirm=False,dry_run=None):
        if dry_run is None: dry_run=not confirm
        days=self.settings()['retention_days'] if days is None else days
        if type(days) is not int or days<1:raise ValueError('보관 기간은 1일 이상입니다.')
        cutoff=iso(self.now()-timedelta(days=days))
        removed_emails=[]
        with self.transaction() as c:
            # Only customers whose EVERY case is closed/old and have no uncertain attempt.
            ids=[r['id'] for r in c.execute("""SELECT cu.id FROM customers cu WHERE EXISTS(SELECT 1 FROM cases ca WHERE ca.customer_id=cu.id)
             AND NOT EXISTS(SELECT 1 FROM cases ca WHERE ca.customer_id=cu.id AND (ca.workflow!='closed' OR ca.updated_at>?))
             AND NOT EXISTS(SELECT 1 FROM outbox o JOIN cases ca ON o.case_id=ca.id WHERE ca.customer_id=cu.id AND o.status IN ('sending','uncertain'))""",(cutoff,))]
            if not dry_run:
                for customer_id in ids:
                    email=c.execute('SELECT email FROM customers WHERE id=?',(customer_id,)).fetchone()['email']
                    if email:removed_emails.append(email)
                    case_ids=[r['id'] for r in c.execute('SELECT id FROM cases WHERE customer_id=?',(customer_id,))]
                    for case_id in case_ids:
                        c.execute("DELETE FROM operations WHERE json_extract(details,'$.case_id')=?",(case_id,))
                        # Retain only non-content provider identifiers to avoid replaying
                        # purged mail from the company's still-existing Gmail mailbox.
                        c.execute('INSERT OR IGNORE INTO deleted_messages SELECT provider_id FROM messages WHERE case_id=? AND provider_id IS NOT NULL',(case_id,))
                        for table in ('outbox','jobs','cycles','drafts','state_history','messages','calls'):
                            c.execute(f'DELETE FROM audit WHERE object_id IN (SELECT id FROM {table} WHERE case_id=?)',(case_id,))
                            c.execute(f'DELETE FROM {table} WHERE case_id=?',(case_id,))
                        c.execute('DELETE FROM audit WHERE object_id=?',(case_id,))
                        c.execute('DELETE FROM cases WHERE id=?',(case_id,))
                    c.execute('DELETE FROM audit WHERE object_id=?',(customer_id,))
                    c.execute('DELETE FROM customers WHERE id=?',(customer_id,))
                self._audit(c,actor,'retention.purged',details={'customer_count':len(ids),'cutoff':cutoff})
        if not dry_run:
            with self.connect() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        mock_cleanup=True
        if not dry_run and getattr(self.gmail,'is_mock',False) and hasattr(self.gmail,'purge_customer'):
            for email in removed_emails:
                try:self.gmail.purge_customer(email)
                except Exception:mock_cleanup=False
        return {'dry_run':dry_run,'customer_count':len(ids),'cutoff':cutoff,'mock_cleanup_complete':mock_cleanup}
