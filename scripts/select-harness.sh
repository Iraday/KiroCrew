#!/bin/bash
# Point agent.acp_backend at one harness, by its policy name.
#
# Called by: Makefile (`make start backend=codex`, `make use backend=claude`)
# Usage: select-harness.sh <kiro|claude|codex|kas|...>
#
# Why this exists rather than a bare `kirocrew config set`: that command WRITES
# whatever it is handed. An unknown value is not rejected there, it is accepted
# and then degraded at load time to the default backend with a warning in the
# gateway log -- which is the right behaviour for a persisted config that a newer
# build might understand, and the wrong one for a flag someone just typed. `make
# start backend=codx` would start Kiro, having said nothing the user was looking
# at. So the name is checked against the live selectable set FIRST, and a typo is
# a refusal.
#
# It also translates the policy name. The kiro backend is spelled as the empty
# string in code and config, which nobody can type as a make argument, so `kiro`
# is accepted and written as "". That is the same wire name governance rules use
# (``POLICY_ID_KIRO``), not an invention of this script.

set -u

VENV="${VENV:-.venv}"
PY="$VENV/bin/python"
KIROCREW="$VENV/bin/kirocrew"

requested="${1:-}"
if [ -z "$requested" ]; then
  echo "usage: select-harness.sh <harness>" >&2
  exit 2
fi

if [ ! -x "$PY" ]; then
  echo "  ! $PY not found — run 'make backend' first" >&2
  exit 1
fi

# Resolve the policy name to the value config stores, and refuse anything the
# build cannot serve. One python call so the selectable set is read once, from
# the same registry the loader consults.
resolved="$("$PY" - "$requested" <<'PYEOF'
import sys

from kiro_crew.acp_backends import (
    POLICY_ID_BY_BACKEND,
    POLICY_ID_KIRO,
    selectable_backends,
)

requested = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
selectable = set(selectable_backends())

# Accept either the policy name ("kiro", "claude") or the stored value ("" is not
# typeable, so it only ever arrives as the policy name).
by_policy = {POLICY_ID_BY_BACKEND.get(b, b): b for b in selectable}
if requested == POLICY_ID_KIRO and POLICY_ID_KIRO not in by_policy:
    # kiro is spelled "" and may map to a policy id the loop above did not cover.
    print("OK|")
    raise SystemExit(0)

if requested in by_policy:
    print("OK|" + by_policy[requested])
    raise SystemExit(0)

names = ", ".join(sorted(by_policy)) or "(none registered)"
print("ERR|" + names)
PYEOF
)"

status="${resolved%%|*}"
payload="${resolved#*|}"

if [ "$status" != "OK" ]; then
  echo "  ! '$requested' is not a selectable harness on this build." >&2
  echo "    Available: $payload" >&2
  exit 1
fi

current="$("$KIROCREW" config get agent.acp_backend 2>/dev/null | tr -d '"' | tr -d "'" | tr -d ' ')"
if [ "$current" = "$payload" ]; then
  echo "  ✓ harness already set to '$requested'"
  exit 0
fi

# Persisting is deliberate, and is the difference between this and a run-scoped
# flag: `make start backend=codex` is a user naming the harness they want, and a
# setting that silently reverted on the next plain `make start` would be the
# surprising half. `make use` exists for the same reason without starting a
# gateway.
if "$KIROCREW" config set agent.acp_backend "$payload" >/dev/null 2>&1; then
  echo "  ✓ harness set to '$requested' (agent.acp_backend=\"$payload\")"
else
  echo "  ! could not write agent.acp_backend" >&2
  exit 1
fi
