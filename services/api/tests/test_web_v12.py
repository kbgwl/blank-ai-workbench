"""P1-5：联网工具与出站防线。

联网是数据外泄与 SSRF 的入口，所以测试重点不在"能不能抓网页"，而在
"抓不到不该抓的东西"：内网、回环、元数据地址、重定向绕过、域名黑白名单。
"""

import httpx
import pytest

from blank_api.services.tools import web
from blank_api.services.tools.web import check_url, extract_text, fetch

from tests.test_agent_v04 import env  # noqa: F401  (复用阶段3 的夹具)

PUBLIC = "http://93.184.216.34/"  # 公网 IP 字面量，离线也能过 getaddrinfo


# --------------------------------------------------------------------------- #
# 1. 出站校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://service.internal/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "",
    ],
)
def test_check_url_blocks_internal_and_non_http_targets(url: str) -> None:
    assert check_url(url) != ""


def test_check_url_allows_public_address() -> None:
    assert check_url(PUBLIC) == ""


def test_domain_deny_list_wins(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_WEB_DENY_DOMAINS", "93.184.216.34,example.com")
    assert "禁止" in check_url(PUBLIC)


def test_domain_allow_list_is_exclusive(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_WEB_ALLOW_DOMAINS", "docs.example.com")
    assert "不在允许" in check_url(PUBLIC)
    monkeypatch.setenv("BLANK_WEB_ALLOW_DOMAINS", "93.184.216.34")
    assert check_url(PUBLIC) == ""


# --------------------------------------------------------------------------- #
# 2. 正文提取
# --------------------------------------------------------------------------- #
def test_extract_text_strips_markup_and_scripts() -> None:
    title, text = extract_text(
        "<html><head><title>示例 &amp; 标题</title><style>p{color:red}</style></head>"
        "<body><script>alert(1)</script><h1>正文</h1><p>第一段</p><p>第二段</p>"
        "<!-- 注释 --><div>第三段</div></body></html>"
    )
    assert title == "示例 & 标题"
    assert "正文" in text and "第一段" in text and "第三段" in text
    assert "alert" not in text and "color:red" not in text and "<!--" not in text


# --------------------------------------------------------------------------- #
# 3. fetch：逐跳校验 + 大小上限
# --------------------------------------------------------------------------- #
def _client_factory(handler):
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


async def test_fetch_refuses_a_redirect_into_the_intranet(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == PUBLIC
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    monkeypatch.setattr(web, "CLIENT_FACTORY", _client_factory(handler))
    with pytest.raises(ValueError) as excinfo:
        await fetch(PUBLIC)
    assert "内网" in str(excinfo.value)
    # 第二轮请求根本没发出去
    assert "127.0.0.1" not in str(handler.__doc__ or "")


async def test_fetch_follows_one_public_redirect_and_extracts_text(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PUBLIC:
            return httpx.Response(301, headers={"location": "http://8.8.8.8/page"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><head><title>Page</title></head><body><p>hello world</p></body></html>",
        )

    monkeypatch.setattr(web, "CLIENT_FACTORY", _client_factory(handler))
    result = await fetch(PUBLIC)
    assert result.status == 200
    assert result.title == "Page"
    assert "hello world" in result.text
    assert result.redirects == ["http://8.8.8.8/page"]


async def test_fetch_stops_after_too_many_redirects(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        hop = str(request.url)
        return httpx.Response(302, headers={"location": hop + "x"})

    monkeypatch.setattr(web, "CLIENT_FACTORY", _client_factory(handler))
    with pytest.raises(ValueError) as excinfo:
        await fetch(PUBLIC)
    assert "重定向" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 4. 工具层
# --------------------------------------------------------------------------- #
class _Ctx:
    root = None
    task_id = "web"
    extra_secrets = ()


async def test_web_fetch_tool_truncates_and_reports(monkeypatch) -> None:
    body = "<html><head><title>T</title></head><body>" + "字" * 5_000 + "</body></html>"
    monkeypatch.setattr(
        web,
        "CLIENT_FACTORY",
        _client_factory(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/html"}, content=body.encode("utf-8")
            )
        ),
    )
    tool = {item.name: item for item in web.web_tools()}["web_fetch"]
    outcome = await tool.handler(_Ctx(), {"url": PUBLIC, "max_chars": 600})
    assert outcome.ok
    assert outcome.data["truncated"] is True
    assert outcome.data["chars"] <= 600
    assert len(outcome.output) < 1_000


async def test_web_search_parses_duckduckgo_results(monkeypatch) -> None:
    page = """
    <div class="result">
      <a class="result__a" href="https://example.com/a">第一条 <b>结果</b></a>
      <a class="result__snippet">摘要一</a>
    </div>
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb">第二条</a>
      <a class="result__snippet">摘要二</a>
    </div>
    """
    monkeypatch.delenv("BLANK_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(
        web,
        "CLIENT_FACTORY",
        _client_factory(lambda request: httpx.Response(200, text=page)),
    )
    tool = {item.name: item for item in web.web_tools()}["web_search"]
    outcome = await tool.handler(_Ctx(), {"query": "测试", "max_results": 2})
    assert outcome.ok
    results = outcome.data["results"]
    assert [item["title"] for item in results] == ["第一条 结果", "第二条"]
    # duckduckgo 的跳转链接被还原成真实地址
    assert results[1]["url"] == "https://example.org/b"


def test_web_tools_are_not_free_in_plan_mode() -> None:
    from blank_api.services.permissions import MODE_PLAN, PermissionPolicy

    policy = PermissionPolicy.build(MODE_PLAN)
    tools = {item.name: item for item in web.web_tools()}
    assert policy.decide(tools["web_fetch"], {"url": PUBLIC}).allowed is True
