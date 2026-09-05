"""The Windows swap from npm's ``.cmd`` shim to the executable it wraps.

Node refuses to spawn a ``.cmd`` without a shell, and the adapter's SDK spawns
``pathToClaudeCodeExecutable`` directly, so the shim must never reach it.

tmp_path trees only: no subprocess and no PATH lookup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _npm_layout(root: Path, *, entry: str = "bin/claude.exe") -> Path:
    """A minimal npm global-bin tree: a .cmd shim over a real package binary."""
    from kiro_crew.acp.client import CLAUDE_CODE_NPM_PKG

    shim_path = root / "claude.cmd"
    shim_path.write_text("@ECHO off\n", encoding="utf-8")
    pkg = root / "node_modules" / CLAUDE_CODE_NPM_PKG
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"bin": {"claude": entry}}), encoding="utf-8")
    target = pkg / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    return shim_path


def test_native_binary_is_found_behind_the_cmd_shim(tmp_path: Path) -> None:
    from kiro_crew.acp.client import _windows_native_claude_binary

    shim_path = _npm_layout(tmp_path)
    resolved = _windows_native_claude_binary(str(shim_path))
    assert resolved is not None
    assert Path(resolved).name == "claude.exe"


def test_missing_package_degrades_to_none(tmp_path: Path) -> None:
    from kiro_crew.acp.client import _windows_native_claude_binary

    shim_path = tmp_path / "claude.cmd"
    shim_path.write_text("@ECHO off\n", encoding="utf-8")
    assert _windows_native_claude_binary(str(shim_path)) is None


def test_a_bin_entry_that_is_itself_a_shim_is_refused(tmp_path: Path) -> None:
    """Swapping one unspawnable path for another would fix nothing."""
    from kiro_crew.acp.client import _windows_native_claude_binary

    shim_path = _npm_layout(tmp_path, entry="bin/claude.cmd")
    assert _windows_native_claude_binary(str(shim_path)) is None


def test_prefer_native_is_a_noop_off_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
    shim_path = str(_npm_layout(tmp_path))
    assert acp_client._prefer_native_binary(shim_path) == shim_path


def test_prefer_native_swaps_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
    shim_path = str(_npm_layout(tmp_path))
    assert acp_client._prefer_native_binary(shim_path).endswith("claude.exe")


def test_prefer_native_leaves_a_real_executable_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
    assert acp_client._prefer_native_binary("C:/x/claude.exe") == "C:/x/claude.exe"


def test_prefer_native_passes_none_through() -> None:
    from kiro_crew.acp import client as acp_client

    assert acp_client._prefer_native_binary(None) is None
