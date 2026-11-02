# -*- coding: utf-8 -*-
"""HTTP 限流上限：默认值不许动，但必须可配。

为什么要有这一组判据：`RateLimitMiddleware` 的 120 rpm 过去写死在构造默认值里，
于是一个批量客户端（评测装置 32 例 × 约 6 个请求、迁移脚本、集成方 SDK）会在几十秒内
把窗口打满，接着看到一片 `429 rate_limit_exceeded`——读起来像"服务挂了"，实际是
装置与限流在打架。把上限变成可配置是为了让那类客户端能被显式放宽，
**默认值一个字都不改**，所以这里第一条判据就是"默认还是 120"。
"""

from __future__ import annotations

import pytest

from blank_api import config as config_module


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def test_default_limit_is_still_120(monkeypatch):
    # conftest 全批设了 BLANK_RATE_LIMIT_RPM=0（测试进程共用一个计数器，整批跑必被自己拦下）。
    # 这条判据问的是"没有 env 时默认多少"，所以先把 env 摘掉再断言，不然测的是装置不是默认值。
    monkeypatch.delenv("BLANK_RATE_LIMIT_RPM", raising=False)
    assert config_module.Settings().rate_limit_rpm == 120, "默认值改了就是放宽策略，要单独讨论"


def test_limit_can_be_raised_or_disabled(monkeypatch):
    monkeypatch.setenv("BLANK_RATE_LIMIT_RPM", "1200")
    config_module.get_settings.cache_clear()
    assert config_module.get_settings().rate_limit_rpm == 1200

    monkeypatch.setenv("BLANK_RATE_LIMIT_RPM", "0")
    config_module.get_settings.cache_clear()
    assert config_module.get_settings().rate_limit_rpm == 0


def test_app_wires_the_configured_limit_and_can_skip_it(monkeypatch):
    from blank_api.app import create_app

    def limiter_rpm(app) -> list[int]:
        return [
            int(item.kwargs.get("rpm") or 0)
            for item in app.user_middleware
            if item.cls.__name__ == "RateLimitMiddleware"
        ]

    monkeypatch.setenv("BLANK_RATE_LIMIT_RPM", "300")
    config_module.get_settings.cache_clear()
    assert limiter_rpm(create_app()) == [300]

    # 0 = 显式关掉：批处理实例上不该再被自己的流量打断
    monkeypatch.setenv("BLANK_RATE_LIMIT_RPM", "0")
    config_module.get_settings.cache_clear()
    assert limiter_rpm(create_app()) == []
