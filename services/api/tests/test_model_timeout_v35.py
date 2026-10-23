"""模型超时设置要真的接到适配器上。

`BLANK_MODEL_TIMEOUT_SECONDS` 过去只住在 `Settings` 里：`build_adapter` 从没传
`timeout_seconds`，所以每家适配器都按 dataclass 的 60 秒截断——把环境变量调大对
慢推理模型毫无作用，调成 0 也照跑。这一格守的是"设置项 = 生效值"，不是"字段存在"。
"""

import httpx
import pytest

from blank_api.config import get_settings
from blank_api.models.model_provider import ModelProvider
from blank_api.services.models.registry import build_adapter


def _provider() -> ModelProvider:
    return ModelProvider(
        id="p-timeout",
        name="slow-vendor",
        kind="openai_compatible",
        base_url="http://127.0.0.1:9/v1",
        api_key_encrypted="",
        default_model="m",
        temperature=0.2,
        max_tokens=512,
        context_window=8000,
        supports_tools=True,
        supports_vision=False,
    )


@pytest.fixture
def clear_settings():
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_setting_reaches_the_adapter(monkeypatch, clear_settings):
    monkeypatch.setenv("BLANK_MODEL_TIMEOUT_SECONDS", "17.5")
    assert build_adapter(_provider()).config.timeout_seconds == 17.5


def test_default_when_unset(monkeypatch, clear_settings):
    monkeypatch.delenv("BLANK_MODEL_TIMEOUT_SECONDS", raising=False)
    assert build_adapter(_provider()).config.timeout_seconds == 60.0


def test_non_positive_does_not_become_zero_timeout(monkeypatch, clear_settings):
    """0 秒 = 每次调用立刻判超时，等于把整条模型链路打死；宁可退回默认。"""
    for value in ("0", "-5"):
        monkeypatch.setenv("BLANK_MODEL_TIMEOUT_SECONDS", value)
        get_settings.cache_clear()
        timeout = build_adapter(_provider()).config.timeout_seconds
        assert timeout == 60.0, f"BLANK_MODEL_TIMEOUT_SECONDS={value} 读出了 {timeout}"


def test_a_typo_fails_loudly_instead_of_becoming_the_default(monkeypatch, clear_settings):
    """写错单位（`60s`）不该被静默读成 60 或静默退回默认——启动就要炸。"""
    monkeypatch.setenv("BLANK_MODEL_TIMEOUT_SECONDS", "60s")
    get_settings.cache_clear()
    with pytest.raises(Exception, match="model_timeout_seconds"):
        get_settings()



def test_the_timeout_is_actually_applied_to_the_client():
    """配置→适配器→真正建出来的 AsyncClient，三段都要接上。

    只断言 `config.timeout_seconds` 会被这种改法骗过：把它写进配置却仍用
    `factory()` 不带 timeout 建客户端。这里直接看客户端自己的超时表。
    """
    seen: dict[str, object] = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return httpx.AsyncClient(**kwargs)

    get_settings.cache_clear()
    adapter = build_adapter(_provider(), client_factory=spy)
    assert adapter.config.timeout_seconds == 60.0
    adapter._new_client()
    timeout = seen.get("timeout")
    assert isinstance(timeout, httpx.Timeout), f"建客户端时没带超时：{seen}"
    assert timeout.read == 60.0
