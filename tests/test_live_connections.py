"""Offline transport contracts for the real adapters; never calls external APIs."""
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.google_connect import GoogleConnect, GMAIL_SCOPES
from app.integrations import GmailAPI, GoogleOAuth, IntegrationError, OpenAIDrafter, ReauthenticationRequired
from tests.test_integrations import client_for, gmail_message, oauth_credentials


def oauth_env(monkeypatch, tmp_path):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-client-secret')
    monkeypatch.setenv('APP_BASE_URL', 'https://staff.example.test')
    monkeypatch.setenv('GOOGLE_TOKEN_FILE', str(tmp_path / 'secrets' / 'gmail.json'))
    return tmp_path / 'secrets' / 'gmail.json'


def grant_handler(account='staff@example.test', scope=' '.join(GMAIL_SCOPES)):
    def handler(request):
        if request.url.path == '/token':
            data = parse_qs(request.content.decode())
            assert data['code_verifier'] == ['test-verifier']
            assert data['redirect_uri'] == ['https://staff.example.test/api/connections/gmail/callback']
            return httpx.Response(200, json={'access_token': 'test-access', 'refresh_token': 'test-refresh',
                                             'expires_in': 3600, 'scope': scope})
        assert request.url.path.endswith('/profile')
        return httpx.Response(200, json={'emailAddress': account})
    return handler


def test_oauth_config_pending_and_production_https_required(tmp_path, monkeypatch):
    for key in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_TOKEN_FILE', 'APP_SECRET_DIR', 'APP_BASE_URL'):
        monkeypatch.delenv(key, raising=False)
    assert GoogleConnect().status()['configured'] is False
    oauth_env(monkeypatch, tmp_path)
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('APP_BASE_URL', 'http://127.0.0.1:8001')
    with pytest.raises(IntegrationError):
        GoogleConnect().authorization_url('state', 'challenge')


def test_google_authorization_requests_only_gmail_read_send_and_pkce(tmp_path, monkeypatch):
    oauth_env(monkeypatch, tmp_path)
    url = GoogleConnect().authorization_url('one-use-state', 'pkce-challenge')
    params = parse_qs(urlparse(url).query)
    assert set(params['scope'][0].split()) == set(GMAIL_SCOPES)
    assert params['state'] == ['one-use-state']
    assert params['code_challenge'] == ['pkce-challenge']
    assert params['code_challenge_method'] == ['S256']
    assert params['access_type'] == ['offline']
    assert 'select_account' in params['prompt'][0]


def test_google_exchange_verifies_actual_account_and_stores_private_file(tmp_path, monkeypatch):
    path = oauth_env(monkeypatch, tmp_path)
    link = GoogleConnect(client_for(grant_handler()))
    result = link.exchange('one-use-code', 'test-verifier')
    assert result['account_email'] == 'staff@example.test' and result['read_verified']
    assert result['send_verified'] is False
    saved = json.loads(path.read_text())
    assert saved['refresh_token'] == 'test-refresh'
    assert 'client_secret' not in saved
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'test-refresh' not in json.dumps(link.status())
    adapter = GmailAPI(db_path=tmp_path / 'app.db', client=client_for(lambda r: httpx.Response(200, json={'emailAddress': 'staff@example.test'})))
    assert adapter.verify()['account_email'] == result['account_email']


def test_oauth_missing_scope_and_account_change_never_replace_saved_token(tmp_path, monkeypatch):
    path = oauth_env(monkeypatch, tmp_path)
    link = GoogleConnect(client_for(grant_handler()))
    link.exchange('code', 'test-verifier')
    before = path.read_bytes()
    wrong = GoogleConnect(client_for(grant_handler('different@example.test')))
    with pytest.raises(IntegrationError, match='cannot be changed'):
        wrong.exchange('code', 'test-verifier', expected_account='staff@example.test')
    assert path.read_bytes() == before
    insufficient = GoogleConnect(client_for(grant_handler(scope=GMAIL_SCOPES[0])))
    with pytest.raises(IntegrationError, match='Both Gmail'):
        insufficient.exchange('code', 'test-verifier')
    assert path.read_bytes() == before


