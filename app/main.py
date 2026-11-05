"""Authenticated HTTP interface. External sending is disabled until verified by an admin."""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Auth
from app.core import Service
from app.integrations import IntegrationError, gmail_from_env, drafter_from_env, sheets_from_env

ROOT = Path(__file__).resolve().parents[1]


class OAuthAccessLogFilter(logging.Filter):
    """Never place an authorization code or OAuth state in access logs."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split('?', 1)[0] == '/api/connections/gmail/callback':
                args[2] = '/api/connections/gmail/callback'
                record.args = tuple(args)
        return True


logging.getLogger('uvicorn.access').addFilter(OAuthAccessLogFilter())


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


def create_app(service=None, auth=None, scheduler=None, demo=None):
    production = os.getenv('APP_ENV', 'development') == 'production'
    base_url = os.getenv('APP_BASE_URL', '').rstrip('/')
    if production:
        parsed = urlparse(base_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('운영 환경에는 실제 HTTPS 주소를 APP_BASE_URL에 설정해야 합니다.')
        if not os.getenv('APP_DATA_DIR') or not Path(os.environ['APP_DATA_DIR']).is_absolute():
            raise ValueError('운영 환경에는 영속 디스크의 절대 경로 APP_DATA_DIR가 필요합니다.')
        if demo:
            raise ValueError('운영 환경에서는 검수 모드를 실행할 수 없습니다.')
    data_dir = Path(os.getenv('APP_DATA_DIR', str(ROOT / 'var')))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / 'app.sqlite3'
    if service is None:
        service = Service(str(path), gmail_from_env(str(path)), drafter_from_env(str(path)))
        service.initialize(str(ROOT / 'data' / 'followup_templates.xlsx'))
    auth = auth or Auth(service.db_path, production=production)
    if production and not auth.production:
        raise ValueError('운영용 직원 인증 설정이 필요합니다.')
    enabled = scheduler if scheduler is not None else os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'
    if demo:
        if service is not demo.service or not service.gmail.is_mock or not service.drafter.is_mock:
            raise ValueError('검수 모드에는 모의 어댑터만 사용할 수 있습니다.')
        enabled = False
    runtime = {'poll_error': None, 'sheets_error': None, 'connection_status': {},
               'verified_this_process': False, 'scheduler_error': None, 'oauth_error': None}

    def record(name, ok, details=None, error=None):
        service.record_operation(name, ok, details=details, error=error)

    def verify_gmail():
        try:
            result = service.gmail.verify()
            runtime['connection_status']['gmail'] = result
            runtime['verified_this_process'] = bool(not service.gmail.is_mock and result.get('read_verified'))
            record('gmail_connection', True, result)
            return result
        except Exception as exc:
            runtime['verified_this_process'] = False
            result = {'ok': False, 'error': type(exc).__name__}
            runtime['connection_status']['gmail'] = result
            record('gmail_connection', False, error=type(exc).__name__)
            return result

    def configure_send():
        if not getattr(service.gmail, 'is_mock', False):
            service.gmail.allow_send = bool(service.settings().get('live_enabled') and runtime['verified_this_process'])

    def tick():
        if not service.gmail.is_mock and not runtime['verified_this_process']:
            verify_gmail()
        configure_send()
        try:
            result = service.poll_mail()
            runtime['poll_error'] = None if result.get('ok') else result.get('error', 'Gmail 조회 실패')
            if runtime['poll_error']:
                runtime['verified_this_process'] = False
                configure_send()
                return
        except Exception as exc:
            runtime['poll_error'] = type(exc).__name__
            runtime['verified_this_process'] = False
            configure_send()
            return
        try:
            service.run_due()
            runtime['scheduler_error'] = None
        except Exception as exc:
            runtime['scheduler_error'] = type(exc).__name__
            record('scheduler', False, error=type(exc).__name__)

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

    app = FastAPI(title='한국갭이어 상담 관리', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.auth, app.state.runtime = service, auth, runtime
    app.state.tick = tick
    if production:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(base_url).hostname])

    @app.middleware('http')
    async def secure_requests(request, call_next):
        try:
            content_length = int(request.headers.get('content-length', '0') or 0)
        except ValueError:
            return JSONResponse({'detail': '요청 크기가 올바르지 않습니다.'}, status_code=400)
        if content_length > 1_000_000:
            return JSONResponse({'detail': '요청이 너무 큽니다.'}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
        if production:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
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
            origin = request.headers.get('origin')
            expected_origin = base_url or str(request.base_url).rstrip('/')
            if origin and origin != expected_origin:
                raise HTTPException(403, '허용되지 않은 요청 출처입니다.')
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
        if origin and origin != (base_url or str(request.base_url).rstrip('/')):
            raise HTTPException(403, '허용되지 않은 로그인 요청입니다.')
        try:
            token, user = auth.login(data.username, data.password)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        response = JSONResponse(user)
        # OAuth returns through a top-level cross-site GET. Lax keeps that session
        # while CSRF tokens and Origin checks protect every modifying endpoint.
        response.set_cookie('gapyear_session', token, httponly=True, samesite='lax',
                            secure=production or (not demo and os.getenv('COOKIE_SECURE', 'false').lower() == 'true'), max_age=43200)
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
    def cases(queue: str | None = None, user=Depends(current)):
        return service.list_cases(queue=queue)

    @app.get('/api/cases/{case_id}')
    def case(case_id: str, user=Depends(current)):
        return service.get_case(case_id)

    @app.post('/api/cases/{case_id}/state')
    def state(case_id: str, data: dict = Body(...), user=Depends(current)):
        revision = integer(data, 'expected_revision', minimum=0)
        generation = integer(data, 'expected_generation', minimum=0)
        impact = service.preview_state_change(case_id, data.get('state'))
        if impact['requires_confirmation'] and data.get('confirm_change') is not True:
            raise HTTPException(409, {'message': '영향받는 후속 예약을 확인한 뒤 상태 변경을 승인하세요.', 'impact': impact})
        return service.select_state(case_id, data.get('state'), user['username'],
                                    expected_revision=revision, expected_generation=generation,
                                    confirm_change=data.get('confirm_change') is True)

    @app.get('/api/cases/{case_id}/state-impact')
    def state_impact(case_id: str, state: str, user=Depends(current)):
        return service.preview_state_change(case_id, state)

    def integer(data, key, minimum=1):
        value = data.get(key)
        if type(value) is not int or value < minimum:
            raise ValueError('최신 화면의 저장 버전이 필요합니다. 내용을 보존한 뒤 새로고침하세요.')
        return value

    @app.patch('/api/cases/{case_id}')
    def update_case(case_id: str, data: dict = Body(...), user=Depends(current)):
        return service.update_case(case_id, data, user['username'])

    @app.post('/api/cases/{case_id}/draft')
    def generate(case_id: str, data: dict = Body(default={}), user=Depends(current)):
        regenerate = data.get('regenerate', False)
        if type(regenerate) is not bool:
            raise ValueError('재생성 여부는 true 또는 false여야 합니다.')
        version = integer(data, 'expected_version') if regenerate else None
        try:
            result = service.generate_draft(case_id, user['username'], regenerate=regenerate, expected_version=version)
            return result
        except IntegrationError as exc:
            record('openai_draft', False, error=type(exc).__name__)
            raise

    @app.put('/api/drafts/{draft_id}')
    def edit_draft(draft_id: str, data: dict = Body(...), user=Depends(current)):
        if not isinstance(data.get('body'), str) or len(data['body']) > 20000:
            raise ValueError('답변 본문은 20,000자 이내의 문자열이어야 합니다.')
        return service.update_draft(draft_id, data['body'], user['username'],
                                    expected_version=integer(data, 'expected_version'))

    @app.post('/api/drafts/{draft_id}/send')
    def send(draft_id: str, data: dict = Body(...), user=Depends(current)):
        version = integer(data, 'expected_version')
        approval = data.get('approval')
        if not isinstance(approval, dict) or set(approval) != {'email', 'subject', 'generation', 'state_revision'}:
            raise ValueError('수신자와 최신 문의를 확인한 발송 승인이 필요합니다.')
        configure_send()
        try:
            return service.send_draft(draft_id, user['username'], expected_version=version, expected_context=approval)
        except Exception as exc:
            record('staff_send', False, error=type(exc).__name__)
            raise

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
        if runtime['poll_error']:
            runtime['verified_this_process'] = False
            configure_send()
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
                'gmail_account': None if service.gmail.is_mock else getattr(service.gmail, 'own_email', None),
                'activation': service.activation_status(), 'demo': bool(demo)}

    @app.put('/api/settings')
    def update_settings(data: dict = Body(...), user=Depends(admin)):
        allowed = {'business_start', 'business_end', 'weekdays', 'retention_days', 'legal_review_confirmed', 'live_enabled',
                   'auto_followups_paused', 'verification_recipient', 'operation_mode'}
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
                record(key + '_connection', True, results[key])
            except Exception as exc:
                results[key] = {'ok': False, 'error': type(exc).__name__}
                record(key + '_connection', False, error=type(exc).__name__)
        runtime['connection_status'] = results
        runtime['verified_this_process'] = bool(not service.gmail.is_mock and results['gmail'].get('read_verified', False))
        service.update_settings({'connectivity_verified': runtime['verified_this_process'],
                                 **({} if runtime['verified_this_process'] else {'live_enabled': False})}, user['username'])
        configure_send()
        return {**results, 'connectivity_verified': runtime['verified_this_process']}

    @app.put('/api/connections/openai/limits')
    def openai_limits(data: dict = Body(...), user=Depends(admin)):
        if demo or service.drafter.is_mock:
            raise ValueError('실제 OpenAI 모드에서 사용량 제한을 설정할 수 있습니다.')
        if set(data) - {'daily_requests', 'daily_tokens', 'alert_percent'}:
            raise ValueError('지원하지 않는 사용량 설정입니다.')
        result = service.drafter.configure_usage_limits(data)
        record('openai_limits', True, result)
        return result

    def collection_status():
        return service.gmail.collection_status() if not service.gmail.is_mock else {'configured': False, 'label': None, 'start_at': None}

    @app.get('/api/operations')
    def operations(user=Depends(current)):
        telemetry = service.operations_status()
        config = service.settings()
        mail_mock = service.gmail.is_mock
        ai_mock = service.drafter.is_mock
        collection = collection_status()
        poll_status = telemetry.get('gmail_poll', {})
        ai_status = telemetry.get('openai_draft') or telemetry.get('openai_connection', {})
        def capability(mock, configured, observation, stopped=False):
            state = ('stopped' if stopped else 'normal' if mock else 'waiting' if not configured else
                     'error' if observation.get('ok') is False else 'normal' if observation.get('ok') else 'waiting')
            return {'state': state, 'mode': 'mock' if mock else 'live',
                    'last_success_at': observation.get('last_success_at'), 'error': observation.get('error')}
        mail = capability(mail_mock, collection.get('configured', False), poll_status)
        ai = capability(ai_mock, bool(getattr(service.drafter, 'api_key', None) and getattr(service.drafter, 'model', None)), ai_status)
        manual_observation = poll_status if poll_status.get('ok') is False else telemetry.get('staff_send', {})
        manual = capability(mail_mock, collection.get('configured', False), manual_observation,
                            stopped=not mail_mock and not config['live_enabled'])
        automatic_observation = poll_status if poll_status.get('ok') is False else telemetry.get('scheduler', {})
        automatic = capability(mail_mock, collection.get('configured', False), automatic_observation,
                               stopped=config.get('auto_followups_paused', False) or (not mail_mock and not config['live_enabled']))
        usage = service.drafter.usage_status() if hasattr(service.drafter, 'usage_status') else {'mode': 'mock'}
        return {'capabilities': {'gmail_receive': mail, 'openai_draft': ai, 'manual_send': manual, 'auto_followup': automatic},
                'telemetry': telemetry, 'sender': None if mail_mock else getattr(service.gmail, 'own_email', None),
                'collection': collection, 'openai': usage, 'activation': service.activation_status(),
                'operation_mode': config.get('operation_mode'), 'scheduler_enabled': enabled,
                'automatic_followups_paused': config.get('auto_followups_paused', False)}

    @app.post('/api/operations/auto-followups/pause')
    def pause_automatic(user=Depends(admin)):
        return service.update_settings({'auto_followups_paused': True}, user['username'])

    @app.post('/api/cases/{case_id}/verification')
    def verification_case(case_id: str, user=Depends(admin)):
        if demo or service.gmail.is_mock:
            raise ValueError('실제 Gmail 확인용 상담에서만 사용할 수 있습니다.')
        return service.mark_verification_case(case_id, user['username'])

    @app.post('/api/cases/{case_id}/verification-followup')
    def verification_followup(case_id: str, data: dict = Body(...), user=Depends(admin)):
        if demo or service.gmail.is_mock:
            raise ValueError('실제 Gmail 확인용 상담에서만 사용할 수 있습니다.')
        return service.schedule_verification_followup(case_id, data.get('template_id'), data.get('due_at'), user['username'])

    @app.get('/api/activation')
    def activation(user=Depends(admin)):
        return service.activation_status()

    def google_connection():
        from app.google_connect import GoogleConnect
        if demo or service.gmail.is_mock:
            raise ValueError('실제 Gmail 모드로 실행한 서버에서 관리자 인증을 진행하세요.')
        return GoogleConnect()

    @app.get('/api/connections/gmail')
    def gmail_connection_status(user=Depends(admin)):
        if demo or service.gmail.is_mock:
            return {'configured': False, 'connected': False, 'mode': 'mock', 'collection': collection_status()}
        status = google_connection().status()
        scope = collection_status()
        telemetry = service.operations_status()
        connection = telemetry.get('gmail_connection', {})
        last_sync = telemetry.get('gmail_poll', {})
        reconnect = bool(scope.get('reconnect_required'))
        # A new successful profile check can recover the connection while the
        # separate collection sync still shows its previous failure.
        verified = bool(runtime['verified_this_process'] and not reconnect)
        return {**status, 'authorization_saved': status.get('connected', False), 'connected': verified,
                'read_verified': verified, 'collection': scope, 'reconnect_required': reconnect,
                'error': runtime['oauth_error'] or runtime['poll_error'] or connection.get('error') or status.get('error'),
                'last_read_verified_at': connection.get('last_success_at'), 'last_sync': last_sync}

    @app.post('/api/connections/gmail/start')
    def gmail_connect(request: Request, user=Depends(admin)):
        connection = google_connection()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        # Reconnection pauses delivery before leaving this site; existing records stay intact.
        service.update_settings({'live_enabled': False, 'auto_followups_paused': True}, user['username'])
        configure_send()
        nonce = auth.issue_nonce(request.cookies.get('gapyear_session'), 'gmail-oauth',
                                 {'verifier': verifier, 'expected_account': getattr(service.gmail, 'own_email', None)})
        return {'authorization_url': connection.authorization_url(nonce, challenge)}

    @app.get('/api/connections/gmail/callback')
    def gmail_callback(request: Request, state: str = '', code: str = '', error: str = '', user=Depends(admin)):
        payload = auth.consume_nonce(request.cookies.get('gapyear_session'), 'gmail-oauth', state)
        if error or not code:
            runtime['oauth_error'] = 'Google 인증을 완료하지 않았습니다. 다시 연결하세요.'
            return RedirectResponse('/#settings', status_code=303)
        try:
            result = google_connection().exchange(code, payload['verifier'], expected_account=payload.get('expected_account'))
            service.gmail = gmail_from_env(service.db_path)
            runtime['verified_this_process'] = False
            runtime['oauth_error'] = None
            verification = verify_gmail()
            service.update_settings({'connectivity_verified': runtime['verified_this_process']}, user['username'])
            if not verification.get('read_verified'):
                runtime['oauth_error'] = 'Google 인증 후 Gmail 조회를 확인하지 못했습니다. 연결 확인을 다시 실행하세요.'
            record('gmail_oauth', bool(verification.get('read_verified')), result)
        except Exception as exc:
            runtime['oauth_error'] = 'Gmail 연결에 실패했습니다. OAuth 설정 또는 계정 일치를 확인하고 다시 연결하세요.'
            record('gmail_oauth', False, error=type(exc).__name__)
        configure_send()
        return RedirectResponse('/#settings', status_code=303)

    @app.post('/api/connections/gmail/collection/preview')
    def gmail_collection_preview(request: Request, data: dict = Body(...), user=Depends(admin)):
        google_connection()
        preview = service.gmail.preview_collection(data.get('label'), data.get('start_at'))
        token = auth.issue_nonce(request.cookies.get('gapyear_session'), 'gmail-collection',
                                 {'label': preview['label'], 'start_at': preview['start_at'],
                                  'account_email': getattr(service.gmail, 'own_email', None)})
        return {**preview, 'preview_token': token}

    @app.put('/api/connections/gmail/collection')
    def gmail_collection(request: Request, data: dict = Body(...), user=Depends(admin)):
        google_connection()
        preview = auth.consume_nonce(request.cookies.get('gapyear_session'), 'gmail-collection', data.get('preview_token'))
        if (preview['label'] != data.get('label') or preview['start_at'] != data.get('start_at') or
                preview['account_email'] != getattr(service.gmail, 'own_email', None)):
            raise ValueError('미리 확인한 수집 범위와 다릅니다. 수집 건수를 다시 확인하세요.')
        result = service.gmail.configure_collection(preview['label'], preview['start_at'])
        record('gmail_collection', True, preview)
        return result

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
