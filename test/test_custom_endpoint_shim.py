"""Custom-endpoint plumbing: key precedence, the safe-mode gate and the shim.

Every test here is over a pure function or a tmp_path tree: no network, no
subprocess, no data-home write.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import provider_guard, provider_secrets, shim
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ANTHROPIC_BASE_URL,
)


class _Agent:
    """Minimal stand-in for AgentConfig: the guard reads attributes only."""

    def __init__(self, **kw: object) -> None:
        self.acp_backend = kw.get("acp_backend", ACP_BACKEND_CLAUDE)
        self.provider_base_url = kw.get("provider_base_url", "")
        self.provider_api_key = kw.get("provider_api_key", "")
        self.safe_mode = kw.get("safe_mode", False)
        self.use_shim = kw.get("use_shim", False)
        self.shim_port = kw.get("shim_port", 8787)
        self.shim_openai_base_url = kw.get("shim_openai_base_url", "http://127.0.0.1:11434/v1")
        self.model = kw.get("model", "auto")


# ── Key precedence ──


def test_env_beats_keyring_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(provider_secrets.ENV_VAR, "from-env")
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "from-keyring")
    assert provider_secrets.effective_provider_api_key("from-config") == "from-env"


def test_keyring_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider_secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "from-keyring")
    assert provider_secrets.effective_provider_api_key("from-config") == "from-keyring"


def test_config_is_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider_secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "")
    assert provider_secrets.effective_provider_api_key("from-config") == "from-config"
    assert provider_secrets.describe_key_source("from-config") == "config.json plaintext"


def test_describe_never_returns_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The source description reaches operator-facing surfaces; the key must not."""
    monkeypatch.setenv(provider_secrets.ENV_VAR, "sk-secret-value")
    assert "sk-secret-value" not in provider_secrets.describe_key_source("also-secret")


# ── Safe mode ──


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://192.168.1.5:8080",
        "http://10.0.0.9",
        "http://[::1]:9000",
        "http://100.64.1.2",  # Tailscale CGNAT
        "http://localhost:1234",
        "http://router.internal",
    ],
)
def test_local_endpoints_are_allowed(url: str) -> None:
    assert provider_guard.endpoint_is_local(url) is True
    provider_guard.assert_endpoint_allowed(url, safe_mode=True)


def test_public_literal_is_refused_under_safe_mode() -> None:
    with pytest.raises(ValueError, match="PUBLIC address"):
        provider_guard.assert_endpoint_allowed("http://8.8.8.8/v1", safe_mode=True)


def test_unresolvable_host_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider_guard.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no such host")),
    )
    assert provider_guard.endpoint_is_local("http://nope.example") is None
    with pytest.raises(ValueError, match="fail-closed"):
        provider_guard.assert_endpoint_allowed("http://nope.example", safe_mode=True)


def test_safe_mode_off_permits_anything() -> None:
    provider_guard.assert_endpoint_allowed("http://8.8.8.8/v1", safe_mode=False)


def test_one_public_record_makes_the_host_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """A split-horizon name with one public answer must not read as local."""
    monkeypatch.setattr(
        provider_guard.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (0, 0, 0, "", ("192.168.1.9", 0)),
            (0, 0, 0, "", ("93.184.216.34", 0)),
        ],
    )
    assert provider_guard.endpoint_is_local("http://split.example") is False


# ── Endpoint environment, and its membership gate ──


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
def test_non_members_get_no_endpoint_env(backend: str) -> None:
    """A harness outside the set is handed neither the base URL nor the key."""
    agent = _Agent(provider_base_url="http://127.0.0.1:9/v1", provider_api_key="sk-x")
    assert provider_guard.custom_endpoint_env(agent, backend) == {}


def test_claude_is_the_member() -> None:
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_ANTHROPIC_BASE_URL
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_ANTHROPIC_BASE_URL
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_ANTHROPIC_BASE_URL


