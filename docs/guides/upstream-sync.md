# Syncing from upstream

This repository is a **fork** of `kirodotdev/KiroCrew` that deliberately diverges
from it. Git can merge the text; it cannot decide whether an upstream change
should replace a fork behavior, sit beside it, or be rejected outright. That
decision is what this runbook is for.

The rules that make the decision are already written down — the "Never
re-introduce" list and the security invariants in
[`AGENTS.md`](../../AGENTS.md), the harness invariants in
[harness-parity](../system-specs/modules/harness-parity.md), and the review rules
in [`AUTOSDE.yaml`](../../AUTOSDE.yaml). What was missing is the procedure that
connects them, and a register of what actually differs.

**The rule that matters most: a merge conflict is not resolved by taking the
newer side.** It is resolved by looking up the area in the divergence register
below and applying the recorded resolution. "Upstream changed it more recently"
is evidence about upstream, not about this fork.

## The procedure

1. **Start clean, in a separate worktree.** Never sync in a tree that has
   uncommitted work: a conflict resolution and an unrelated edit become
   indistinguishable, and `git checkout --ours/--theirs` will silently eat the
   edit. `git status` must be empty before you fetch.
2. **Fetch both remotes.** `git fetch origin && git fetch upstream`.
3. **Branch.** `git checkout -b sync/upstream-<date> origin/main`.
4. **Read the range before merging.** `git log --oneline origin/main..upstream/main`
   and `git diff --stat origin/main...upstream/main`. Account for every commit —
   the omissions here are systematic, not random: a commit whose subject names
   one subsystem while touching a shared surface is exactly what a path scan
   misses.
5. **Classify every change** into one of five buckets, and write the answer down
   in the PR body:
   - **Accept** — no fork interaction.
   - **Adapt** — accept, then reapply the fork behavior on top.
   - **Already diverged** — the fork solved this differently; keep ours.
   - **Reject** — violates a fork invariant. Cite the invariant.
   - **Escalate** — a maintainer decides. Do not guess.
6. **Merge, don't rebase.** `git merge upstream/main`. A merge commit records
   exactly which upstream revision was integrated, which is what makes the *next*
   sync tractable. Never force-push the fork's `main`.
7. **Resolve conflicts against the register**, not against recency.
8. **Run every gate** — the full list is in [AGENTS.md's gate
   section](../../AGENTS.md), plus `scripts/scrub-lint.sh`,
   `scripts/check_harness_parity.py`, `scripts/docs_lint.sh`, the i18n gates and
   the frontend build. A sync touches more surface than a feature PR, so the
   partial run that is fine for a one-file change is not fine here.
9. **Open a PR to `origin/main`.** A sync is reviewed like any other integration.
10. **Record what you learned.** A divergence discovered during the sync goes into
    the register below *and* into its owning spec, in the same PR. Add a test or
    gate wherever one can exist.

**Cherry-picking is the exception, not the routine.** It is right when upstream
cannot be merged whole — a release commit, an unrelated migration. Doing it
habitually means the merge base stops advancing, and every later sync replays
conflicts that were already resolved once.

## Documentation is the weakest form of this contract

An invariant that only exists in prose survives exactly as long as the next
person's attention. Prefer, in this order: **a failing test**, then **a CI gate**,
then **a review rule in `AUTOSDE.yaml`**, then prose. The `Verification` column
below is the point of the table — an empty cell there is a divergence waiting to
be silently reverted.

## The divergence register

Each entry: what must stay true here, what upstream may reassert, and what
protects it.

### ACP harness selection

| Field | Value |
|---|---|
| **Area** | ACP / harness selection |
| **Fork behavior** | Four harnesses are selectable at `agent.acp_backend`: kiro-cli (`""`), `claude`, `codex`, `kas`. `agent.provider` stays `enum=["acp"]`. |
| **Upstream behavior** | Prose and config descriptions that treat kiro-cli as the only harness; a second `agent.provider` value as the way to add one. |
| **Resolution** | **Adapt.** Keep the four. A harness is selected at `acp_backend` and never as a provider value. |
| **Owning files** | `acp_backends.py`, `config/sections.py`, `config/loader.py` |
| **Verification** | `test_harness_parity.py::test_provider_enum_is_acp_only` (H2), `::test_kiro_is_the_default_backend` (H1), `test_agent_backend_editable.py::test_baseline_ships_every_known_backend`, `scripts/check_harness_parity.py` |
| **Rationale** | A second provider value routes around every harness invariant at once. |
| **Sunset** | None. This is the architecture, not a workaround. |

