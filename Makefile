# KiroCrew — public build targets (pip + npm/vite + pytest).
# Common flow: `make` runs build (frontend + backend) then tests.
#
# Standalone distribution targets:
#   make wheel     — self-contained pip wheel (dashboard bundled)
#   make backend-bin — standalone backend tree (bundled interpreter, no system Python)
#   make desktop   — double-clickable desktop app (universal DMG on macOS / AppImage on Linux)
#
# Getting a working checkout:
#   make setup     — node + adapters + venv + dashboard + sign-in, then a report
#   make signin    — sign the configured harness in, if it is not already
#
# Running it locally:
#   make start     — stop any running gateway, then run one in this terminal
#   make start backend=codex   — same, after switching harness (also: claude, kiro, kas)
#   make use backend=claude    — switch harness without starting anything
#   make stop      — stop a running gateway
#   make restart   — same as start (stop is already unconditional)
# POSIX only. Windows `make` runs each recipe through cmd.exe, which knows
# neither `export` nor `$(VENV)/bin/...`, so a run from PowerShell fails one
# line at a time with "'NBD' is not recognized" and similar -- a cascade that
# says nothing about the actual cause. Fail once, at the top, with the command
# that works. $(OS) is set to Windows_NT by the Windows build of make and is
# unset under WSL, MSYS and every real POSIX host.
ifeq ($(OS),Windows_NT)
$(error This Makefile is POSIX-only. Run it inside WSL, e.g.: wsl -d Ubuntu -- bash -lc "cd ~/KiroCrew && make $(MAKECMDGOALS)")
endif

.PHONY: all build frontend backend test clean wheel backend-bin desktop \n        start stop restart status token logs setup adapters signin doctor-harness use

PY ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

all: test

# Build the frontend (npm/vite) and stage it into the package, then install
# the backend into a local venv.
build: frontend backend

frontend:
	bash ensure-node.sh || true
	# `cat ... || true`, not `cat ... &&`: on a first run where ensure-node.sh
	# could not record a bin dir (no network, unsupported platform, or node
	# already fine on PATH but the write failed), the marker file is absent and
	# `cat` exits 1. With `&&` chaining that non-zero exit aborts the whole
	# recipe line, so the target fails before npm is ever reached. An absent
	# marker must degrade to "use whatever node is on PATH", not stop the build.
	# website/electron is its own npm package (website/package.json declares no
	# `workspaces`), so the website/ install in this recipe never reaches it --
	# and `npm test` / `npm run check` in website/ then die with MODULE_NOT_FOUND
	# on its missing deps. Install it in the same shell, so it reuses the
	# node-bin-dir PATH handling, and AFTER `npm run build`, so the desktop-only
	# dependency tree cannot block building the dashboard itself (#7226).
	cd website && \
	  NBD="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"; \
	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; }; \
	  if ! command -v npm >/dev/null 2>&1; then \
	    echo "ERROR: npm not found. Install Node >= 18 (see ensure-node.sh) and re-run." >&2; \
	    exit 1; \
	  fi; \
	  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && \
	  npm run build && \
	  ( cd electron && \
	    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi )
	rm -rf src/kiro_crew/static/dist
	mkdir -p src/kiro_crew/static
	cp -R website/dist src/kiro_crew/static/dist

