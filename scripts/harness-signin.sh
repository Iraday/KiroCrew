#!/bin/bash
# Sign the CONFIGURED harness in, when it is not already.
#
# Called by: Makefile (`make signin`, and `make setup` through it)
# Platforms: macOS, Linux. On Windows use WSL — see the sandbox note below.
#
# Installed is not signed in, and the two fail at different times: a missing
# binary is caught by the install probe, a missing credential only at the first
# turn, as a chat error card long after setup said it was done. This closes that
# gap by asking the harness itself before anything is spawned.
#
# Kiro Crew never sees the credential. This hands the terminal to the harness's
# own login command; the harness owns the OAuth round trip and its own store.
# That is also why a non-interactive run does NOT attempt one: there is nobody
# to answer a browser prompt in CI, so it reports the command and exits 0 rather
# than hanging a build on stdin.
#
# Exit code is deliberately 0 in every "cannot check" path. This runs inside
# `make setup`, and a harness that cannot be probed is not a reason to fail an
# otherwise complete install — the readiness report that follows states what is
# unknown.

set -u

VENV="${VENV:-.venv}"
PY="$VENV/bin/python"
KIROCREW="$VENV/bin/kirocrew"

if [ ! -x "$PY" ]; then
  echo "  ! $PY not found — run 'make backend' first" >&2
  exit 0
fi

# The harness this install actually drives. Anything else is a sign-in the user
# did not ask for: probing every known backend would send someone through a
# Codex OAuth flow because they happen to have the adapter installed.
backend="$("$KIROCREW" config get agent.acp_backend 2>/dev/null | tr -d '"' | tr -d "'" | tr -d ' ')"
case "$backend" in
  *[!a-z]*) backend="" ;;  # kiro is the empty string; reject anything odd onto it
esac

# "|" separated, NOT whitespace: every one of these commands is multi-word
# ("claude auth login", "kiro-cli whoami"), so default field splitting would put
# "auth" in login_cmd and scatter the rest across the remaining fields.
IFS='|' read -r policy_id login_cmd status_cmd <<EOF
$("$PY" - "$backend" <<'PYEOF'
import sys
from kiro_crew.acp_backends import (
    POLICY_ID_BY_BACKEND,
    auth_status_command_for,
    login_command_for,
)

backend = sys.argv[1] if len(sys.argv) > 1 else ""
# "-" placeholders keep this a fixed three-field line: an absent command must not
# shift the fields left and make the reader parse a login command as a status one.
print(
    "|".join(
        (
            POLICY_ID_BY_BACKEND.get(backend, backend or "kiro"),
            login_command_for(backend) or "-",
            auth_status_command_for(backend) or "-",
        )
    )
)
PYEOF
)
EOF

if [ -z "${policy_id:-}" ]; then
  echo "  ! could not resolve the configured harness — skipping the sign-in check"
  exit 0
fi

echo "  harness: $policy_id"

if [ "$login_cmd" = "-" ]; then
  echo "  ! no sign-in command is known for '$policy_id' — see that harness's own docs"
  exit 0
fi

# ── Which binary, not just which command ──
#
# A bare name resolved through PATH is NOT good enough here, because a sign-in
# writes to a credential store and the wrong binary writes to the wrong one. Both
# failures have been observed on a working install:
#
# * A snap `codex` shadows the npm one on PATH. Snap confines it to a private
#   home, so `codex login` succeeds, reports "Logged in", and leaves
#   ~/snap/codex/<rev>/auth.json — which the ACP adapter cannot read. The adapter
#   runs a Codex bundled inside its own node_modules that reads ~/.codex, so the
#   session stays signed out while the CLI insists otherwise.
# * A `claude` reached over WSL interop (/mnt/c/...) reads the WINDOWS config. It
#   reports a login the sandboxed Linux child cannot see, because the sandbox
#   correctly denies /mnt.
#
# So resolve the binary whose store the HARNESS actually uses, and say plainly
# when PATH would have chosen a different one. Guessing silently is what turned
# both of these into long hunts.
_adapter_bundled_codex() {
  root="$(npm root -g 2>/dev/null)" || return 1
  [ -n "$root" ] || return 1
  find "$root/@agentclientprotocol/codex-acp/node_modules" \
    -path '*vendor*/bin/codex' -type f 2>/dev/null | head -1
}

