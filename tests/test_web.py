from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.app import bootstrap
from nexus.app.config import Config, ConfigError, ModelConfig, WebConfig, load_config
from nexus.app.session import read_records
from nexus.app.tui import tool_title
from nexus.core.security import SecurityGateway, SecurityPolicy
from nexus.core.types import ExecutionContext, Limits, RuntimeEvent, ToolCancelled, json_text
from nexus.tools import web
from tests.conftest import Recorder, ScriptedModel, call, reply


def context(limit: int = 32768) -> ExecutionContext:
    return ExecutionContext(Path.cwd(), "test", "unused", limit)


def search_payload() -> dict[str, Any]:
    return {
        "results": [
            {
                "title": "Docs",
                "url": "https://example.com/docs",
                "summary": "Read this",
                "snippet": "duplicate",
                "content": "",
                "date": None,
                "authority": -1,
                "rerank_score": 1.8,
            }
        ]
    }


async def test_requests_results_and_lifecycle() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert request.method == "POST" and request.url.host == "api.deepseek.com"
        if request.url.path.endswith("search"):
            assert json.loads(request.content) == {
                "query": "python site:example.com",
                "count": 5,
                "need_content": False,
            }
            return httpx.Response(200, json=search_payload())
        assert json.loads(request.content) == {"url": "https://example.com/docs"}
        return httpx.Response(200, json={"title": "Docs", "content": "# Hello\n你好"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    service = web.WebTools("private-test-key", client=client)
    tools = service.tools()
    search = await tools["web_search"].execute(
        {"query": "python site:example.com"}, context(), Recorder()
    )
    fetch = await tools["web_fetch"].execute(
        {"url": "https://example.com/docs"}, context(), Recorder()
    )
    assert search.ok and fetch.ok and len(requests) == 2
    assert search.data["results"][0] == {
        "title": "Docs",
        "url": "https://example.com/docs",
        "summary": "Read this",
        "date": None,
        "authority": -1,
        "rerank_score": 1.8,
    }
    assert fetch.data["content"] == "# Hello\n你好"
    assert "private-test-key" not in fetch.message().content
    await service.close()
    assert client.is_closed


@pytest.mark.parametrize(
    "name,args",
    [
        ("web_search", {"query": " "}),
        ("web_search", {"query": "x" * 129}),
        ("web_search", {"query": "x", "count": True}),
        ("web_search", {"query": "x", "count": 51}),
        ("web_search", {"query": "x", "need_content": True}),
        ("web_fetch", {"url": "file:///tmp/a"}),
        ("web_fetch", {"url": "https://user:pass@example.com"}),
        ("web_fetch", {"url": "http://127.0.0.1"}),
        ("web_fetch", {"url": "http://[::1]/"}),
        ("web_fetch", {"url": "http://localhost"}),
        ("web_fetch", {"url": "http://10.1.2.3"}),
        ("web_fetch", {"url": "https://example.com:bad"}),
        ("web_fetch", {"url": "https://example.com/\n"}),
    ],
)
async def test_invalid_arguments_never_send_request(name: str, args: dict[str, Any]) -> None:
    service = web.WebTools("unused")
    result = await service.tools()[name].execute(args, context(), Recorder())
    assert result.error_code == "invalid_arguments" and service.client is None


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "web_http_error"),
        (401, "web_unauthorized"),
        (402, "web_insufficient_balance"),
        (403, "web_access_denied"),
        (404, "web_fetch_unavailable"),
        (408, "web_timeout"),
        (429, "web_rate_limited"),
        (500, "web_http_error"),
        (504, "web_timeout"),
        (302, "web_http_error"),
    ],
)
async def test_http_errors_no_retry_redirect_or_raw_error_leak(status: int, code: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, text="private-test-key", headers={"Location": "https://other.test"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        result = (
            await web.WebTools("private-test-key", client=client)
            .tools()["web_fetch"]
            .execute({"url": "https://example.com"}, context(), Recorder())
        )
    assert calls == 1 and not result.ok and result.error_code == code
    assert result.data["http_status"] == status
    assert "private-test-key" not in result.message().content


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b"{}", b'{"results":[1]}', b'{"results":[{"title":1,"url":"x"}]}']
)
async def test_bad_responses_are_observations(body: bytes) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    ) as client:
        result = (
            await web.WebTools("unused", client=client)
            .tools()["web_search"]
            .execute({"query": "x"}, context(), Recorder())
        )
    assert result.error_code == "web_invalid_response"


def test_budget_empty_results_and_summary_fallback() -> None:
    data, truncated = web.result_data("search", {"query": "x"}, {"results": []}, 256)
    assert data["results"] == [] and not truncated
    payload = search_payload()
    payload["results"][0]["summary"] = ""
    data, truncated = web.result_data("search", {"query": "x"}, payload, 1024)
    assert data["results"][0]["summary"] == "duplicate" and not truncated
    payload["results"] *= 20
    for row in payload["results"]:
        row["summary"] = '你好\\"\n' * 1000
    data, truncated = web.result_data("search", {"query": "x"}, payload, 300)
    assert truncated and data["omitted_results"] > 0 and len(data["results"]) == 1
    assert len(json_text(data).encode()) <= 300
    assert data["results"][0]["url"] == "https://example.com/docs"
    data, truncated = web.result_data(
        "fetch", {"url": "https://example.com"}, {"title": "title", "content": '你好"' * 1000}, 256
    )
    assert truncated and len(json_text(data).encode()) <= 256 and "�" not in data["content"]
    with pytest.raises(web.WebError) as error:
        web.result_data("fetch", {"url": "https://example.com"}, {"title": "", "content": ""}, 256)
    assert error.value.code == "web_fetch_unavailable"
    with pytest.raises(web.WebError):
        web.result_data(
            "fetch", {"url": "https://example.com/" + "x" * 500}, {"title": "", "content": "x"}, 256
        )