def test_oauth_revocation_is_reconnect_required_and_redacted():
    oauth = GoogleOAuth({'client_id':'id','client_secret':'secret','refresh_token':'refresh'},
                        client_for(lambda r: httpx.Response(400, json={'error':'invalid_grant', 'error_description':'private-token'})))
    with pytest.raises(ReauthenticationRequired) as exc:
        oauth.token()
    assert oauth.reconnect_required
    assert 'private-token' not in str(exc.value)


def tracked_database(path, thread_id, email='customer@example.test'):
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE customers (id TEXT,email TEXT); CREATE TABLE cases (id TEXT,customer_id TEXT,thread_id TEXT); CREATE TABLE messages (case_id TEXT,received_at TEXT);')
        db.execute('INSERT INTO customers VALUES (?,?)', ('cust1', email))
        db.execute('INSERT INTO cases VALUES (?,?,?)', ('case1','cust1',thread_id))
        db.execute('INSERT INTO messages VALUES (?,?)', ('case1','2025-01-06T08:00:00+00:00'))


def test_gmail_label_start_preview_does_not_read_bodies_and_persists(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/profile'):
            return httpx.Response(200,json={'emailAddress':'staff@example.test'})
        if request.url.path.endswith('/labels'):
            return httpx.Response(200,json={'labels':[{'id':'Label_7','name':'한국갭이어/문의','type':'user'}]})
        if request.url.path.endswith('/messages'):
            assert request.url.params['labelIds']=='Label_7'
            assert request.url.params['q'].startswith('after:1736121600')
            return httpx.Response(200,json={'messages':[{'id':'m1'}]})
        pytest.fail('Preview must never retrieve message bodies')
    gmail=GmailAPI(oauth_credentials(),'staff@example.test',client=client_for(handler),db_path=tmp_path/'app.db')
    with pytest.raises(IntegrationError,match='label and collection start'):
        gmail.poll()
    result=gmail.preview_collection('한국갭이어/문의','2025-01-06T00:00:00Z')
    assert result['count']==1
    gmail.configure_collection('한국갭이어/문의','2025-01-06T00:00:00Z')
    resumed=GmailAPI(oauth_credentials(),'staff@example.test',db_path=tmp_path/'app.db')
    assert resumed.collection_status()['label']=='한국갭이어/문의'
    different=GmailAPI(oauth_credentials(),'another@example.test',db_path=tmp_path/'app.db')
    assert different.collection_status()['configured'] is False


def test_tracked_thread_reply_and_external_sent_detected_without_label(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/profile'):
            return httpx.Response(200,json={'emailAddress':'staff@example.test'})
        if request.url.path.endswith('/labels'):
            return httpx.Response(200,json={'labels':[{'id':'Label_7','name':'한국갭이어/문의','type':'user'}]})
        if request.url.path.endswith('/messages'):
            return httpx.Response(200,json={})
        if '/threads/' in request.url.path:
            reply=gmail_message('reply',labels=['INBOX'])
            reply['internalDate']='1736154000000'
            sent=gmail_message('sent',sender='staff@example.test',labels=['SENT'])
            sent['internalDate']='1736154001000'
            sent['payload']['headers'][1]['value']='customer@example.test'
            old=gmail_message('old');old['internalDate']='1736000000000'
            unrelated=gmail_message('private',sender='private@example.test')
            unrelated['internalDate']='1736154001000'
            return httpx.Response(200,json={'messages':[old,reply,sent,unrelated]})
        pytest.fail('Unexpected broad mailbox request')
    path=tmp_path/'app.db'
    gmail=GmailAPI(oauth_credentials(),'staff@example.test',client=client_for(handler),db_path=path)
    tracked_database(path,gmail._scope('thread-1'))
    gmail.configure_collection('한국갭이어/문의','2025-01-06T00:00:00Z')
    messages=gmail.poll()
    assert len(messages)==2
    assert {m['direction'] for m in messages}=={'inbound','outbound'}
    assert all(m['email']=='customer@example.test' for m in messages)
    assert all(m['account_email']=='staff@example.test' for m in messages)
    assert messages[0]['id'].startswith(gmail._scope(''))
    calls.clear()
    assert len(gmail.latest('customer@example.test'))==2
    assert len(calls)==1 and '/threads/' in calls[0].url.path
    assert not gmail.latest('unrelated@example.test')


def test_gmail_wrong_mailbox_thread_is_rejected_before_send():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'emailAddress':'staff@example.test'})
    gmail=GmailAPI(oauth_credentials(),'staff@example.test',allow_send=True,client=client_for(handler))
    other=GmailAPI(oauth_credentials(),'other@example.test')
    with pytest.raises(IntegrationError,match='another or unverified mailbox'):
        gmail.send('customer@example.test','subject','body',thread_id=other._scope('thread'))
    assert all(r.method=='GET' for r in calls)


