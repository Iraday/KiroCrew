"""Codex gets Crew's MCP tools, and the two filters that decide which ones.

The delivery itself is the point: before this, a codex session ran with no Crew
tools at all. What needs pinning is not that the array is non-empty but the two
places it is deliberately NARROWER than the spec, because both fail in the
expensive direction if they regress -- one widens the session's tool surface, the
other kills the whole session rather than one server.
"""

from __future__ import annotations

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.providers.mirrors import mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition
from kiro_crew.providers.mirrors.codex import CodexMirror


def _mirror() -> CodexMirror:
    m = mirror_for(ACP_BACKEND_CODEX)
    assert isinstance(m, CodexMirror)
    return m


# ── Registration ──


def test_codex_has_a_mirror_at_all() -> None:
    """The registry answers with a mirror rather than a NO_MIRROR reason."""
    assert mirror_for(ACP_BACKEND_CODEX) is not None


def test_codex_is_in_the_session_mcp_array_set() -> None:
    """H6: the array is this harness's whole MCP surface, so it joins the SET.

    Membership rather than a second ``_is_codex`` branch at the call site is what
    the capability-set design asks for, and it is what makes
    ``_session_mcp_servers`` serve this backend at all.
    """
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_SESSION_MCP_ARRAY


# ── The delivery, and why it needs no ownership flag ──


def test_the_wire_face_returns_the_mcp_servers_key() -> None:
    params = _mirror().session_params(None)
    assert "mcpServers" in params
    assert isinstance(params["mcpServers"], list)


def test_delivery_does_not_depend_on_the_claude_ownership_flag() -> None:
    """Codex is not in the class that must fail closed on it.

    Claude's precondition exists because its routing (seeded settings) is declared
    but unenforced, so a pre-approved tool can skip the gate. Codex routes through
    session-config, which ``acp_tool_gate`` verifies and applies before the first
    prompt and refuses the session without. Gating on the flag as well would
    withhold tools from the one adapter that provably cannot self-approve them.
    """
    without = _mirror().session_params(None)
    with_flag = _mirror().session_params(None, permission_surface_owned=True)
    assert without == with_flag


def test_the_mcp_ruling_states_why_the_precondition_is_absent() -> None:
    """A ruling saying only "delivered" would let the next backend copy the
    delivery and drop the mechanism that makes it safe."""
    ruling = _mirror().rulings()[Concern.MCP_SERVERS]
    assert ruling.disposition is Disposition.DELIVERED
    assert "read-only" in ruling.reason


# ── Filter 1: a narrowed server is withheld whole ──


def test_a_server_with_per_tool_narrowing_is_withheld(monkeypatch) -> None:
    """Dropping a restriction while forwarding the server it narrows WIDENS the
    tool surface, which the repo already treats as a defect elsewhere. This
    backend has nowhere to re-apply the restriction, so the server goes."""
    import kiro_crew.providers.mirrors.codex as codex_mirror

    monkeypatch.setattr(
        codex_mirror,
        "session_mcp_servers",
        lambda *a, **k: [
            {"name": "narrowed", "type": "stdio", "command": "x"},
            {"name": "open", "type": "stdio", "command": "y"},
        ],
    )
    monkeypatch.setattr(
        codex_mirror,
        "session_mcp_deny_rules",
        lambda *a, **k: ["mcp__narrowed__some_tool"],
    )
    kept = _mirror().session_params("agent")["mcpServers"]
    assert [s["name"] for s in kept] == ["open"]


def test_the_denied_tools_ruling_names_the_missing_channel() -> None:
    ruling = _mirror().rulings()[Concern.DENIED_TOOLS]
    assert ruling.disposition is Disposition.NO_CHANNEL
    assert ruling.channel


# ── Filter 2: an unadvertised transport is dropped ──


def _servers(monkeypatch) -> None:
    import kiro_crew.providers.mirrors.codex as codex_mirror

    monkeypatch.setattr(
        codex_mirror,
        "session_mcp_servers",
        lambda *a, **k: [
            {"name": "s_stdio", "type": "stdio", "command": "x"},
            {"name": "s_http", "type": "http", "url": "https://example.invalid"},
            {"name": "s_sse", "type": "sse", "url": "https://example.invalid"},
        ],
    )
    monkeypatch.setattr(codex_mirror, "session_mcp_deny_rules", lambda *a, **k: [])


def test_stdio_survives_with_no_advertised_capabilities(monkeypatch) -> None:
    """stdio is the protocol baseline, so it needs no advertisement."""
    _servers(monkeypatch)
    kept = _mirror().session_params("agent", mcp_capabilities={})["mcpServers"]
    assert [s["name"] for s in kept] == ["s_stdio"]


def test_an_unadvertised_transport_is_dropped_not_sent(monkeypatch) -> None:
    """codex-acp answers -32602 for a transport it does not support, and that
    fails the WHOLE session/new rather than skipping the one server -- so one
    http entry against an http-less adapter costs every tool in the array."""
    _servers(monkeypatch)
    kept = _mirror().session_params("agent", mcp_capabilities=None)["mcpServers"]
    assert "s_http" not in [s["name"] for s in kept]
    assert "s_sse" not in [s["name"] for s in kept]


def test_an_advertised_transport_is_kept(monkeypatch) -> None:
    _servers(monkeypatch)
    kept = _mirror().session_params("agent", mcp_capabilities={"http": True, "sse": False})[
        "mcpServers"
    ]
    names = [s["name"] for s in kept]
    assert "s_http" in names
    assert "s_stdio" in names
    assert "s_sse" not in names


def test_a_missing_type_is_treated_as_stdio(monkeypatch) -> None:
    """``session_mcp`` always spells the transport, but a hand-built entry may
    not, and defaulting to the optional side would drop it for no reason."""
    import kiro_crew.providers.mirrors.codex as codex_mirror

    monkeypatch.setattr(
        codex_mirror, "session_mcp_servers", lambda *a, **k: [{"name": "bare", "command": "x"}]
    )
    monkeypatch.setattr(codex_mirror, "session_mcp_deny_rules", lambda *a, **k: [])
    kept = _mirror().session_params("agent", mcp_capabilities={})["mcpServers"]
    assert [s["name"] for s in kept] == ["bare"]