def test_member_gets_base_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider_secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "")
    agent = _Agent(provider_base_url="http://127.0.0.1:9/v1", provider_api_key="sk-x")
    env = provider_guard.custom_endpoint_env(agent, ACP_BACKEND_CLAUDE)
    assert env == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9/v1", "ANTHROPIC_API_KEY": "sk-x"}


def test_shim_supplies_the_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider_secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "")
    agent = _Agent(use_shim=True, shim_port=9999, provider_base_url="http://ignored.invalid")
    env = provider_guard.custom_endpoint_env(agent, ACP_BACKEND_CLAUDE)
    assert env["ANTHROPIC_BASE_URL"] == f"http://{shim.DEFAULT_HOST}:9999"


def test_safe_mode_refusal_propagates_from_the_factory_helper() -> None:
    agent = _Agent(provider_base_url="http://8.8.8.8/v1", safe_mode=True)
    with pytest.raises(ValueError):
        provider_guard.custom_endpoint_env(agent, ACP_BACKEND_CLAUDE)


def test_unconfigured_member_gets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(provider_secrets.ENV_VAR, raising=False)
    monkeypatch.setattr(provider_secrets, "load_provider_key", lambda: "")
    assert provider_guard.custom_endpoint_env(_Agent(), ACP_BACKEND_CLAUDE) == {}


# ── Shim translation ──


def test_system_prompt_becomes_a_system_message() -> None:
    out = shim.anthropic_to_openai(
        {"model": "m", "system": [{"type": "text", "text": "be terse"}], "messages": []}
    )
    assert out["messages"][0] == {"role": "system", "content": "be terse"}


def test_tool_results_precede_remaining_user_text() -> None:
    """A tool result must reach the backend before the text that follows it."""
    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "42"},
                    {"type": "text", "text": "and now?"},
                ],
            }
        ],
    }
    msgs = shim.anthropic_to_openai(body)["messages"]
    assert msgs[0] == {"role": "tool", "tool_call_id": "t1", "content": "42"}
    assert msgs[1] == {"role": "user", "content": "and now?"}


def test_assistant_tool_use_becomes_tool_calls() -> None:
    body = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {"p": "."}}],
            }
        ],
    }
    call = shim.anthropic_to_openai(body)["messages"][0]["tool_calls"][0]
    assert call["id"] == "t1"
    assert call["function"]["name"] == "ls"
    assert json.loads(call["function"]["arguments"]) == {"p": "."}


def test_tools_are_advertised_as_functions() -> None:
    body = {
        "model": "m",
        "messages": [],
        "tools": [{"name": "grep", "description": "search", "input_schema": {"type": "object"}}],
    }
    fn = shim.anthropic_to_openai(body)["tools"][0]
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "grep"


def test_streaming_requests_a_usage_chunk() -> None:
    out = shim.anthropic_to_openai({"model": "m", "messages": [], "stream": True})
    assert out["stream_options"] == {"include_usage": True}


def test_non_streaming_asks_for_no_usage_chunk() -> None:
    assert "stream_options" not in shim.anthropic_to_openai({"model": "m", "messages": []})


def test_response_translation_carries_text_and_tool_use() -> None:
    payload = {
        "id": "x",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "sure",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "ls", "arguments": '{"p":"."}'}}
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    out = shim.openai_to_anthropic(payload, "m")
    assert out["content"][0] == {"type": "text", "text": "sure"}
    assert out["content"][1]["input"] == {"p": "."}
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_malformed_tool_arguments_keep_the_turn_alive() -> None:
    """Unparseable arguments surface as _raw rather than dropping the reply."""
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{"}}]
                },
            }
        ]
    }
    out = shim.openai_to_anthropic(payload, "m")
    assert out["content"][0]["input"] == {"_raw": "{"}


def test_empty_reply_still_carries_a_content_block() -> None:
    out = shim.openai_to_anthropic({"choices": [{"message": {}}]}, "m")
    assert out["content"] == [{"type": "text", "text": ""}]


def test_shim_binds_loopback_by_default() -> None:
    assert shim.DEFAULT_HOST == "127.0.0.1"
