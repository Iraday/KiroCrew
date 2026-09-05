"""Sign-in guidance names the harness that actually failed.

A shared string is worse than no string here: telling a Codex user to run
``kiro-cli login`` names a binary they may not have, and nothing in the message
lets them tell wrong advice apart from a broken install.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import not_logged_in_message
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    BACKEND_AUTH_STATUS_COMMAND,
    BACKEND_LOGIN_COMMAND,
    auth_status_command_for,
    login_command_for,
)

# ── The tables ──


def test_every_known_backend_has_a_login_command() -> None:
    """A harness with no entry sends its users nowhere at all."""
    assert set(BACKEND_LOGIN_COMMAND) == ACP_BACKENDS_KNOWN


def test_status_commands_are_a_subset() -> None:
    """An entry claims the command EXISTS and is read-only, so it is not guessed."""
    assert set(BACKEND_AUTH_STATUS_COMMAND) <= ACP_BACKENDS_KNOWN


def test_kas_shares_kiro_sign_in() -> None:
    """KAS is kiro-cli's own relay, not a second binary to authenticate."""
    assert login_command_for(ACP_BACKEND_KAS) == login_command_for(ACP_BACKEND_KIRO)


def test_an_unknown_backend_gets_no_command_rather_than_kiros() -> None:
    """Falling back to kiro's command would read as authoritative and be wrong."""
    assert login_command_for("no-such-harness") == ""
    assert auth_status_command_for("no-such-harness") == ""


def test_the_kiro_prerequisite_constant_is_derived() -> None:
    """One owner: a second literal is how the two surfaces drift apart."""
    from kiro_crew.kiro_prerequisite import KIRO_CLI_LOGIN_COMMAND

    assert KIRO_CLI_LOGIN_COMMAND == BACKEND_LOGIN_COMMAND[ACP_BACKEND_KIRO]


# ── The message ──


@pytest.mark.parametrize(
    ("backend", "expected_command"),
    [
        (ACP_BACKEND_KIRO, "kiro-cli login"),
        (ACP_BACKEND_KAS, "kiro-cli login"),
        (ACP_BACKEND_CLAUDE, "claude /login"),
        (ACP_BACKEND_CODEX, "codex login"),
    ],
)
def test_the_message_names_that_backends_command(backend: str, expected_command: str) -> None:
    assert expected_command in not_logged_in_message(backend)


@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_a_non_kiro_backend_is_never_told_to_run_kiro_cli(backend: str) -> None:
    """The defect this exists to prevent, pinned directly."""
    assert "kiro-cli" not in not_logged_in_message(backend)


def test_codex_message_offers_the_read_only_check() -> None:
    message = not_logged_in_message(ACP_BACKEND_CODEX)
    assert "codex login status" in message


def test_claude_message_omits_a_check_it_does_not_have() -> None:
    """No status subcommand exists, so none is invented."""
    message = not_logged_in_message(ACP_BACKEND_CLAUDE)
    assert "Check with" not in message


def test_an_unknown_backend_gets_generic_guidance() -> None:
    message = not_logged_in_message("no-such-harness")
    assert "kiro-cli" not in message
    assert "its own CLI" in message


def test_no_message_leaks_a_wire_id() -> None:
    """The kiro backend is the empty string; no user can be shown that."""
    for backend in ACP_BACKENDS_KNOWN:
        assert "''" not in not_logged_in_message(backend)


# ── The session-expired branch of the shared formatter ──


def _expired_error() -> dict:
    return {"code": -32000, "message": "401 Unauthorized: session expired"}


@pytest.mark.parametrize(
    ("backend", "expected"),
    [(ACP_BACKEND_KIRO, "kiro-cli login"), (ACP_BACKEND_CODEX, "codex login")],
)
def test_expiry_message_follows_the_backend(backend: str, expected: str) -> None:
    from kiro_crew.acp.client import _format_acp_error

    formatted = _format_acp_error(_expired_error(), None, backend)
    assert expected in formatted


def test_expiry_message_without_a_backend_names_no_binary() -> None:
    """An un-threaded caller must degrade to generic, never to kiro's command."""
    from kiro_crew.acp.client import _format_acp_error

    formatted = _format_acp_error(_expired_error(), None, None)
    assert "kiro-cli" not in formatted


# ── The dashboard payload ──


def test_status_rows_carry_the_recovery_commands() -> None:
    """A row that reports only installation leaves a signed-out reader stuck."""
    from kiro_crew.dashboard.handlers.acp_backend_status import _snapshot

    rows = {row["id"]: row for row in _snapshot()}
    assert rows[ACP_BACKEND_CODEX]["login_command"] == "codex login"
    assert rows[ACP_BACKEND_CODEX]["auth_status_command"] == "codex login status"
    assert rows[ACP_BACKEND_KIRO]["login_command"] == "kiro-cli login"


def test_recovery_commands_are_served_regardless_of_install_state() -> None:
    """Sign-in failure shows up on an INSTALLED row, which is where it is needed."""
    from kiro_crew.dashboard.handlers.acp_backend_status import _snapshot

    for row in _snapshot():
        assert row["login_command"] == login_command_for(row["id"])
