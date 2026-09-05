"""The readiness gate only applies to installs that actually spawn kiro-cli.

Every hazard the gate guards is a kiro-cli hazard, so a Claude- or Codex-only
install must not be held behind the readiness of a binary it never runs. The
scope test fails closed, which is what these cases pin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.dashboard import kiro_readiness


def _config(acp_backend: str, member: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(acp_backend=acp_backend, member_acp_backend=member)
    )


@pytest.fixture
def load_config(monkeypatch: pytest.MonkeyPatch):
    """Pin what ``KiroCrewConfig.load`` returns inside the gate's thread hop."""
    from kiro_crew.config.loader import KiroCrewConfig

    def _set(cfg: object) -> None:
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))

    return _set


# ── Which installs are in scope ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acp_backend", "member"),
    [
        (ACP_BACKEND_KIRO, ACP_BACKEND_KAS),
        (ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE),
        (ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE),
        # The default shape: a Claude chat backend still routes member DMs to
        # KAS, which is kiro-cli's own relay, so kiro-cli readiness still counts.
        (ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS),
    ],
)
async def test_kiro_dependent_installs_stay_gated(
    load_config, acp_backend: str, member: str
) -> None:
    load_config(_config(acp_backend, member))
    assert await kiro_readiness.install_depends_on_kiro_cli() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acp_backend", "member"),
    [
        (ACP_BACKEND_CLAUDE, ACP_BACKEND_CLAUDE),
        (ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE),
    ],
)
async def test_installs_that_never_spawn_kiro_are_out_of_scope(
    load_config, acp_backend: str, member: str
) -> None:
    load_config(_config(acp_backend, member))
    assert await kiro_readiness.install_depends_on_kiro_cli() is False


# ── Fail-closed ──


@pytest.mark.asyncio
async def test_unreadable_config_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(*a: object, **k: object) -> object:
        raise OSError("config is gone")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
    assert await kiro_readiness.install_depends_on_kiro_cli() is True


@pytest.mark.asyncio
async def test_missing_field_gates(load_config) -> None:
    load_config(SimpleNamespace(agent=SimpleNamespace()))
    assert await kiro_readiness.install_depends_on_kiro_cli() is True


@pytest.mark.asyncio
async def test_a_degraded_value_gates(load_config) -> None:
    """An unknown backend normalizes to kiro (the empty string), which is in the set."""
    load_config(_config(ACP_BACKEND_KIRO, ACP_BACKEND_KIRO))
    assert await kiro_readiness.install_depends_on_kiro_cli() is True


# ── The gate itself ──


class _FakeRequest:
    path = "/api/models"

    def __init__(self) -> None:
        self.app: dict[str, object] = {}


@pytest.mark.asyncio
async def test_out_of_scope_install_is_not_refused(
    load_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No service at all, which normally fails closed, must still pass here."""
    load_config(_config(ACP_BACKEND_CLAUDE, ACP_BACKEND_CLAUDE))
    probed = False

    async def _never(_service: object) -> bool:
        nonlocal probed
        probed = True
        return False

    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", _never)
    assert await kiro_readiness.reject_if_kiro_unverified(_FakeRequest()) is None
    assert not probed, "an out-of-scope install must not pay for the readiness re-probe"


@pytest.mark.asyncio
async def test_in_scope_install_with_no_service_still_fails_closed(load_config) -> None:
    load_config(_config(ACP_BACKEND_KIRO, ACP_BACKEND_KAS))
    resp = await kiro_readiness.reject_if_kiro_unverified(_FakeRequest())
    assert resp is not None
    assert resp.status == 503


@pytest.mark.asyncio
async def test_in_scope_install_passes_when_verified(
    load_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_config(_config(ACP_BACKEND_KIRO, ACP_BACKEND_KAS))

    async def _ready(_service: object) -> bool:
        return True

    monkeypatch.setattr(kiro_readiness, "kiro_verified_ready", _ready)
    assert await kiro_readiness.reject_if_kiro_unverified(_FakeRequest()) is None


# ── The first-run setup gate asks a narrower question ──


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO])
@pytest.mark.parametrize("chat", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_first_run_gate_holds_for_a_kiro_chat_harness(
    load_config, chat: str, member: str
) -> None:
    """Whatever member DMs are set to, a kiro chat harness still needs the binary."""
    load_config(_config(chat, member))
    assert await kiro_readiness.first_run_gate_requires_kiro_cli() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
async def test_first_run_gate_opens_on_the_shipped_member_default(load_config, chat: str) -> None:
    """The case the whole predicate exists for.

    ``member_acp_backend`` DEFAULTS to ``kas``, so a fresh install that switches
    only its chat harness still reads as kiro-dependent to the 503 gate. Holding
    the full-screen first-run block on that default would make the switch
    unreachable: the gate wraps the dashboard, so the panel that changes the
    member field is behind the very screen the field is keeping shut.
    """
    load_config(_config(chat, ACP_BACKEND_KAS))
    assert await kiro_readiness.first_run_gate_requires_kiro_cli() is False


@pytest.mark.asyncio
async def test_the_two_gates_disagree_by_exactly_the_member_field(load_config) -> None:
    """Pins the difference itself, so narrowing one never silently narrows the other."""
    load_config(_config(ACP_BACKEND_CODEX, ACP_BACKEND_KAS))
    assert await kiro_readiness.install_depends_on_kiro_cli() is True
    assert await kiro_readiness.first_run_gate_requires_kiro_cli() is False


@pytest.mark.asyncio
async def test_first_run_gate_fails_closed_on_an_unreadable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(*a: object, **k: object) -> object:
        raise OSError("config is gone")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
    assert await kiro_readiness.first_run_gate_requires_kiro_cli() is True


@pytest.mark.asyncio
async def test_first_run_gate_fails_closed_on_a_missing_field(load_config) -> None:
    load_config(SimpleNamespace(agent=SimpleNamespace()))
    assert await kiro_readiness.first_run_gate_requires_kiro_cli() is True