def ai_response():
    return httpx.Response(200,headers={'x-request-id':'req-test'},json={
        'id':'chat-test','model':'model-test','usage':{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130},
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':'문의에 답변드립니다.','checks':['비용 확인 필요']})}}]})


def test_openai_probe_is_real_request_without_customer_and_metadata_recorded(tmp_path):
    requests=[]
    def handler(request):
        requests.append(request)
        return ai_response()
    ai=OpenAIDrafter('test-key','model-test',client_for(handler),db_path=tmp_path/'app.db')
    result=ai.verify()
    assert len(requests)==1 and requests[0].method=='POST'
    assert result['verified'] and result['request_id']=='req-test'
    assert result['usage']['total_tokens']==130
    payload=json.loads(requests[0].content)
    assert 'connection_check' in payload['messages'][1]['content']
    assert payload['store'] is False and payload['max_completion_tokens']==2048
    assert 'test-key' not in json.dumps(ai.usage_status())
    resumed=OpenAIDrafter('test-key','model-test',client_for(handler),db_path=tmp_path/'app.db')
    assert resumed.usage_status()['requests_used']==1
    assert resumed.usage_status()['tokens_used_or_reserved']==130


def test_openai_daily_limit_blocks_network_and_alerts(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return ai_response()
    ai=OpenAIDrafter('test-key','model-test',client_for(handler),db_path=tmp_path/'app.db')
    ai.configure_usage_limits({'daily_requests':1,'daily_tokens':250000,'alert_percent':80})
    ai.generate({'state':'S4','latest_inquiry':'참가 가능한가요?'})
    assert ai.usage_status()['alert'] is True
    with pytest.raises(IntegrationError,match='daily usage limit'):
        ai.generate({'state':'S4','latest_inquiry':'새 요청'})
    assert len(calls)==1


def test_openai_failure_never_substitutes_mock_and_budget_remains_reserved(tmp_path):
    ai=OpenAIDrafter('test-key','model-test',client_for(lambda r: httpx.Response(429,json={'error':'private-prompt'})),db_path=tmp_path/'app.db')
    with pytest.raises(IntegrationError) as exc:
        ai.generate({'latest_inquiry':'문의'})
    assert 'private-prompt' not in str(exc.value)
    status=ai.usage_status()
    assert status['requests_used']==1 and status['tokens_used_or_reserved']>0
    assert status['last_request']['status']=='error'


def test_connected_grant_wins_over_obsolete_bootstrap_token(tmp_path, monkeypatch):
    path = oauth_env(monkeypatch, tmp_path)
    link = GoogleConnect(client_for(grant_handler()))
    link.exchange('code', 'test-verifier')
    monkeypatch.setenv('GOOGLE_REFRESH_TOKEN', 'obsolete-refresh')
    monkeypatch.setenv('GOOGLE_ACCESS_TOKEN', 'obsolete-access')
    oauth = GoogleOAuth()
    assert oauth.credentials['refresh_token'] == 'test-refresh'
    assert oauth.credentials['access_token'] == 'test-access'


def test_two_ai_instances_share_daily_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'app.db'
    first = OpenAIDrafter('test-key', 'model-test', client_for(lambda r: ai_response()), db_path=path)
    first.configure_usage_limits({'daily_requests': 1})
    second = OpenAIDrafter('test-key', 'model-test', client_for(lambda r: ai_response()), db_path=path)
    def generate(adapter):
        try:
            adapter.generate({'latest_inquiry': '참가 문의'})
            return True
        except IntegrationError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(generate, [first, second]))
    assert sorted(outcomes) == [False, True]
    assert first.usage_status()['requests_used'] == 1