async def test_response_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 32)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 100))
    ) as client:
        result = (
            await web.WebTools("unused", client=client)
            .tools()["web_fetch"]
            .execute({"url": "https://example.com"}, context(), Recorder())
        )
    assert result.error_code == "web_response_too_large"


async def test_total_timeout_cancel_and_connection_error() -> None:
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        service = web.WebTools("unused", 0.02, client=client)
        result = await service.tools()["web_fetch"].execute(
            {"url": "https://example.com"}, context(), Recorder()
        )
        assert result.error_code == "web_timeout"
        service.timeout = 60
        started.clear()
        task = asyncio.ensure_future(
            service.tools()["web_fetch"].execute(
                {"url": "https://example.com"}, context(), Recorder()
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(ToolCancelled) as cancelled:
            await task
        assert cancelled.value.result.error_code == "interrupted_unknown"

    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-error-text")

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
        result = (
            await web.WebTools("unused", client=client)
            .tools()["web_search"]
            .execute({"query": "x"}, context(), Recorder())
        )
        assert result.error_code == "web_network_error" and "secret-error-text" not in str(result)


@pytest.mark.parametrize(
    "section",
    [
        'enabled = "yes"',
        "timeout_seconds = 0",
        "timeout_seconds = 601",
        'api_key_env = "bad-key"',
        'api_key = "secret"',
    ],
)
def test_invalid_config(tmp_path: Path, section: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nname="test"\n[web]\n' + section, encoding="utf-8")
    with pytest.raises(ConfigError, match="web"):
        load_config(path, require_key=False)


async def ignore(event: RuntimeEvent) -> None:
    pass


async def test_registration_configuration_and_restricted_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nname="test"\n[memory]\nenabled=false\n', encoding="utf-8")
    cfg = load_config(path, require_key=False)
    assert not cfg.web.enabled
    path.write_text(
        path.read_text() + '[web]\nenabled=true\napi_key_env="TEST_WEB_KEY"\n', encoding="utf-8"
    )
    cfg = load_config(path, require_key=False)
    assert cfg.web == WebConfig(True, "TEST_WEB_KEY", 60)
    monkeypatch.delenv("TEST_WEB_KEY", raising=False)
    missing = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    assert "TEST_WEB_KEY" in missing.web_unavailable and "web_search" not in missing.registry
    await missing.close()
    monkeypatch.setenv("TEST_WEB_KEY", "private-test-key")
    active = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    assert {"web_search", "web_fetch"} <= active.registry.keys()
    assert "private-test-key" in active.secret_values()
    tool = active.registry["web_search"]
    for mode in ("read-only", "workspace-write"):
        policy = SecurityPolicy(mode)
        assert SecurityGateway(policy).route(tool, {"query": "x"})[0] == "deny"
        restricted = bootstrap.Conversation(
            tmp_path, replace(cfg, security=policy), ignore, home=tmp_path
        )
        assert "Local" in restricted.web_unavailable and "web_fetch" not in restricted.registry
        await restricted.close()
    assert SecurityGateway(SecurityPolicy(ask_tools=("web_search",))).route(tool, {})[0] == "ask"
    custom = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, registry={})
    assert not custom.registry and not custom.web_unavailable
    await custom.close()
    await active.close()


async def test_conversation_loop_logs_and_resume_without_repeating_web(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    clients: list[httpx.AsyncClient] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=search_payload()
            if request.url.path.endswith("search")
            else {"title": "Docs", "content": "# Documentation"},
        )

    def service(key: str, timeout: int) -> web.WebTools:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return web.WebTools(key, timeout, client=client)

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    model = Model(
        [
            reply(call("web_search", {"query": "docs"})),
            reply(call("web_fetch", {"url": "https://example.com/docs"}, "fetch")),
            reply(text="done"),
            reply(text="resumed"),
        ]
    )
    monkeypatch.setattr(bootstrap, "WebTools", service)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    monkeypatch.setenv("TEST_WEB_KEY", "private-test-key")
    cfg = Config(
        ModelConfig("test", 32768, api_key_env="TEST_WEB_KEY"),
        Limits(),
        memory_enabled=False,
        web=WebConfig(True, "TEST_WEB_KEY"),
    )
    conversation = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    result = await conversation.turn("search then fetch")
    assert result.outcome == "completed" and len(calls) == 2
    assert conversation.writer is not None
    path = conversation.writer.path
    await conversation.close()
    assert clients[0].is_closed
    with path.open("rb") as stream:
        records = read_records(stream)[0]
    assert (
        len(
            [
                r
                for r in records
                if r["kind"] == "tool_checkpoint" and r["data"]["phase"] == "COMPLETED"
            ]
        )
        == 2
    )
    assert "private-test-key" not in path.read_text(encoding="utf-8")
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    assert (await resumed.turn("continue")).outcome == "completed"
    assert len(calls) == 2
    await resumed.close()
    assert all(client.is_closed for client in clients)
    assert any(
        '"content":"# Documentation"' in m.content for m in model.requests[-1] if m.role == "tool"
    )


def test_web_tool_labels() -> None:
    assert (
        tool_title({"name": "web_search", "arguments_json": '{"query":"python"}'})
        == "Search web python"
    )
    assert (
        tool_title({"name": "web_fetch", "arguments_json": '{"url":"https://example.com"}'})
        == "Fetch web https://example.com"
    )
