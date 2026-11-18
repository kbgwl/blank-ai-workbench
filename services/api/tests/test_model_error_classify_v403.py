"""HTTP 失败要翻成"用户能据此动手"的错误。

对齐 Claude / Codex：认证失败、模型名不存在、上下文超限这三类**不能重试**
（同样的请求再发只会得到同样的 401/400），而且必须说清是什么问题；
429 与 5xx 才是"现在别问"，交给上层退避。
"""

import pytest

from blank_api.services.models.base import classify_http_failure


def test_auth_failure_is_not_retryable() -> None:
    for status in (401, 403):
        error = classify_http_failure(status, '{"error":"invalid api key"}')
        assert error.code == "authentication_error"
        assert error.retryable is False
        assert "API Key" in error.message


@pytest.mark.parametrize(
    "body",
    [
        "This model's maximum context length is 128000 tokens",
        '{"error":{"code":"context_length_exceeded"}}',
        "prompt is too long: 210000 tokens",
        "input is too long for this model",
    ],
)
def test_context_overflow_is_recognised(body: str) -> None:
    error = classify_http_failure(400, body)
    assert error.code == "context_overflow"
    assert error.retryable is False


@pytest.mark.parametrize(
    "body",
    ['{"error":"model_not_found"}', "The model `deepseek-chat-x` does not exist", "invalid model name"],
)
def test_unknown_model_name_is_recognised(body: str) -> None:
    error = classify_http_failure(400, body)
    assert error.code == "model_not_found"
    assert "Model ID" in error.message


def test_missing_endpoint_points_at_the_url() -> None:
    error = classify_http_failure(404, "not found")
    assert error.code == "endpoint_not_found"
    assert "/v1" in error.message


def test_rate_limit_carries_retry_after() -> None:
    error = classify_http_failure(429, "slow down", retry_after=7.5)
    assert error.code == "rate_limit"
    assert error.retryable is True
    assert error.retry_after == 7.5
    assert error.status == 429


def test_server_error_is_retryable() -> None:
    error = classify_http_failure(503, "overloaded")
    assert error.code == "model_overloaded"
    assert error.retryable is True


def test_plain_400_stays_model_http_error_for_stream_fallback() -> None:
    """供应商拿 400 拒 `stream` 参数时，上层要能退回整包——这条口径不能破。"""
    error = classify_http_failure(400, "unsupported parameter: stream")
    assert error.code == "model_http_error"
    assert error.retryable is False
    assert error.status == 400


def test_classification_is_case_insensitive() -> None:
    error = classify_http_failure(400, "MAXIMUM CONTEXT LENGTH exceeded")
    assert error.code == "context_overflow"
