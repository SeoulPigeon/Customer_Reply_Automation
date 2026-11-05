"""Authenticated HTTP interface. External sending is disabled until verified by an admin."""
import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Auth
from app.core import Service
from app.integrations import gmail_from_env, drafter_from_env, sheets_from_env

ROOT = Path(__file__).resolve().parents[1]


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


def create_app(service=None, auth=None, scheduler=None, demo=None):
    data_dir = Path(os.getenv('APP_DATA_DIR', str(ROOT / 'var')))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / 'app.sqlite3'
    if service is None:
        service = Service(str(path), gmail_from_env(str(path)), drafter_from_env())
        service.initialize(str(ROOT / 'data' / 'followup_templates.xlsx'))
    auth = auth or Auth(service.db_path)
    enabled = scheduler if scheduler is not None else os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'
    if demo:
        if service is not demo.service or not service.gmail.is_mock or not service.drafter.is_mock:
            raise ValueError('검수 모드에는 모의 어댑터만 사용할 수 있습니다.')
        enabled = False
    runtime = {'poll_error': None, 'sheets_error': None, 'connection_status': {}, 'verified_this_process': False}

    def configure_send():
        if not getattr(service.gmail, 'is_mock', False):
            service.gmail.allow_send = bool(service.settings().get('live_enabled') and runtime['verified_this_process'])

    def tick():
        configure_send()
        try:
            result = service.poll_mail()
            runtime['poll_error'] = None if result.get('ok') else result.get('error', 'Gmail 조회 실패')
            if runtime['poll_error']:
                return
        except Exception as exc:
            runtime['poll_error'] = type(exc).__name__
            return
        try:
            service.run_due()
        except Exception as exc:
            runtime['poll_error'] = type(exc).__name__

    async def loop():
        while True:
            await asyncio.to_thread(tick)
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(loop()) if enabled else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title='한국갭이어 상담 관리', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service, app.state.auth, app.state.runtime = service, auth, runtime

    @app.middleware('http')
    async def secure_requests(request, call_next):
        if int(request.headers.get('content-length', '0') or 0) > 1_000_000:
            return JSONResponse({'detail': '요청이 너무 큽니다.'}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logging.getLogger(__name__).error('Operation failed: %s', type(exc).__name__)
        return JSONResponse({'detail': '처리에 실패했습니다. 연결 상태와 감사 기록을 확인해주세요.'}, status_code=503)

    def current(request: Request):
        user = auth.session(request.cookies.get('gapyear_session'))
        if not user:
            raise HTTPException(401, '로그인이 필요합니다.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), user['csrf_token']):
                raise HTTPException(403, '요청 인증이 만료되었습니다. 새로고침해주세요.')
        return user

    def admin(user=Depends(current)):
        if user['role'] != 'admin':
            raise HTTPException(403, '관리자 권한이 필요합니다.')
        return user

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/api/login')
    def login(data: Login, request: Request):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '허용되지 않은 로그인 요청입니다.')
        try:
            token, user = auth.login(data.username, data.password)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        response = JSONResponse(user)
        response.set_cookie('gapyear_session', token, httponly=True, samesite='strict',
                            secure=not demo and os.getenv('COOKIE_SECURE', 'false').lower() == 'true', max_age=43200)
        return response

    @app.get('/api/me')
    def me(user=Depends(current)):
        return user

    @app.post('/api/logout')
    def logout(request: Request, user=Depends(current)):
        auth.logout(request.cookies['gapyear_session'])
        response = JSONResponse({'ok': True})
        response.delete_cookie('gapyear_session')
        return response

    @app.get('/api/dashboard')
    def dashboard(user=Depends(current)):
        return {**service.dashboard(), 'poll_error': runtime['poll_error'], 'mode': 'mock' if service.gmail.is_mock else 'live'}

    @app.get('/api/cases')
    def cases(user=Depends(current)):
        return service.list_cases()

    @app.get('/api/cases/{case_id}')
    def case(case_id: str, user=Depends(current)):
        return service.get_case(case_id)

    @app.post('/api/cases/{case_id}/state')
    def state(case_id: str, data: dict = Body(...), user=Depends(current)):
        return service.select_state(case_id, data.get('state'), user['username'])

    @app.patch('/api/cases/{case_id}')
    def update_case(case_id: str, data: dict = Body(...), user=Depends(current)):
        return service.update_case(case_id, data, user['username'])

    @app.post('/api/cases/{case_id}/draft')
    def generate(case_id: str, user=Depends(current)):
        return service.generate_draft(case_id, user['username'])

    @app.put('/api/drafts/{draft_id}')
    def edit_draft(draft_id: str, data: dict = Body(...), user=Depends(current)):
        return service.update_draft(draft_id, data.get('body', ''), user['username'])

    @app.post('/api/drafts/{draft_id}/send')
    def send(draft_id: str, user=Depends(current)):
        configure_send()
        return service.send_draft(draft_id, user['username'])

    @app.get('/api/templates')
    def templates(user=Depends(current)):
        return service.list_templates()

    @app.put('/api/templates/{template_id}')
    def edit_template(template_id: str, data: dict = Body(...), user=Depends(admin)):
        return service.update_template(template_id, data.get('body', ''), user['username'])

    @app.post('/api/templates/{template_id}/approval')
    def approval(template_id: str, data: dict = Body(...), user=Depends(admin)):
        if type(data.get('approved')) is not bool:
            raise ValueError('승인 여부는 true/false여야 합니다.')
        return service.approve_template(template_id, data['approved'], user['username'])

    @app.get('/api/jobs')
    def jobs(user=Depends(current)):
        return service.list_jobs()

    @app.post('/api/jobs/{job_id}/approve')
    def approve_job(job_id: str, user=Depends(current)):
        configure_send()
        result = service.approve_job(job_id, user['username'])
        if result.get('status') == 'staff_check':
            raise HTTPException(409, result.get('reason', '발송 조건 확인이 필요합니다.'))
        return result

    @app.post('/api/outbox/{outbox_id}/reconcile')
    def reconcile(outbox_id: str, user=Depends(current)):
        return service.reconcile_send(outbox_id, user['username'])

    @app.get('/api/calls')
    def calls(user=Depends(current)):
        return service.list_calls()

    @app.post('/api/calls')
    def create_call(data: dict = Body(...), user=Depends(current)):
        return service.create_phone(data, user['username'])

    @app.post('/api/calls/{call_id}/complete')
    def complete(call_id: str, user=Depends(current)):
        return service.complete_call(call_id, user['username'])

    @app.post('/api/mail/poll')
    def poll(user=Depends(current)):
        result = service.poll_mail()
        runtime['poll_error'] = None if result.get('ok') else result.get('error', 'Gmail 조회 실패')
        return result

    @app.post('/api/mock/inbound')
    def mock_inbound(data: dict = Body(...), user=Depends(current)):
        if not getattr(service.gmail, 'is_mock', False):
            raise HTTPException(409, '모의 모드에서만 사용할 수 있습니다.')
        if not data.get('email') or not data.get('body'):
            raise ValueError('이메일과 문의 내용이 필요합니다.')
        allowed = {key: data[key] for key in ('email', 'name', 'subject', 'body', 'thread_id') if key in data}
        message = service.gmail.receive(**allowed)
        return service.ingest_message(message)

    @app.get('/api/settings')
    def settings(user=Depends(current)):
        return {**service.settings(), 'mode': 'mock' if service.gmail.is_mock else 'live',
                'connection_status': runtime['connection_status'], 'verified_this_process': runtime['verified_this_process'],
                'demo': bool(demo)}

    @app.put('/api/settings')
    def update_settings(data: dict = Body(...), user=Depends(admin)):
        allowed = {'business_start', 'business_end', 'weekdays', 'retention_days', 'legal_review_confirmed', 'live_enabled'}
        if set(data) - allowed:
            raise ValueError('변경할 수 없는 설정이 포함되어 있습니다.')
        if data.get('live_enabled') and (service.gmail.is_mock or not runtime['verified_this_process']):
            raise ValueError('실제 Gmail 연결을 검증한 뒤 발송을 활성화해주세요.')
        result = service.update_settings(data, user['username'])
        configure_send()
        return result

    @app.post('/api/connections/verify')
    def verify(user=Depends(admin)):
        results = {}
        for key, factory in [('gmail', lambda: service.gmail), ('openai', lambda: service.drafter), ('sheets', lambda: None if demo else sheets_from_env())]:
            try:
                adapter = factory()
                results[key] = adapter.verify() if adapter else {'configured': False}
            except Exception as exc:
                results[key] = {'ok': False, 'error': type(exc).__name__}
        runtime['connection_status'] = results
        runtime['verified_this_process'] = bool(not service.gmail.is_mock and results['gmail'].get('read_verified', False))
        service.update_settings({'connectivity_verified': runtime['verified_this_process']}, user['username'])
        configure_send()
        return {**results, 'connectivity_verified': runtime['verified_this_process']}

    @app.post('/api/sheets/sync')
    def sync(user=Depends(admin)):
        if demo:
            raise HTTPException(409, '직접 검수 모드에서는 실제 Google Sheets 연결을 차단합니다.')
        adapter = sheets_from_env()
        if not adapter:
            raise ValueError('Google Sheets 인증과 문서 ID를 환경변수로 설정해주세요.')
        result = adapter.export(service.export_rows())
        runtime['sheets_error'] = None
        return result

    @app.get('/api/audit')
    def audit(user=Depends(admin)):
        return service.audit_log()

    @app.get('/api/demo')
    def demo_info(case_id: str | None = None, user=Depends(current)):
        return demo.info(case_id) if demo else {'enabled': False}

    def require_demo():
        if not demo:
            raise HTTPException(404, '직접 검수 모드에서만 사용할 수 있습니다.')

    @app.post('/api/demo/advance')
    def demo_advance(data: dict = Body(...), user=Depends(current)):
        require_demo()
        return demo.advance(data.get('day'), data.get('case_id'), user['username'])

    @app.post('/api/demo/reply')
    def demo_reply(data: dict = Body(...), user=Depends(current)):
        require_demo()
        return demo.reply(data.get('case_id'), data.get('body'), user['username'])

    @app.post('/api/demo/scenario')
    def demo_scenario(user=Depends(current)):
        require_demo()
        return demo.scenario(user['username'])

    app.mount('/static', StaticFiles(directory=str(ROOT / 'app' / 'static')), name='static')

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'app' / 'static' / 'index.html')

    return app


app = create_app()
