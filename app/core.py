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
                    'retention_days':365}


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
'''


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
            for k,v in DEFAULT_SETTINGS.items():
                c.execute('INSERT OR IGNORE INTO app_settings VALUES (?,?)',(k,json.dumps(v)))
            if not c.execute('SELECT 1 FROM template_versions LIMIT 1').fetchone():
                self._import_templates(c, template_path or Path(__file__).parent.parent/'data/followup_templates.xlsx')
        return {'templates':len(self.list_templates())}

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
        unknown=set(data)-set(DEFAULT_SETTINGS)
        if unknown: raise ValueError('알 수 없는 설정: '+', '.join(sorted(unknown)))
        values.update(data)
        if not isinstance(values['business_start'],int) or not isinstance(values['business_end'],int) or not 0<=values['business_start']<values['business_end']<=24:
            raise ValueError('영업시간 범위를 확인하세요.')
        if not values['weekdays'] or any(type(x) is not int or x not in range(7) for x in values['weekdays']):
            raise ValueError('발송 가능 요일을 선택하세요.')
        if any(type(values[k]) is not bool for k in ('live_enabled','legal_review_confirmed','connectivity_verified')):raise ValueError('설정 확인 값은 true 또는 false여야 합니다.')
        if values['timezone']!='Asia/Seoul': raise ValueError('시간대는 Asia/Seoul입니다.')
        if type(values['retention_days']) is not int or values['retention_days']<1: raise ValueError('보관 기간은 1일 이상입니다.')
        if values['live_enabled'] and not (values['legal_review_confirmed'] and values['connectivity_verified']):
            raise ValueError('실제 발송 전 연결 검증과 적법한 연락 조건의 관리자 확인이 필요합니다.')
        with self.transaction() as c:
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

    def list_cases(self):
        with self.connect() as c:
            rows=c.execute('''SELECT ca.*,cu.name,cu.email,cu.phone,cu.email_allowed,cu.opt_out,
            (SELECT min(due_at) FROM jobs j WHERE j.case_id=ca.id AND j.status IN ('pending','staff_check')) next_due
            FROM cases ca JOIN customers cu ON ca.customer_id=cu.id ORDER BY ca.updated_at DESC''').fetchall()
            return [self._decode_case(dict(r)) for r in rows]

    def _decode_case(self,r):
        r['fields']=json.loads(r.get('fields','{}'))
        for key in ('email_allowed','opt_out'):
            if key in r:r[key]=bool(r[key])
        return r

    def get_case(self,case_id):
        with self.connect() as c:
            result=self._decode_case(self._case(c,case_id))
            result['customer']=dict(c.execute('SELECT * FROM customers WHERE id=?',(result['customer_id'],)).fetchone())
            result['history']=[dict(r) for r in c.execute('SELECT * FROM messages WHERE case_id=? ORDER BY received_at,created_at',(case_id,))]
            result['drafts']=[self._decode_draft(dict(r)) for r in c.execute('SELECT * FROM drafts WHERE case_id=? ORDER BY created_at DESC',(case_id,))]
            result['jobs']=[dict(r) for r in c.execute('SELECT * FROM jobs WHERE case_id=? ORDER BY created_at,step',(case_id,))]
            result['state_history']=[dict(r) for r in c.execute('SELECT * FROM state_history WHERE case_id=? ORDER BY created_at',(case_id,))]
            result['calls']=[dict(r) for r in c.execute('SELECT * FROM calls WHERE case_id=? ORDER BY due_at',(case_id,))]
            result['outbox']=[dict(r) for r in c.execute('SELECT * FROM outbox WHERE case_id=? ORDER BY created_at',(case_id,))]
            result['next_due']=min((j['due_at'] for j in result['jobs'] if j['status'] in ('pending','staff_check')),default=None)
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
        else:case_id=case['id']
        c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),case_id,pid,m.get('message_id'),thread,direction,
                  'gmail_external' if direction=='outbound' else 'gmail',m.get('subject') or '',m.get('body') or '',received,now))
        reason='고객 새 답장 수신' if direction=='inbound' else '직원이 Gmail에서 직접 발송: 후속관리 재확인 필요'
        self._cancel(c,customer['id'],reason)
        c.execute("UPDATE drafts SET status='stale',updated_at=? WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) AND status='draft'",(now,customer['id']))
        # All this customer's old selections become stale, including separate threads.
        c.execute('UPDATE cases SET selected_generation=-1,updated_at=? WHERE customer_id=?',(now,customer['id']))
        if direction=='inbound':
            c.execute("UPDATE cases SET generation=generation+1,last_inbound_at=?,workflow='state_selection',updated_at=? WHERE id=?",(received,now,case_id))
            body=m.get('body') or ''
            if re.search(r'수신\s*거부|연락\s*(?:하지\s*마|중단)|메일\s*(?:보내지|그만)|unsubscribe|do not (?:email|contact)',body,re.I):
                c.execute('UPDATE customers SET opt_out=1,email_allowed=0,updated_at=? WHERE id=?',(now,customer['id']))
                self._cancel(c,customer['id'],'고객 연락 거부 요청')
                c.execute("UPDATE cases SET workflow='staff_check',followup_mode='stopped' WHERE customer_id=?",(customer['id'],))
        else:
            c.execute("UPDATE cases SET workflow='staff_check',followup_mode='paused' WHERE customer_id=?",(customer['id'],))
        self._audit(c,'gmail','mail.received',case_id,{'provider_id':pid,'direction':direction})
        return case_id

    def poll_mail(self):
        try:
            messages=self.gmail.poll()
        except Exception as exc:
            with self.transaction() as c:
                c.execute("UPDATE jobs SET status='staff_check',reason='Gmail 연결 오류: 새 답장 확인 불가',updated_at=? WHERE status IN ('pending','staff_check')",(iso(self.now()),))
                self._audit(c,'scheduler','gmail.poll_failed',details={'type':type(exc).__name__})
            return {'ok':False,'count':0,'error':'Gmail 연결 오류: 새 답장 확인 불가'}
        ids=[]
        with self.transaction() as c:
            for m in sorted(messages,key=lambda x:x.get('received_at','')):
                result=self._ingest(c,m)
                if result:ids.append(result)
        return {'ok':True,'count':len(ids),'case_ids':list(dict.fromkeys(ids))}

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

    def select_state(self,case_id,state,actor):
        if state not in STATES:raise ValueError('S0~S6 상태를 선택하세요.')
        with self.transaction() as c:
            case=self._case(c,case_id)
            self._cancel(c,case['customer_id'],'직원 상태 재선택',case_id)
            now=iso(self.now());revision=case['state_revision']+1
            c.execute("UPDATE cases SET state=?,state_revision=?,selected_generation=generation,workflow='consulting',updated_at=? WHERE id=?",(state,revision,now,case_id))
            c.execute("UPDATE drafts SET status='stale',updated_at=? WHERE case_id=? AND status='draft'",(now,case_id))
            c.execute('INSERT INTO state_history VALUES (?,?,?,?,?,?)',(uid(),case_id,state,actor,now,revision))
            self._audit(c,actor,'state.selected',case_id,{'state':state,'revision':revision})
        return self.get_case(case_id)

    def update_case(self,case_id,data,actor):
        allowed={'email_allowed','consent_note','opt_out','followup_mode','workflow','owner','fields','notes','payment','name','phone','email','followup_at'}
        if set(data)-allowed:raise ValueError('지원하지 않는 상담 수정 항목입니다.')
        with self.transaction() as c:
            case=self._case(c,case_id);now=iso(self.now())
            if any(type(data[k]) is not bool for k in ('email_allowed','opt_out') if k in data):raise ValueError('연락 허용 값은 true 또는 false여야 합니다.')
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
            for k in ('followup_mode','workflow','owner','notes','fields','payment'):
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
            self._audit(c,actor,'case.updated',case_id,{'fields':list(data)})
        return self.get_case(case_id)

    def _decode_draft(self,r):
        r['checks']=json.loads(r['checks'])
        return r

    def generate_draft(self,case_id,actor):
        with self.transaction() as c:
            case=self._case(c,case_id)
            if not case['state'] or case['selected_generation']!=case['generation']:raise ValueError('직원이 최신 문의의 S0~S6 상태를 먼저 선택해야 합니다.')
            previous=c.execute("SELECT * FROM drafts WHERE case_id=? AND revision=? AND generation=? AND status='draft' ORDER BY created_at DESC LIMIT 1",(case_id,case['state_revision'],case['generation'])).fetchone()
            if previous:return self._decode_draft(dict(previous))
            history=[dict(r) for r in c.execute('SELECT direction,body,subject,received_at,source FROM messages WHERE case_id IN (SELECT id FROM cases WHERE customer_id=?) ORDER BY received_at DESC LIMIT 30',(case['customer_id'],))][::-1]
            context={'state':case['state'],'customer_name':case['name'],'subject':case['subject'],'history':history,
                     'latest_inquiry':next((m['body'] for m in reversed(history) if m['direction']=='inbound'),''),
                     'fields':json.loads(case['fields']),'notes':case['notes'],'channel':case['channel']}
            result=self.drafter.generate(context)
            if not result.get('body','').strip():raise ValueError('답변 초안 생성 결과가 비어 있습니다.')
            draft_id=uid();now=iso(self.now())
            c.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(draft_id,case_id,case['state_revision'],case['generation'],result['body'],json.dumps(result.get('checks',[]),ensure_ascii=False),result.get('provider','unknown'),'draft',actor,None,now,now))
            c.execute("UPDATE cases SET workflow='draft_review',updated_at=? WHERE id=?",(now,case_id))
            self._audit(c,actor,'draft.generated',draft_id)
            return self._decode_draft(dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()))

    def update_draft(self,draft_id,body,actor):
        if not body.strip():raise ValueError('발송 본문을 입력하세요.')
        with self.transaction() as c:
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft or draft['status']!='draft':raise ValueError('수정할 수 있는 초안이 아닙니다.')
            c.execute('UPDATE drafts SET body=?,updated_at=? WHERE id=?',(body,iso(self.now()),draft_id))
            self._audit(c,actor,'draft.edited',draft_id)
            return self._decode_draft(dict(c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()))

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
            if not approved:
                c.execute("UPDATE template_versions SET approval='승인 취소' WHERE id=?",(template_id,))
                c.execute("UPDATE jobs SET status='staff_check',reason='문구 승인 취소',updated_at=? WHERE template_id=? AND status IN ('pending','staff_check')",(iso(self.now()),template_id))
            self._audit(c,actor,'template.approved' if approved else 'template.revoked',template_id,{'version':r['version']})
            return dict(c.execute('SELECT * FROM template_versions WHERE id=? AND version=?',(template_id,r['version'])).fetchone())

    def _contact_reason(self,case):
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
        cycle=c.execute('SELECT * FROM cycles WHERE id=?',(job['cycle_id'],)).fetchone()
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
        expected_message_id='<gapyear-'+hashlib.sha256(out_id.encode()).hexdigest()+'@automation.gapyear.local>'
        c.execute('UPDATE outbox SET provider_message_id=? WHERE id=?',(expected_message_id,out_id))
        if draft:c.execute("UPDATE drafts SET status='sending',approved_by=?,updated_at=? WHERE id=?",(actor,iso(self.now()),draft['id']))
        else:c.execute("UPDATE jobs SET status='sending',approved_by=?,updated_at=? WHERE id=?",(actor if actor!='scheduler' else None,iso(self.now()),job['id']))
        self._audit(c,actor,'send.reserved',out_id,{'draft_id':draft['id'] if draft else None,'job_id':job['id'] if job else None})
        return dict(c.execute('SELECT * FROM outbox WHERE id=?',(out_id,)).fetchone())

    def send_draft(self,draft_id,actor):
        with self.connect() as c:
            draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            if not draft:raise ValueError('초안을 찾을 수 없습니다.')
            prior=c.execute('SELECT * FROM outbox WHERE draft_id=?',(draft_id,)).fetchone()
            if prior:return dict(prior)
            case_id=draft['case_id']
        try:self._preflight(case_id)
        except Exception as exc:raise ValueError('Gmail 최신 답장 확인 실패: 발송하지 않았습니다.') from exc
        with self.transaction() as c:
            case=self._case(c,case_id);draft=c.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone()
            prior=c.execute('SELECT * FROM outbox WHERE draft_id=?',(draft_id,)).fetchone()
            if prior:return dict(prior)
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
                c.execute("UPDATE cases SET workflow='waiting_reply',updated_at=? WHERE id=?",(now,case['id']))
                self._cancel(c,case['customer_id'],'새 직원 승인 답변으로 관리 주기 교체')
                if case['followup_mode']=='active' and not self._contact_reason(case):
                    self._create_cycle(c,case,sent_time)
        else:
            c.execute("UPDATE jobs SET status='sent',sent_at=?,reason='',updated_at=? WHERE id=?",(now,now,out['job_id']))
            job=c.execute('SELECT * FROM jobs WHERE id=?',(out['job_id'],)).fetchone()
            if job['step']==5:
                c.execute("UPDATE cycles SET active=0,closed_reason='마지막 기본 활성 연락 완료',closed_at=? WHERE id=?",(now,job['cycle_id']))
        self._audit(c,out['actor'],'send.sent',out['id'],{'provider_id':result['id']})

    def _create_cycle(self,c,case,sent_time):
        cycle_id=uid();now=iso(self.now())
        c.execute('INSERT INTO cycles(id,case_id,state,revision,generation,base_sent_at) VALUES (?,?,?,?,?,?)',(cycle_id,case['id'],case['state'],case['state_revision'],case['generation'],iso(sent_time)))
        for step,days in enumerate(SCHEDULES[case['state']],1):
            tid=f"{case['state']}-{step:02d}"
            t=c.execute('SELECT * FROM template_versions WHERE id=? ORDER BY version DESC LIMIT 1',(tid,)).fetchone()
            original=dt(sent_time)+timedelta(days=days)
            c.execute('INSERT INTO jobs(id,case_id,cycle_id,template_id,template_version,step,original_due_at,due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (uid(),case['id'],cycle_id,tid,t['version'],step,iso(original),iso(self._next_allowed(original)),now,now))

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
            out=self._reserve(c,case,actor,job=job)
        return self._deliver(out['id'])

    def run_due(self):
        with self.connect() as c:
            ids=[r['id'] for r in c.execute("SELECT id FROM jobs WHERE status IN ('pending','staff_check') AND due_at<=? ORDER BY due_at,step",(iso(self.now()),))]
        results=[]
        for job_id in ids:results.append(self._process_job(job_id))
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
        cases=self.list_cases();jobs=self.list_jobs()
        today=self.now().astimezone(SEOUL).date()
        return {'new_inquiries':sum(x['generation']==1 and x['state'] is None for x in cases),
                'state_selection':sum(x['selected_generation']!=x['generation'] for x in cases),
                'draft_review':sum(x['workflow']=='draft_review' for x in cases),
                'new_replies':sum(x['generation']>1 and x['selected_generation']!=x['generation'] for x in cases),
                'today_followups':sum(dt(x['due_at']).astimezone(SEOUL).date()==today and x['status'] in ('pending','staff_check') for x in jobs),
                'sent_followups':sum(x['status']=='sent' for x in jobs),
                'staff_check':sum(x['status']=='staff_check' for x in jobs),
                'errors':sum(x['status'] in ('uncertain','sending') for x in jobs)+sum(x['workflow'] in ('closed','paused','staff_check') for x in cases),
                'cases':cases}

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
