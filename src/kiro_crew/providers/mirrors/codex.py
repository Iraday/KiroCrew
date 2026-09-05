"""Codex's agent-config mirror (``codex-acp``).

One face, not two. The adapter reads no file Kiro Crew owns -- ``~/.codex/config.toml``
belongs to the user, and create-or-decline forbids merging into it -- so everything
Crew projects onto this backend rides the ``session/new`` / ``session/load``
``mcpServers`` array. That single channel is why several concerns below are
``no-channel`` rather than delivered: they are real capabilities of the harness
with no path from the spec to it, not decisions.

**Why this mirror delivers tools without Claude's ownership precondition.**
:meth:`AgentConfigMirror.session_params` requires a mirror to fail closed on
``permission_surface_owned`` when the backend gates tool calls through a file Crew
may or may not own. Codex is not in that class. It routes through
``Routing.SESSION_CONFIG``: ``acp_tool_gate`` verifies ``session/new`` advertised
``mode=read-only``, applies it before the first prompt, and REFUSES the session
otherwise, which is why ``routing_verdict`` answers ``ROUTED`` for this backend and
only ``INDETERMINATE`` for Claude. The precondition Claude's flag stands in for is
therefore established here by a stronger mechanism, and gating on the flag as well
would withhold tools from the one adapter that provably cannot self-approve them.

The trade is recorded rather than hidden: ACP v1 still cannot force a prompt for a
passive READ, so this harness's reads do not reach the gate. That is compensated at
the OS boundary by ``acp_tool_gate.adapter_hidden_credential_dirs``, which is a
different control covering a different reader.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import session_mcp_deny_rules, session_mcp_servers
from kiro_crew.acp_backends import ACP_BACKEND_CODEX
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
)

logger = logging.getLogger(__name__)

_D = Disposition

#: Transports every ACP agent is required to accept, so they need no advertisement.
#: ``stdio`` is the baseline of the protocol; ``http`` and ``sse`` are optional and
#: are only sent when the handshake said so.
_ALWAYS_SUPPORTED_TRANSPORTS = frozenset({"stdio"})

#: ``agentCapabilities.mcpCapabilities`` key that gates each optional transport.
_OPTIONAL_TRANSPORT_CAPABILITY = {"http": "http", "sse": "sse"}

#: Prefix of a Claude/Codex-style per-tool deny rule, ``mcp__<server>__<tool>``.
_DENY_RULE_PREFIX = "mcp__"


def _servers_with_per_tool_narrowing(agent: str | None, work_dir: str | Path | None) -> set:
    """Server names whose spec entry disables individual tools.

    Derived from the deny rules rather than re-read from the spec, so this and the
    rules the Claude backend writes cannot disagree about which servers are
    narrowed. A rule is ``mcp__<server>__<tool>``; the server is what matters here
    and the tool is not, because this backend has nowhere to put a per-tool rule.
    """
    narrowed: set = set()
    for rule in session_mcp_deny_rules(agent, work_dir=work_dir):
        if not rule.startswith(_DENY_RULE_PREFIX):
            continue
        body = rule[len(_DENY_RULE_PREFIX) :]
        server, _, tool = body.partition("__")
        if server and tool:
            narrowed.add(server)
    return narrowed


def _transport_of(entry: Mapping[str, Any]) -> str:
    """The transport an array element declares. ``session_mcp`` always spells it."""
    declared = entry.get("type")
    return declared if isinstance(declared, str) and declared else "stdio"


class CodexMirror(AgentConfigMirror):
    """Projects the agent spec onto codex-acp's session MCP array."""

    backend = ACP_BACKEND_CODEX

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers. The adapter reads no agent "
                "file, so this array is the session's whole MCP surface. Delivered "
                "without Claude's settings-ownership precondition because this "
                "backend routes through session-config (mode=read-only, verified "
                "and applied before the first prompt, session refused otherwise), "
                "so every tool call reaches Crew's gate by a mechanism the file "
                "seed only declares. Two entries are dropped rather than sent: a "
                "server whose spec narrows individual tools, and one whose "
                "transport the adapter did not advertise",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied HERE as the allowlist deciding "
                "which servers enter the array, exactly as on Claude. A missing or "
                "non-list `tools` is an empty allowlist, matching kiro-cli. The same "
                "residual asymmetry applies: an `@server/tool` grant narrows to one "
                "tool on kiro-cli but mounts the whole server here",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.NO_CHANNEL,
                "`mcpServers.<name>.disabledTools` is a kiro-cli-only key that "
                "cannot ride along in an array element, and this backend has no "
                "Crew-owned settings file to re-apply it in the way Claude's "
                "permissions.deny does. Dropping a restriction while still "
                "forwarding the server it narrows would WIDEN the session's tool "
                "surface behind the user's back, so the whole server is withheld "
                "instead. Narrower than the user asked for, never wider, and the "
                "withhold is logged with the server name",
                channel="a Crew-owned Codex settings file, or a per-tool deny the "
                "session-config channel can carry; neither exists today",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "same reason as every other backend: pre-approving a call inside "
                "the adapter means it never calls back to the host, so it would "
                "skip Crew's permission gate, governance ceiling and SEL audit. "
                "Every MCP call on this backend must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "session/set_config_option('model', ...) — this backend is in "
                "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION, so the model is a session "
                "verb rather than a file the mirror writes",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.NO_CHANNEL,
                "the adapter advertises its own model set at session/new and Crew "
                "has no file here to narrow it in. Nothing is lost silently: an "
                "unusable pick is caught by the shared advertised-set predicate "
                "(acp.client.model_is_unusable) before it reaches the wire",
                channel="a Crew-owned Codex settings file, the way "
                "settings.local.json carries availableModels for Claude",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.DELIVERED,
                "session/set_config_option('mode', 'read-only'), verified against "
                "what session/new advertised and applied before the first prompt by "
                "acp_tool_gate, which refuses the session when the option is absent. "
                "This is the strongest permission channel of any adapted harness and "
                "is what lets MCP_SERVERS above be delivered unconditionally",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT — steering files are injected as context text, not "
                "projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.NO_CHANNEL,
                "the spec's `hooks` block is executed by the harness, and nothing "
                "writes it for this backend, so a user's per-agent hooks reach "
                "kiro-cli and no other. Crew's OWN hooks (hooks.py, fired on ACP "
                "tool events) are unaffected and work here — this gap is only the "
                "spec block",
                channel="a Crew-owned Codex settings file; unlike Claude there is "
                "no such file today, so this is a stage behind that backend's gap",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        work_dir: str | Path | None = None,
        mcp_capabilities: Mapping[str, Any] | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        """The ``mcpServers`` array for a codex session.

        ``permission_surface_owned`` is accepted and IGNORED — see the module
        docstring for why this backend is not in the class that must fail closed
        on it.

        Two filters, and both drop rather than degrade:

        * **Per-tool narrowing.** A server whose spec disables individual tools is
          withheld whole, because there is nowhere here to re-apply the
          restriction and forwarding it without one widens the surface.
        * **Unadvertised transport.** codex-acp answers ``session/new`` with
          ``-32602`` for a transport it does not support, and that fails the WHOLE
          session rather than skipping the one server — so a single http entry
          against an adapter without http capability costs every tool in the
          array. ``mcp_capabilities`` is what the handshake advertised; absent, only
          the always-supported transports are sent.

        Blocking (reads the agent spec), so callers run it off the event loop and
        serve the shared session/new call site from the cache (harness-parity H13).
        """
        servers = session_mcp_servers(
            agent,
            stub_server_names=stub_server_names,
            work_dir=work_dir,  # type: ignore[arg-type]
        )
        narrowed = _servers_with_per_tool_narrowing(agent, work_dir)
        allowed = set(_ALWAYS_SUPPORTED_TRANSPORTS)
        caps = mcp_capabilities or {}
        for transport, cap_key in _OPTIONAL_TRANSPORT_CAPABILITY.items():
            if caps.get(cap_key):
                allowed.add(transport)

        kept: list[dict[str, Any]] = []
        for entry in servers:
            name = str(entry.get("name") or "")
            if name in narrowed:
                logger.warning(
                    "codex session MCP: withholding server %r — its spec disables "
                    "individual tools and this backend has no channel to re-apply "
                    "that restriction, so forwarding it would widen the tool surface",
                    name,
                )
                continue
            transport = _transport_of(entry)
            if transport not in allowed:
                logger.warning(
                    "codex session MCP: dropping server %r — the adapter did not "
                    "advertise the %r transport, and one unsupported entry fails "
                    "the whole session/new rather than just that server",
                    name,
                    transport,
                )
                continue
            kept.append(entry)
        return {"mcpServers": kept}
