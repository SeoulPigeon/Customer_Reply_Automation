"""Sanitized OpenAI failure categories; no real provider or credential is used."""
import json

import httpx
import pytest

from app.integrations import IntegrationError, OpenAIDrafter, OpenAIError


PRIVATE = 'PRIVATE_CANARY_KEY_AND_CUSTOMER_TEXT'


def adapter(handler, **kwargs):
    return OpenAIDrafter('test-key-only', 'model-test',
                        httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def assert_safe(error, expected, status=None):
    assert isinstance(error, IntegrationError)
    assert error.code == expected
    assert error.http_status == status
    public = error.public_details()
    assert set(public) == {'provider', 'code', 'message', 'retryable', 'http_status'}
    assert public['provider'] == 'openai'
    assert public['message'] == str(error)
    assert PRIVATE not in str(error) + repr(error) + json.dumps(public, ensure_ascii=False)
    assert any('\uac00' <= char <= '\ud7a3' for char in error.message)


@pytest.mark.parametrize('status,provider_code,expected,retryable', [
    (401, 'invalid_api_key', 'openai_authentication_failed', False),
    (404, 'model_not_found', 'openai_model_unavailable', False),
    (400, 'model_not_found', 'openai_model_unavailable', False),
    (403, 'model_access_denied', 'openai_model_unavailable', False),
    (403, 'insufficient_permissions', 'openai_access_denied', False),
    (429, 'insufficient_quota', 'openai_quota_exceeded', False),
    (429, 'billing_hard_limit_reached', 'openai_quota_exceeded', False),
    (402, None, 'openai_quota_exceeded', False),
    (429, 'rate_limit_exceeded', 'openai_rate_limited', True),
    (503, None, 'openai_service_unavailable', True),
    (504, None, 'openai_timeout', True),
    (400, 'context_length_exceeded', 'openai_context_too_large', False),
    (400, PRIVATE, 'openai_request_rejected', False),
])
def test_http_failures_classified_without_provider_text(status, provider_code, expected, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(status, json={'error': {'code': provider_code, 'type': 'untrusted-provider-type', 'message': PRIVATE}})
    ai = adapter(handler)
    with pytest.raises(OpenAIError) as raised:
        ai.generate({'latest_inquiry': PRIVATE})
    assert_safe(raised.value, expected, status)
    assert raised.value.retryable is retryable
    assert len(calls) == 1  # No retry, no silent mock substitution.
    assert ai.usage_status()['last_request']['status'] == 'error'
    assert ai.usage_status()['tokens_used_or_reserved'] > 0


def test_quota_type_is_recognized_when_provider_code_is_missing():
    ai = adapter(lambda request: httpx.Response(429, json={'error': {'type': 'insufficient_quota', 'message': PRIVATE}}))
    with pytest.raises(OpenAIError) as raised:
        ai.verify()
    assert_safe(raised.value, 'openai_quota_exceeded', 429)


@pytest.mark.parametrize('failure,code', [
    (httpx.ConnectError, 'openai_network_error'),
    (httpx.ReadTimeout, 'openai_timeout'),
])
def test_network_failures_use_fixed_messages(failure, code):
    calls = []
    def handler(request):
        calls.append(request)
        raise failure(PRIVATE, request=request)
    ai = adapter(handler)
    with pytest.raises(OpenAIError) as raised:
        ai.verify()
    assert_safe(raised.value, code)
    assert raised.value.__suppress_context__ is True
    assert len(calls) == 1
    assert ai.usage_status()['last_request']['status'] == 'error'


@pytest.mark.parametrize('response', [
    httpx.Response(200, text=PRIVATE),
    httpx.Response(200, json=[PRIVATE]),
    httpx.Response(200, json={'choices': [PRIVATE], 'usage': PRIVATE}),
    httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': PRIVATE}}]}),
])
def test_invalid_responses_remain_errors_without_mock_answer(response):
    ai = adapter(lambda request: response)
    with pytest.raises(OpenAIError) as raised:
        ai.generate({'latest_inquiry': '가상 문의'})
    assert_safe(raised.value, 'openai_invalid_response', 200)


@pytest.mark.parametrize('api_key,model', [(None, 'model-test'), ('test-key-only', None)])
def test_missing_server_configuration_does_not_call_provider(monkeypatch, api_key, model):
    for name in ('OPENAI_API_KEY', 'GAPYEAR_OPENAI_API_KEY', 'OPENAI_MODEL'):
        monkeypatch.delenv(name, raising=False)
    calls = []
    ai = OpenAIDrafter(api_key, model, httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))))
    with pytest.raises(OpenAIError) as raised:
        ai.verify()
    assert_safe(raised.value, 'openai_configuration_required')
    assert calls == []


def test_local_budget_is_distinct_from_provider_billing_quota():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'body': '연결 확인', 'checks': []})}}],
                                        'usage': {'total_tokens': 10}})
    ai = adapter(handler)
    ai.configure_usage_limits({'daily_requests': 1})
    assert ai.verify()['verified'] is True
    with pytest.raises(OpenAIError) as raised:
        ai.generate({'latest_inquiry': '가상 문의'})
    assert_safe(raised.value, 'openai_local_budget_exceeded')
    assert len(calls) == 1


def test_oversized_context_is_rejected_before_provider_call():
    calls = []
    ai = adapter(lambda request: calls.append(request))
    with pytest.raises(OpenAIError) as raised:
        ai.generate({'latest_inquiry': '가' * 50000})
    assert_safe(raised.value, 'openai_context_too_large')
    assert calls == []
