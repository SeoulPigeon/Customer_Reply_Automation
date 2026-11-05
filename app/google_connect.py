"""Administrator initiated Gmail OAuth. Session/state/PKCE storage lives in Auth.

Only authorization code exchange can establish a connected account. A configured
email address alone never proves authorization. No Google password is collected.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlparse

import httpx

from app.integrations import (
    IntegrationError, ReauthenticationRequired, _address, _safe_response,
    google_token_path, write_secret_json,
)

GMAIL_SCOPES = (
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
)


class GoogleConnect:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=30)
        self.client_id = os.getenv('GOOGLE_CLIENT_ID')
        self.client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
        self.base_url = os.getenv('APP_BASE_URL', '').rstrip('/')
        self.token_path = google_token_path()

    @property
    def redirect_uri(self):
        return self.base_url + '/api/connections/gmail/callback' if self.base_url else None

    def _valid_base(self):
        p = urlparse(self.base_url)
        if p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
            return False
        if p.scheme == 'https' and p.hostname:
            return True
        return (os.getenv('APP_ENV') != 'production' and p.scheme == 'http'
                and p.hostname in ('localhost', '127.0.0.1', '::1'))

    def _saved(self):
        if not self.token_path or not self.token_path.exists():
            return {}
        try:
            value = json.loads(self.token_path.read_text())
        except (OSError, ValueError) as exc:
            raise IntegrationError('Gmail credential storage could not be read') from exc
        if not isinstance(value, dict):
            raise IntegrationError('Gmail credential storage is invalid')
        return value

    def status(self):
        configured = bool(self.client_id and self.client_secret and self.token_path and self._valid_base())
        try:
            saved = self._saved()
            error = None
        except IntegrationError:
            saved = {}
            error = '인증정보 저장소를 확인해주세요.'
        # Stored authorization is distinct from successful current API verification.
        return {'configured': configured, 'connected': bool(saved.get('account_email') and saved.get('refresh_token')),
                'account_email': saved.get('account_email'), 'redirect_uri': self.redirect_uri,
                'read_verified': False, 'scopes': list(GMAIL_SCOPES), 'error': error}

    def _require_config(self):
        if not (self.client_id and self.client_secret and self.token_path and self._valid_base()):
            raise IntegrationError('Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, private token storage and APP_BASE_URL before connecting Gmail')

    def authorization_url(self, state: str, challenge: str):
        self._require_config()
        if not state or not challenge:
            raise IntegrationError('An administrator session and PKCE challenge are required')
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
            'response_type': 'code', 'scope': ' '.join(GMAIL_SCOPES),
            'access_type': 'offline', 'prompt': 'select_account consent',
            'include_granted_scopes': 'false', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        })

    def exchange(self, code: str, verifier: str, expected_account: str | None = None):
        self._require_config()
        if not code or not verifier:
            raise IntegrationError('Google authorization code and PKCE verifier are required')
        try:
            response = self.client.post('https://oauth2.googleapis.com/token', data={
                'code': code, 'client_id': self.client_id, 'client_secret': self.client_secret,
                'redirect_uri': self.redirect_uri, 'grant_type': 'authorization_code',
                'code_verifier': verifier,
            })
        except httpx.HTTPError as exc:
            raise IntegrationError('Google authorization exchange connection failed') from exc
        if response.status_code in (400, 401):
            raise ReauthenticationRequired('Google authorization expired or was declined; start connection again')
        result = _safe_response(response, 'Google authorization exchange')
        if not isinstance(result.get('access_token'), str) or not result['access_token']:
            raise IntegrationError('Google did not return an access token')
        scopes = set(str(result.get('scope', '')).split())
        if not set(GMAIL_SCOPES).issubset(scopes):
            raise IntegrationError('Both Gmail read and send permissions must be approved')
        try:
            profile_response = self.client.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                headers={'Authorization': 'Bearer ' + result['access_token']})
        except httpx.HTTPError as exc:
            raise IntegrationError('Gmail account verification connection failed') from exc
        profile = _safe_response(profile_response, 'Gmail account verification')
        account = _address(profile.get('emailAddress', ''))
        if expected_account and account != _address(expected_account):
            raise IntegrationError('Connected Gmail account cannot be changed for an existing customer database')
        saved = self._saved()
        refresh = result.get('refresh_token')
        if not refresh and saved.get('account_email') == account:
            refresh = saved.get('refresh_token')
        if not isinstance(refresh, str) or not refresh:
            raise IntegrationError('Google offline access was not granted; reconnect with consent')
        write_secret_json(self.token_path, {
            'access_token': result['access_token'], 'refresh_token': refresh,
            'expires_at': time.time() + float(result.get('expires_in', 3600)),
            'scope': ' '.join(sorted(scopes)), 'account_email': account,
        })
        return {'provider': 'gmail', 'account_email': account, 'connected': True,
                'read_verified': True, 'send_verified': False, 'reconnect_required': False,
                'redirect_uri': self.redirect_uri}