### Kiro is the floor, and adapters adapt

| Field | Value |
|---|---|
| **Area** | ACP / harness parity |
| **Fork behavior** | The Kiro path gains no conditional, argument, or failure mode in service of an adapter (H13). Identity is positive (H5); capabilities are opt-in membership sets (H6). |
| **Upstream behavior** | A refactor that collapses per-harness literals into one shape every harness accepts, or a `not is_<x>_backend` test. |
| **Resolution** | **Reject** the collapse; reapply the per-harness form. Cite the H-number. |
| **Owning files** | `acp/runtime.py`, `acp/client.py`, `sandbox.py`, `providers/acp.py` |
| **Verification** | `scripts/check_harness_parity.py` (added-line gate), `test_harness_parity.py`, the `harness-parity` rule in `AUTOSDE.yaml` |
| **Rationale** | A negation fails toward the permissive answer, so nothing goes red until an operator who never opted into that harness pays for it. |
| **Sunset** | None. |

### Backend-specific sign-in and recovery

| Field | Value |
|---|---|
| **Area** | Authentication / error text |
| **Fork behavior** | Sign-in guidance names the harness that actually failed. `acp_backends.BACKEND_LOGIN_COMMAND` / `BACKEND_AUTH_STATUS_COMMAND` are the single owner; `kiro_prerequisite.KIRO_CLI_LOGIN_COMMAND` derives from them. An unknown harness gets generic guidance, never kiro's command. |
| **Upstream behavior** | A single `kiro-cli login` string shared by every backend. |
| **Resolution** | **Preserve.** Re-thread the backend argument if a merge drops it. |
| **Owning files** | `acp_backends.py`, `acp/client.py`, `acp/session_provider.py`, `providers/acp.py`, `kiro_prerequisite.py` |
| **Verification** | `test_backend_recovery_commands.py` — in particular `test_a_non_kiro_backend_is_never_told_to_run_kiro_cli` and `test_every_known_backend_has_a_login_command` |
| **Rationale** | Naming a binary the operator may not have installed is indistinguishable, to them, from a broken install. |
| **Sunset** | If ACP ever carries a structured auth-recovery field, the table becomes a fallback. |

### Readiness gating is scoped to kiro-cli

| Field | Value |
|---|---|
| **Area** | Dashboard / readiness |
| **Fork behavior** | `reject_if_kiro_unverified` applies only when a selected harness is in `ACP_BACKENDS_KIRO_IDENTITY_STORE`. Fails closed on an unreadable config or an unknown value. |
| **Upstream behavior** | An unconditional gate, on the assumption that every session runs kiro-cli. |
| **Resolution** | **Preserve.** |
| **Owning files** | `dashboard/kiro_readiness.py` |
| **Verification** | `test_kiro_readiness_backend_scope.py` |
| **Rationale** | Every hazard the gate guards is a kiro-cli hazard; on a Claude- or Codex-only install it becomes a permanent 503 for want of a binary nothing spawns. |
| **Sunset** | If `kiro_prerequisite` becomes a per-harness readiness service, this collapses into it. |

### Codex specifics

| Field | Value |
|---|---|
| **Area** | ACP / Codex |
| **Fork behavior** | Codex is an **adapted** ACP backend, selectable, with independent authentication. A codex session carries **no** Crew MCP tools (`_codex_session_mcp_servers` returns `[]`); the claude mirror **is** implemented and does carry them. Backend changes apply to **new sessions only**. |
| **Upstream behavior** | Codex described as a dormant seam, or absent. |
| **Resolution** | **Preserve.** Keep it in `BASELINE_SELECTABLE_BACKENDS`. |
| **Owning files** | `acp_backends.py`, `acp/client.py`, `providers/mirrors/registry.py`, `agent_sdk/backend_install.py` |
| **Verification** | `test_harness_parity.py::test_codex_is_selectable_and_answerable`, `::test_codex_carries_its_own_provider_label`, `::test_codex_spawn_keeps_its_own_branch` |
| **Rationale** | The two halves that gated it (an install probe, and routed tool calls) both landed. |
| **Sunset** | The MCP-tools gap closes when a codex mirror lands beside the claude one. |

### The passive-read limitation