backend:
	bash ensure-python.sh || true
	# Same `|| true` reasoning as the frontend target: an absent marker file must
	# fall back to $(PY), not abort the recipe.
	PY="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/python-bin" 2>/dev/null || true)"; [ -n "$$PY" ] || PY="$(PY)"; \
	  if [ -x $(VENV)/bin/python ] && ! $(VENV)/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then \
	    echo "  → recreating $(VENV) (existing interpreter < 3.12)"; rm -rf $(VENV); fi; \
	  if ! "$$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then \
	    echo "ERROR: '$$PY' is not Python >= 3.12 (package requires-python is >=3.12)." >&2; \
	    echo "       Without this gate the venv is built from a too-old interpreter, the" >&2; \
	    echo "       version guard above deletes it on every run, and the install either" >&2; \
	    echo "       backtracks forever or crashes at import. Provision 3.12+ first:" >&2; \
	    echo "         bash ensure-python.sh   # or: make backend PY=python3.12" >&2; \
	    exit 1; \
	  fi; \
	  test -x $(VENV)/bin/python || "$$PY" -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	# --prefer-binary: on hosts below the modern manylinux baseline (e.g. Amazon
	# Linux 2, glibc 2.26) the newest release of a compiled dep may ship only a
	# manylinux_2_28 wheel + an sdist. Without this flag pip picks the newest
	# version and builds the sdist from source, which fails (no toolchain / old
	# GCC / missing -dev headers). --prefer-binary makes pip take an older
	# prebuilt wheel instead. No-op where the newest deps already have a usable
	# wheel (macOS, AL2023).
	KIROCREW_SKIP_FRONTEND=1 $(PIP) install --prefer-binary -e ".[dev]"
	# CI parity: also install the PEP 735 dev dependency-group (pins
	# jsonschema so the config-validation guard tests actually run).
	$(PIP) install --group dev
	bash packaging/resign-macos-libs.sh $(VENV)/bin/python

test: build
	$(PYTEST) -q

# --- Standalone distribution -------------------------------------------------

# Self-contained pip wheel: builds + stages the dashboard, then produces a
# wheel that bundles the SPA (see setup.py BuildWithFrontend + MANIFEST.in).
#
# Runs through the venv the `backend` target provisions rather than a bare
# `$(PY) -m pip install --upgrade build`: on hosts whose system python3 is older
# than 3.12 (Amazon Linux 2023 ships 3.9) that bare form installs `build` into
# the *system* interpreter — mutating it without a venv, and tripping PEP 668
# "externally-managed-environment" where the marker exists. Depending on
# `backend` guarantees a >= 3.12 venv exists first.
wheel: frontend backend
	$(PIP) install --upgrade build
	$(VENV)/bin/python -m build --wheel

# Standalone backend tree on a bundled python-build-standalone interpreter (no
# system Python needed). Stages the dashboard first so it's embedded in the
# bundle. Host-arch only (UNIVERSAL=0): the standalone backend is a
# local-machine artifact, not a distributable app.
backend-bin: frontend
	UNIVERSAL=0 SKIP_FRONTEND=1 SKIP_ELECTRON=1 bash packaging/build-desktop.sh

# Full double-clickable desktop app. macOS: ONE universal DMG (arm64 + x86_64,
# needs an Apple-Silicon host with Rosetta 2 — see docs/build/desktop-app.md;
# UNIVERSAL=0 for a faster host-arch-only build). Linux: AppImage (host arch).
#
# build-desktop.sh runs `npm ci` / `npm run build` itself, so it needs node on
# PATH. It provisions its own uv + PBS interpreter but NOT node, so bootstrap
# node here — otherwise a first `make desktop` on a node-less host dies at the
# script's npm step instead of installing it like every other target does.
desktop:
	bash ensure-node.sh || true
	NBD="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"; \
	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; }; \
	  bash packaging/build-desktop.sh

# ── One-shot local setup ──
#
# Gets this checkout to the point where a session can actually RUN, which is a
# larger claim than "it builds": a non-kiro harness is driven through an ACP
# adapter that Crew SPAWNS, so an install with a built dashboard and no adapter
# serves the UI and then fails every turn at spawn. `build` covers the first
# half, `adapters` the second, and the report at the end names whatever is still
# missing rather than leaving it to the first failed turn.
#
# It deliberately does NOT write agent.acp_backend. Which harness you drive is a
# choice, and a setup target that quietly rewrote it would surprise anyone
# re-running this on a configured install; the report prints the command instead.

# Binary → npm package. The binary is what the resolution ladder looks for, and
# a global install of the scoped package is what puts the unscoped binary on
# PATH, so these agree by construction rather than by coincidence.
ADAPTER_SPECS := claude-agent-acp=@agentclientprotocol/claude-agent-acp                  codex-acp=@agentclientprotocol/codex-acp                  claude=@anthropic-ai/claude-code codex=@openai/codex

