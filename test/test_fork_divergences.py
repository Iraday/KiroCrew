"""Fork divergences that only prose protects, made executable.

This is a public fork that deliberately differs from upstream, and the runbook
for reconciling the two is `docs/guides/upstream-sync.md`. Most of its register
is already pinned by a test somewhere else (harness parity, scrub lint, the
built-in-app rule). The entries pinned HERE are the ones whose only other
guardian was a paragraph, which is the weakest kind: nothing executes a
paragraph, so an upstream merge can revert one and every gate still passes.

Deliberately asserted against SUBSTANCE, not phrasing -- a command that must
work, a claim that must not be made -- so an editor can rewrite these documents
freely and only a change of meaning goes red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    BACKEND_LOGIN_COMMAND,
)

#: The docs that ship inside the package and are read by users.
SHIPPED_DOCS = Path(kiro_crew.__file__).resolve().parent / "docs"

#: Repo-root docs. `test/` sits at the root, so its parent is the checkout.
REPO_DOCS = Path(__file__).resolve().parents[1] / "docs"

_GETTING_STARTED = SHIPPED_DOCS / "getting-started.md"
_CONFIGURATION = SHIPPED_DOCS / "configuration.md"
_TROUBLESHOOTING = SHIPPED_DOCS / "troubleshooting.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"shipped doc is missing: {path}"
    return path.read_text(encoding="utf-8")


def _all_shipped() -> str:
    return "\n".join(_read(p) for p in (_GETTING_STARTED, _CONFIGURATION, _TROUBLESHOOTING))


# ── The claim upstream keeps reasserting ──


@pytest.mark.parametrize(
    "claim",
    [
        "is the only provider",
        "kiro-cli is the agent backend and is required",
    ],
)
def test_shipped_docs_do_not_call_kiro_cli_the_only_backend(claim: str) -> None:
    """Four harnesses are selectable, so this sentence is simply false.

    It is the divergence most likely to be merged back without anyone noticing,
    because prose does not fail a build.
    """
    assert claim not in _all_shipped(), (
        f"A shipped doc claims {claim!r}. Four harnesses are selectable "
        "(see docs/guides/upstream-sync.md); reconcile rather than accept."
    )


def test_every_backend_sign_in_command_reaches_the_user() -> None:
    """A harness a user can select but cannot sign in to is not documented.

    Read from the table rather than restated, so adding a harness without
    documenting its sign-in fails here rather than shipping quietly.
    """
    shipped = _all_shipped()
    for backend, command in sorted(BACKEND_LOGIN_COMMAND.items()):
        assert command in shipped, (
            f"No shipped doc names {command!r} for backend {backend!r}. "
            "Every selectable harness needs its sign-in documented."
        )


def test_configuration_lists_the_selectable_backends() -> None:
    text = _read(_CONFIGURATION)
    for value in ("`claude`", "`codex`", "`kas`"):
        assert value in text, f"configuration.md does not list {value} as a backend value"


def test_a_backend_switch_is_documented_as_affecting_new_sessions_only() -> None:
    """A running session keeps the backend it started on, which surprises people."""
    assert "new sessions only" in _read(_CONFIGURATION)


# ── Security limitations that must reach the person choosing a harness ──


def test_shipped_docs_disclose_the_codex_passive_read_gap() -> None:
    """ACP v1 cannot expose a passive read to Crew's gate.

    Hiding credential directories from the child is containment, not
    observation, and the wording must never be upgraded to imply otherwise.
    """
    text = _read(_CONFIGURATION) + _read(_TROUBLESHOOTING)
    assert "passive" in text and "read" in text, "the Codex passive-read gap is not disclosed"
    assert ACP_BACKEND_CODEX in text.lower()


def test_shipped_docs_disclose_the_claude_pre_approval_escape() -> None:
    """A tool pre-approved in Claude's own settings never reaches Crew's gate."""
    text = _read(_CONFIGURATION)
    assert "settings.json" in text, "the Claude pre-approval escape is not disclosed"
    assert ACP_BACKEND_CLAUDE in text.lower()


def test_troubleshooting_never_asks_for_a_credential_in_chat() -> None:
    """Crew signs no backend in, and must say so where a signed-out user looks."""
    assert "never asks you to paste a credential into chat" in _read(_TROUBLESHOOTING)


# ── Spec-level consistency ──


def test_harness_parity_does_not_call_codex_dormant() -> None:
    """Codex is selectable; the spec's own later paragraphs already said so.

    The opening paragraph disagreed with them for a while, which is exactly the
    drift the post-merge consistency sweep exists to catch.
    """
    text = (REPO_DOCS / "system-specs" / "modules" / "harness-parity.md").read_text(
        encoding="utf-8"
    )
    assert "dormant `ACP_BACKEND_CODEX`" not in text
    assert "dormant ACP_BACKEND_CODEX" not in text


def test_the_sync_runbook_exists_and_is_indexed() -> None:
    """The register is only useful if the next person finds it."""
    runbook = REPO_DOCS / "guides" / "upstream-sync.md"
    assert runbook.is_file(), "docs/guides/upstream-sync.md is missing"
    index = (REPO_DOCS / "guides" / "README.md").read_text(encoding="utf-8")
    assert "upstream-sync.md" in index, "the runbook is not listed in docs/guides/README.md"