| Field | Value |
|---|---|
| **Area** | Security / Codex |
| **Fork behavior** | ACP v1 gives an adapter no way to request a passive **read**, so the sensitive-path block cannot observe reads a codex session performs. `acp_tool_gate.adapter_hidden_credential_dirs` hides credential homes from the child at the OS boundary. This is stated in the shipped docs, not only in specs. |
| **Upstream behavior** | Silence, or a claim that all tool activity is gated. |
| **Resolution** | **Preserve**, and never upgrade the wording. Containment is not observation. |
| **Owning files** | `acp_tool_gate.py`, `src/kiro_crew/docs/configuration.md`, `src/kiro_crew/docs/troubleshooting.md` |
| **Verification** | `test_fork_divergences.py::test_shipped_docs_disclose_the_codex_passive_read_gap` |
| **Rationale** | A user choosing a harness is choosing a governance model and must be told before, not after. |
| **Sunset** | A protocol version that lets an adapter ask for a read. |

### Claude pre-approval escape

| Field | Value |
|---|---|
| **Area** | Security / Claude Code |
| **Fork behavior** | A tool pre-approved in Claude's own `settings.json` — including one inside a cloned project — never reaches Crew's approval path. Disclosed on the Agent Backend panel and in the shipped docs. |
| **Upstream behavior** | May widen the harness's reach without closing this. |
| **Resolution** | **Escalate** if a change widens it. Do not widen further without closing it. |
| **Owning files** | `acp/client.py`, `providers/mirrors/claude_code.py`, `website/src/pages/developer/AgentBackendTab.tsx` |
| **Verification** | `AgentBackendTab.test.tsx` (`claude_uses_its_own_permissions`), `test_fork_divergences.py` |
| **Rationale** | The guarantee differs per harness; an operator must not discover that from a shell command that never asked. |
| **Sunset** | A `PreToolUse` hook forwarded over ACP, or excluding `project` from `settingSources`. |

### De-Amazoned surface

| Field | Value |
|---|---|
| **Area** | Build / services / auth |
| **Fork behavior** | The "Never re-introduce" list in `AGENTS.md`: Brazil, internal registries, enterprise SSO, internal ticketing, the stubbed modules that keep the import graph whole. |
| **Upstream behavior** | Any of it, reintroduced by a merge. |
| **Resolution** | **Reject.** |
| **Owning files** | `src/`, `website/src/`, `scripts/`, `config/`, `packaging/`, top level |
| **Verification** | `scripts/scrub-lint.sh` |
| **Rationale** | This is a public OSS fork; the internal surface cannot ship. |
| **Sunset** | None. |

### Withdrawn built-in apps

| Field | Value |
|---|---|
| **Area** | Apps |
| **Fork behavior** | The Channels app is hidden from the App Store, the Board app is removed, and the built-in set is **closed**. |
| **Upstream behavior** | Restoring either, or adding a new built-in app. |
| **Resolution** | **Reject.** New apps ship as external apps through the KiroCrewApps registry. |
| **Owning files** | `src/kiro_crew/apps/builtins/` |
| **Verification** | the `no-new-builtin-apps` rule in `AUTOSDE.yaml` |
| **Rationale** | Recorded in [post-launch-removals](../system-specs/post-launch-removals.md). |
| **Sunset** | A maintainer decision to reopen the set. |

### Shipped documentation

| Field | Value |
|---|---|
| **Area** | Docs |
| **Fork behavior** | `src/kiro_crew/docs/` describes four selectable harnesses and each one's sign-in. |
| **Upstream behavior** | Prose asserting kiro-cli is the only provider, or is required. |
| **Resolution** | **Reconcile**, never accept blindly. This is the divergence most likely to be merged without anyone noticing, because prose does not fail a build. |
| **Owning files** | `src/kiro_crew/docs/getting-started.md`, `configuration.md`, `troubleshooting.md` |
| **Verification** | `test_fork_divergences.py` |
| **Rationale** | A doc that contradicts the switch costs the user more than a missing doc. |
| **Sunset** | None. |

## After the merge: the consistency sweep

Prose drifts silently, so check the four layers agree before opening the PR —
`AGENTS.md`, the system specs, the shipped docs in `src/kiro_crew/docs/`, and
the tests. A real example: `harness-parity.md` called Codex "dormant" in its
opening paragraph and counted "three harnesses" while its own later paragraphs
described Codex leaving `NOT_SHIPPED_SELECTABLE`. Nothing failed, because nothing
executes a paragraph. `test_fork_divergences.py` now pins the parts of that which
can be pinned; the rest is this sweep.