setup: build adapters signin doctor-harness

# Install only what is absent. `npm i -g` on an already-installed package is a
# network round trip and a rebuild, and this target is meant to be re-runnable
# on an established checkout without paying for three of them.
adapters:
	@$(NODE_ON_PATH); $(REQUIRE_NATIVE_NODE); 	  for spec in $(ADAPTER_SPECS); do 	    bin="$${spec%%=*}"; pkg="$${spec#*=}"; 	    if command -v "$$bin" >/dev/null 2>&1; then 	      echo "  ✓ $$bin already installed"; 	    else 	      echo "  → installing $$pkg (provides $$bin)"; 	      npm i -g "$$pkg" --no-audit --no-fund >/dev/null || 	        echo "  ! could not install $$pkg — $$bin stays unavailable" >&2; 	    fi; 	  done

# Sign the configured harness in. See scripts/harness-signin.sh for why this is
# only the CONFIGURED one, why a non-interactive run reports instead of blocking,
# and why it never fails the build.
signin:
	@$(NODE_ON_PATH); echo ""; echo "── sign-in ──"; 	  VENV=$(VENV) bash scripts/harness-signin.sh

# What this host can actually serve, and the two things that are not a binary.
doctor-harness:
	@$(NODE_ON_PATH); echo ""; echo "── harness readiness ──"; 	  $(VENV)/bin/python -c "from kiro_crew.agent_sdk import probe_backends; [print(f'  {s.policy_id:8} {s.installed:10}' + (f'  missing: {\", \".join(s.missing_components)}  →  {s.install_command}' if s.missing_components else '')) for s in probe_backends()]"
	@echo ""; echo "── this checkout's Claude permission surface ──"; 	  if [ -f .claude/settings.local.json ]; then 	    echo "  ! .claude/settings.local.json exists and Crew did not author it."; 	    echo "    Crew's seed is create-or-decline, so it governs nothing here and"; 	    echo "    WITHHOLDS its whole MCP array — sessions in this directory get no"; 	    echo "    Crew tools. Rename that file to restore them."; 	  else 	    echo "  ✓ no foreign settings.local.json — Crew seeds it and mounts its MCP tools"; 	  fi
	@echo ""; echo "── sign-in ──"; 	  echo "  Installed is NOT signed in, and the two fail at different times: a missing"; 	  echo "  binary is caught here, a missing credential only at the first turn. These"; 	  echo "  are the commands, not a measurement — Crew deliberately does not read a"; 	  echo "  harness's credential store, because a wrong negative would gate a user who"; 	  echo "  is already signed in through an ambient key or a relocated home."; 	  $(VENV)/bin/python -c "from kiro_crew.acp_backends import login_command_for, ACP_BACKENDS_KNOWN, POLICY_ID_BY_BACKEND; [print(f'    {POLICY_ID_BY_BACKEND.get(b, b):8} {login_command_for(b) or \"(no known command)\"}') for b in sorted(ACP_BACKENDS_KNOWN, key=lambda x: POLICY_ID_BY_BACKEND.get(x, x))]"
	@echo ""; echo "── select a harness ──"; 	  echo "  $(KIROCREW) config set agent.acp_backend claude   # or: codex, kas, '' (kiro-cli)"; 	  echo "  current: $$($(KIROCREW) config get agent.acp_backend 2>/dev/null || echo '(unreadable)')"; 	  echo ""

# ── Gateway lifetime ──
#
# `kirocrew gateway` runs in the FOREGROUND and stops on Ctrl-C, which is the
# right shape for development: the log is in front of you and the process dies
# with the terminal. What it does not do is notice an already-running gateway.
# A second one loses the race for the port and dies at bind, and the message it
# prints is about the port rather than about the gateway already up, so the
# usual fix is to hunt a PID. `start` therefore depends on `stop` and `restart`
# is just an alias -- there is no start path here that can hit an occupied port.
#
# The stop is best-effort by design. "nothing was running" is the normal case on
# a first run, not a failure, so a non-zero exit must not abort the build.
KIROCREW := $(VENV)/bin/kirocrew