resolve_harness_bin() {
  bare="$1"
  on_path="$(command -v "$bare" 2>/dev/null || true)"
  case "$policy_id" in
    codex)
      # Prefer the bundled binary unconditionally -- it is the one the adapter
      # runs, so its store is the session's store by construction. But only WARN
      # when the PATH copy would have used a DIFFERENT store, which is what
      # actually costs the user a wasted login. An ordinary npm/global codex
      # reads the same ~/.codex and is a perfectly good place to sign in; saying
      # otherwise would be the same over-claim in the other direction.
      bundled="$(_adapter_bundled_codex)"
      if [ -n "$bundled" ]; then
        case "$on_path" in
          /snap/*)
            echo "  ! '$bare' on PATH is $on_path — a snap, which is confined to" >&2
            echo "    its own home (~/snap/codex/<rev>/). A login there succeeds and" >&2
            echo "    reports 'Logged in', but writes a credential the adapter cannot" >&2
            echo "    read, so the session stays signed out." >&2
            echo "    Signing in with the adapter's own Codex instead." >&2
            ;;
          /mnt/*)
            echo "  ! '$bare' on PATH is $on_path — reached over interop, so it uses" >&2
            echo "    the host's config, which the Linux sandbox denies." >&2
            echo "    Signing in with the adapter's own Codex instead." >&2
            ;;
        esac
        echo "$bundled"
        return 0
      fi
      ;;
    claude)
      case "$on_path" in
        /mnt/*)
          echo "  ! '$bare' on PATH is $on_path — a Windows binary reached over" >&2
          echo "    interop. It reads the Windows config, which the Linux sandbox" >&2
          echo "    denies, so a login there is invisible to the session." >&2
          echo "    Install the native one: npm i -g @anthropic-ai/claude-code" >&2
          ;;
      esac
      ;;
  esac
  [ -n "$on_path" ] && echo "$on_path"
}

# ── Is it already signed in? ──
#
# Per-harness, because the shapes genuinely differ and a uniform exit-code test
# is wrong for at least one of them: `claude auth status` exits 0 whether or not
# it is signed in and reports the answer in its JSON body, so keying on the exit
# code would call every signed-out Claude "signed in" and skip the login this
# script exists to run.
signed_in="unknown"
if [ "$status_cmd" != "-" ]; then
  status_bin="$(resolve_harness_bin "${status_cmd%% *}")"
  status_args="${status_cmd#* }"
  if [ -n "$status_bin" ]; then
    # shellcheck disable=SC2086 # args are our own constants, word splitting intended
    out="$("$status_bin" $status_args 2>&1)"; rc=$?
    case "$policy_id" in
      claude)
        case "$out" in
          *'"loggedIn": true'*|*'"loggedIn":true'*) signed_in="yes" ;;
          *) signed_in="no" ;;
        esac
        ;;
      *)
        [ "$rc" -eq 0 ] && signed_in="yes" || signed_in="no"
        ;;
    esac
  else
    echo "  ! '${status_cmd%% *}' is not on PATH — cannot check sign-in state"
  fi
fi

if [ "$signed_in" = "yes" ]; then
  echo "  ✓ already signed in"
  exit 0
fi

# The same resolution the status check used, for the same reason: this is the
# call that WRITES a credential, so running the wrong binary is the failure that
# costs the most to diagnose -- it succeeds, it says "Logged in", and the session
# stays signed out.
login_bin="$(resolve_harness_bin "${login_cmd%% *}" 2>/dev/null)"
login_args="${login_cmd#* }"
if [ -z "$login_bin" ]; then
  echo "  ! '${login_cmd%% *}' is not on PATH, so '$login_cmd' cannot run."
  echo "    Install that harness's CLI first, then re-run: make signin"
  exit 0
fi

# What the user would have to type to reproduce this by hand. Spelled with the
# resolved path when it differs from the bare name, because "run codex login"
# is exactly the advice that sends someone to the snap copy.
if [ "$login_bin" = "$(command -v "${login_cmd%% *}" 2>/dev/null)" ]; then
  login_display="$login_cmd"
else
  login_display="$login_bin $login_args"
fi

# A login is an interactive OAuth round trip. Without a terminal there is nobody
# to complete it, and blocking on a prompt would hang the build.
# The state is reported as it actually is. "unknown" is not "no": saying "not
# signed in" to someone who is would send them through a login they did not need,
# and it is the same wrong-negative this script avoids elsewhere by never reading
# a credential store directly.
if [ ! -t 0 ] || [ ! -t 1 ]; then
  if [ "$signed_in" = "no" ]; then
    echo "  → not signed in. No terminal attached, so this is not started for you."
  else
    echo "  → sign-in state unknown. No terminal attached, so nothing is started."
  fi
  echo "    Run it yourself:  $login_display"
  exit 0
fi

if [ "$signed_in" = "no" ]; then
  echo "  → not signed in. Starting '$login_display' — complete it in the browser."
else
  echo "  → sign-in state unknown. Starting '$login_display' — complete it in the browser."
fi
echo ""
# Not captured and not piped: the harness draws its own prompts and needs the
# terminal. Its failure is reported, never fatal — an aborted login leaves an
# install that is otherwise finished.
# shellcheck disable=SC2086 # args are our own constants, word splitting intended
if "$login_bin" $login_args; then
  echo ""
  echo "  ✓ '$login_display' finished"
else
  echo ""
  echo "  ! '$login_display' did not complete. Re-run it yourself, or: make signin" >&2
fi
exit 0