# The same node-bin-dir marker `frontend` reads, for a different reason: the ACP
# adapters (claude-agent-acp, codex-acp) are node programs the gateway SPAWNS, so
# node has to be on PATH for a session to start at all, not merely to build the
# dashboard. Without it the gateway comes up, serves the UI, and every turn dies
# at spawn -- a failure that looks like a harness problem and is not.
NODE_ON_PATH = NBD="$$(cat "$${KIROCREW_HOME:-$$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"; 	  { [ -z "$$NBD" ] || export PATH="$$NBD:$$PATH"; }

# Refuse a Node reached over WSL interop, for the targets that spawn or install
# with it.
#
# WSL appends the WHOLE Windows PATH by default (appendWindowsPath), and the
# stock Ubuntu .bashrc returns early for a non-interactive shell -- so nvm, which
# it loads a hundred lines further down, never runs under `bash -c` or `make`.
# The result is a Linux shell with no Linux node and ~59 Windows PATH entries, so
# `npm` resolves to /mnt/c/.../npm. Nothing errors: `npm i -g` installs into the
# WINDOWS profile, and the adapter the gateway then spawns is a .exe shim the
# Linux sandbox cannot execute. It surfaces much later as an authentication or
# spawn failure that says nothing about PATH.
#
# The remedy is the marker above, which ensure-node.sh writes. Fail here rather
# than silently do the right thing in the wrong operating system.
REQUIRE_NATIVE_NODE = _npm="$$(command -v npm 2>/dev/null || true)"; case "$$_npm" in /mnt/*) echo "ERROR: npm resolves to $$_npm - a Windows binary reached over WSL interop." >&2; echo "       It writes to the Windows profile and yields adapters the Linux sandbox cannot run." >&2; echo "       Fix: bash ensure-node.sh   (records the native node for make)" >&2; exit 1;; "") echo "ERROR: no npm on PATH. Fix: bash ensure-node.sh" >&2; exit 1;; esac

stop:
	@$(NODE_ON_PATH); $(KIROCREW) stop 2>/dev/null || echo "  → no gateway was running"

# Which harness to drive, named the way a governance rule names it: `codex`,
# `claude`, `kas`, or `kiro`. Empty leaves the configured one alone, so a bare
# `make start` never rewrites config.
#
# `agent=` is accepted as an alias because it is the word people reach for, but
# `backend=` is the name to prefer: this repo already uses "agent" for a Crew
# agent definition (`kirocrew agent`, `--agent`, `agent.default`), which is a
# different thing that is also selected per session.
BACKEND := $(if $(backend),$(backend),$(agent))

# Switch harness without starting anything.
use:
	@$(NODE_ON_PATH); 	  if [ -z "$(BACKEND)" ]; then 	    echo "usage: make use backend=<codex|claude|kiro|kas>" >&2; exit 2; 	  fi; 	  VENV=$(VENV) bash scripts/select-harness.sh "$(BACKEND)"

start: stop
	@$(NODE_ON_PATH); $(REQUIRE_NATIVE_NODE); 	  if [ -n "$(BACKEND)" ]; then 	    VENV=$(VENV) bash scripts/select-harness.sh "$(BACKEND)" || exit 1; 	  fi; 	  exec $(KIROCREW) gateway

restart: start

status:
	@$(NODE_ON_PATH); $(KIROCREW) status

# Prints a dashboard URL. The token in it is a ~5 minute bootstrap credential
# that the page trades for a ~20h session cookie, so open it promptly; a stale
# one is what the red "Session expired" banner is reporting.
token:
	@$(KIROCREW) token

logs:
	@$(KIROCREW) logs

clean:
	rm -rf build dist *.egg-info src/*.egg-info \
	       src/kiro_crew/static/dist website/dist \
	       website/electron/backend-dist website/electron/dist \
	       .pytest_cache .mypy_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
