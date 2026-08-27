---
name: fulcra-rapid-prototype
description: "Scaffolds a running, project-specific Fulcra agent harness (control loop + sandboxed tools + provider adapter), then operates it with an inspectable task-specific iteration discipline: immutable user-approved specs, separate generation/evaluation, milestone-sized loops, durable Fulcra Workspace state, bounded liveness/retry, and reviewable git/PR gates. Depends on fulcra-prototype-grill-me for requirements gathering."
author: schr3b3r
version: 1.1.0
metadata:
  tags: [fulcra, agent-harness, scaffolding, meta, rapid-prototype, evaluation, milestones]
---

# Fulcra Rapid Prototype

This skill turns a project idea into a **running, project-specific agent
harness** — a small, hand-rolled control loop (model call -> tool
dispatch -> feedback -> repeat) with sandboxed file/git/shell tools, a
system prompt describing the specific project, and a first real task
prompt to run against it.

It has two complementary responsibilities:

1. **Scaffold the runtime harness.** The bundled `engine/`, `scripts/`,
   and `templates/` create an inspectable reference harness with a provider
   adapter and sandboxed tools. This is upstream's concrete starter
   implementation, not hypothetical documentation.
2. **Operate/evolve it reliably.** Once a project is scaffolded, use the
   task-specific iteration discipline in this document so the harness
   builds work in small verifiable increments instead of making one large,
   unreviewed model attempt.

It does not directly build the user's whole project in one opaque session;
it builds and governs the thing that will build the project.

This exists as a reference implementation of building on Fulcra with an
agent harness — see
[fulcra-for-agents.md](https://github.com/kubla/fulcra-for-agents/blob/main/fulcra-for-agents.md)
for the architectural patterns (Context-Compute Separation, Derived
Context, Resumable Discovery, etc.) this skill's generated
`ENGINEERING_STANDARDS.md` encodes as concrete rules.

## When to use this skill

Trigger this when the user explicitly wants to:

- Build a new application/tool on top of Fulcra using a **custom agent
  harness** they control and can inspect/modify (not just "have Claude
  Code build it directly").
- Learn agent-harness engineering fundamentals by having a real, minimal
  reference implementation to study and extend.
- Run a project-specific generation/evaluation loop with durable state,
  reviewable artifacts, and an escalation path.

Do NOT use this for quick one-off scripts, tasks where the user just wants
you (Claude Code) to build something directly without a separate harness
layer, or projects with no Fulcra involvement at all.

## Prerequisites

- The `fulcra-prototype-grill-me` skill must be available. This skill
  depends on it for Intake/Interview/Architecture/Plan; it does not
  duplicate that requirements-gathering logic.
- Provider credentials will be needed eventually to actually run the
  harness (Anthropic, Gemini, or OpenAI — see
  `engine/providers/__init__.py`'s module docstring for the full
  auto-selection order). **Do not ask for them yet.** Intake, Interview,
  Architecture, Plan, dry-run scaffolding, and static artifact review do
  not need them. Ask only at the first point the generated runtime harness
  is actually verified/run — and even then, prefer OAuth over an API key:
  if the user already has a Claude subscription, `claude setup-token`
  needs no separate API key at all; similarly `gcloud auth
  application-default login` covers Gemini via Vertex AI. Only fall back
  to asking for a raw `GEMINI_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`
  if neither OAuth path is available. The adapter set is an extension
  point, not a permanent provider limitation.
- For multi-agent evaluation or a different provider, adapt the provider
  invocation layer while retaining the operational contract below. Do not
  make project specs or durable coordination depend on one CLI/provider.

# Part I — Scaffold the Runtime Harness

## The flow (follow these steps in order)

### 1. Run fulcra-prototype-grill-me through its Plan phase

Load `fulcra-prototype-grill-me` and run its Intake -> Interview ->
Architecture -> Plan phases with the user, exactly as that skill specifies
(including its Architecture user gate — do not skip it). Stop before that
skill's own Prototype/Build phases: this skill's generated harness replaces
those phases for a project that wants a custom harness.

By the end, the rapid-prototype project git repo should have:

```text
intake/brief.md
architecture.md   (approved by the user)
plan.md
```

If upstream Grill-Me creates any additional interview artifact, retain it.
If the user already has recent approved artifacts, confirm they remain
current rather than rerunning discovery from scratch.

### 2. Confirm the bundled scaffold is available

This skill's scaffolding logic ships beside this `SKILL.md`, in `scripts/`,
`engine/`, and `templates/`. Confirm `scripts/scaffold.py` is present
relative to the skill directory before proceeding; do not rely on a sibling
clone existing at a guessed path.

### 3. Decide where the new project will live

Ask the user where the new project's own repo/directory should be created.
Normally use a new sibling directory, **not** the skill directory (the skill
stays a reusable template, not a place real projects accumulate).

### 4. Run the scaffold script

```bash
cd <this skill's own directory>
python scripts/scaffold.py \
  --project-name "<Human-readable project name>" \
  --rapid-prototype-dir <path to repo with intake/, architecture.md, plan.md> \
  --output-dir <path to new project directory> \
  --domain-library-guidance "- For X: use library Y, not a hand-rolled Z."
```

Run with `--dry-run` first and show the user what would be written before
the real run.

`--domain-library-guidance` is optional but useful when Architecture
surfaced an obvious domain (for example, audio processing or a web backend).
If omitted, the generated `ENGINEERING_STANDARDS.md` contains a visible
TODO; that is acceptable when no sensible library choice exists yet.

**Git history:** default `--history=auto` preserves
`fulcra-prototype-grill-me`'s phase history when `--rapid-prototype-dir` is
a git working tree. That lets future sessions inspect genuine
Intake/Architecture/Plan decisions via `git log` rather than a flattened
snapshot. A bundle can be unpacked first with `git clone <bundle> <dir>`.
If the source is not a git repo, auto falls back to copy mode; use
`--history=preserve` only when lack of history should be a hard error.

History-preserving mode clones directly into `--output-dir`, so that path
must not exist at all. Pick a fresh target or remove/rename the old target
before running it.

### 5. Verify the scaffold actually works before handoff

Do **not** claim success merely because scaffolding exits 0. At the first
actual runtime verification point, get the user authenticated to a
provider and write the result into the new project's `.env`. Prefer
OAuth over an API key: if the user already has a Claude subscription,
`claude setup-token` needs no separate API key; `gcloud auth
application-default login` similarly covers Gemini via Vertex AI. Only
fall back to asking for a raw API key
(`GEMINI_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) if neither OAuth
path applies — see `.env.example` and `engine/providers/__init__.py`'s
module docstring for the full menu and auto-selection order. Then run:

```bash
cd <new project dir>
git log --oneline
# commit generated scaffold if the script did not preserve/create the repo
# history itself
python -m venv .venv && .venv/bin/pip install -e .
# If venv creation fails with an ensurepip/venv error ("The virtual
# environment was not created successfully because ensurepip is not
# available" -- common on minimal/PEP 668-style environments), use uv
# instead; it needs no ensurepip and produces an equivalent .venv/:
#   uv venv --clear .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env
.venv/bin/python -m harness.test_loop_smoke
.venv/bin/python -m harness.test_context_smoke
.venv/bin/python -m harness.tools.test_filesystem_smoke
.venv/bin/python -m harness.tools.test_git_smoke
.venv/bin/python -m harness.tools.test_git_commit_gate_smoke
.venv/bin/python -m harness.tools.test_run_command_smoke
```

All six must pass. If any fails, fix the scaffold before saying it is ready.
A known fast-successive-write bytecode-cache artifact in the git-commit-gate
smoke test may be resolved by clearing `app/**/__pycache__` and running that
test once again; do not use this as an excuse for unrelated failures.

### 6. Hand off the verified scaffold

Point the user at:

- generated `harness/prompts/system_prompt.md` and
  `harness/prompts/task_001_*.md` — heuristic starting points to inspect,
  not guaranteed finished prompts;
- the generated project `README.md` for getting started;
- `app/CONTEXT.md` and `app/features/` for runtime context/progress.

Then either run the first task (`python -m harness.run_task task_001_*.md`)
if the user wants a demonstration, or hand the verified scaffold over.

### 7. Scaffold the portable outer control harness

The bundled runtime harness is the inner model/tool loop. If the project
will use independent generation/evaluation, milestone branches, durable
Workspace tracking, or unattended operation, scaffold the separate outer
control harness now:

```bash
cd <this skill's own directory>
python scripts/scaffold_control_harness.py \
  --project-name "<Human-readable project name>" \
  --output-dir <new sibling path such as ../<project>-control>
```

Run with `--dry-run` first. The script refuses to overwrite a non-empty
control harness. Fill its `spec.md`/`milestones.md` from the approved
Grill-Me artifacts, then follow its README to configure provider adapters
and bootstrap a Fulcra Workspace team. Keep it separate from the generated
deliverable repo: the control harness governs work; the deliverable is the
artifact being evaluated.

### 8. Set up the harness dashboard (recommended; public deployment optional)

Once the control harness has a Workspace team, invoke
**`fulcra-harness-dashboard`** as the normal manager/operator visibility
step. It builds on **`fulcra-project-dashboard`** and renders the
actionable harness state that a project dashboard alone does not make
explicit: milestone flight plan, run evidence timeline, current checkpoint,
decision requests, escalations, and next bearing.

Dashboard setup is recommended because durable status is part of the
control-harness contract and an operator needs to see it across sessions.
The normal flow is to publish the curated dashboard to an **unguessable
URL** (Surge is the preferred simple default) so all authorized players or
operators can open the same view. This is not access control: make clear to
the user that the dashboard is publicly reachable by anyone who has the
URL. Before deploying, print the exact isolated `public/` manifest, confirm
it contains only intended curated files, add `noindex,nofollow`, and obtain
explicit user confirmation. Follow `fulcra-harness-dashboard` for the
refresh/publish adapter; never publish raw inboxes, full verdict archives,
credentials, or private repository data.

# Part II — Operate the Scaffold as a Reliable Task Harness

The bundled runtime loop may be single-agent. That does **not** remove the
need for independent project-level evaluation. Treat its output as a
Generator artifact and run a separate Evaluator session/process against it.

## Universal operational invariants

1. **Separate Generator from Evaluator.** Generator builds the current
   artifact; Evaluator independently tests/grades it. Do not use one
   session switching personas as a substitute for independent evaluation.
2. **Immutable approved spec during a run.** Requirements change only after
   a user decision. Do not patch output to make a verdict disappear.
3. **Milestones, not whole-spec attempts.** Build exactly one bounded,
   independently gradeable work item per Coordinator invocation. Supply
   the whole spec for invariants, but scope actual work narrowly.
4. **Durable state is not chat memory.** Fulcra Workspace files record
   status, milestone state, requests, verdicts, escalations, and summaries.
5. **Bounded retry/liveness.** Manual mode should normally attempt once and
   return control. Unattended mode may retry within a visible bound. Watch
   activity (file/output changes) plus a hard wall clock; never let a
   process appear to run forever.
6. **Fix the process, not one output.** Repeated failures modify prompts,
   schemas, permissions, provider adapters, milestone scope, or evaluation
   logic — not just a single generated file.
7. **Git is the review/audit trail.** Keep the portable control harness and
   deliverable/project in separate repositories. Use milestone branches and
   PRs; merge only after evaluator approval.

## Required portable control-harness contract

Alongside the scaffolded project, maintain a portable control-harness repo
(or a clearly separated `prototype-control/` directory) containing:

```text
spec.md                    # approved requirements; immutable during a run
decisions.md               # append-only raw user decisions
knowledge/                 # earned domain/process findings
roles/manifest.md          # active roles, not hardcoded pair names
roles/generator.md
roles/evaluator.md
schemas/message.md
schemas/verdict.md
schemas/decision-request.md
coordinator/milestones.md
coordinator/policy.md
coordinator/unattended-recovery.md
RUNBOOK.md
HARNESS_GOVERNANCE.md
bootstrap.sh
doctor.sh
```

Keep three boundaries distinct:

- **Control harness:** portable process/spec/roles/schemas/knowledge; no
  execution history or deliverable code.
- **Fulcra Workspace team:** durable run state, inboxes, progress, verdicts,
  decisions, escalations, session summaries, dashboard source data.
- **Deliverable:** generated project/runtime-harness/app code in its own git
  repo and PR history.

A fresh `bootstrap.sh <team>` must provision a new Workspace from only the
control-harness contents. If it needs old inboxes or local memory, the
harness is not portable.

## Decision and governance contract

- `decisions.md` preserves chronological raw user answers; `spec.md` is the
  formalized target derived from those decisions.
- Only the user may change `spec.md` or `decisions.md`.
- A distinct Harness Maintainer may automatically append evidence-backed
  knowledge, improve milestone decomposition, and fix control-harness
  mechanics. It must never edit deliverable code or user requirements.
- A role that discovers a user-only judgment emits a structured
  `decision_request: true` record (one question, context, options,
  priority). Coordinator persists it under `team/<team>/decision/`, writes
  `DECISION REQUIRED` status/dashboard state, reports it to the origin, and
  pauses blocking work. Never bury such a request in prose or auto-retry it.

## Milestone execution and evaluation

1. Define ordered milestones with scope, requirement IDs, explicit done
   criteria, dependencies, and intentionally-out-of-scope later work.
2. Coordinator creates/resumes a `milestone/<id>-<slug>` deliverable branch.
   Generator receives the narrowest required tool permissions (for example,
   git stage/commit/push and declared test runner), commits/pushes only that
   branch, and never merges.
3. GitHub cannot create a zero-diff PR. After Generator's first pushed
   commit, Coordinator creates/resumes the PR before Evaluator runs.
4. Evaluator grades committed branch state, not an uncommitted worktree. It
   emits exact `overall: PASS` or `overall: FAIL`.
5. If a declared test runner exists, Evaluator must execute it independently
   and emit exact `test_runner: PASS` or `test_runner: FAIL` with command/
   count evidence. A permission block is a FAIL/escalation, never a reason
   to replace executable testing with code reading.
6. `UNTESTABLE` must distinguish later-milestone scope (document when it
   will be tested) from genuine in-scope ambiguity (decision request and
   halt). Only an all-in-scope PASS plus passing executable evidence may
   merge the PR.
7. On PASS, merge, update `milestone-progress.md`, and write a concise
   `status-summary.md`: where we are, where we are going, next bearing.

## Safe recovery and unattended operation

Before any branch switch, remove only documented ephemeral test caches
(e.g. Python `__pycache__/`). Never broadly clean untracked files.

- Meaningful dirty work on the exact current milestone branch is resumed in
  place so Generator can inspect/test/commit it.
- Meaningful work on another branch is preserved in a named `git stash -u`
  plus Workspace handoff record before checkout/reset.
- Never discard source work, force-push, or let a verifier directly repair
  deliverable code/tests.

For unattended work, pair the Coordinator with a delayed verifier on the
same cadence. The verifier checks scheduler history, Workspace state,
branch/PR state, and dashboard evidence — not chat history or a scheduler
"completed" flag alone. It may repair control-harness/branch/worktree state
through the safe recovery protocol, then prove one clean Coordinator
preflight. Provider/session/rate limits are transient capacity escalations:
retain their evidence but allow a later scheduled retry after reset; keep
real spec/decision/state blockers paused.

Durable status, decision, milestone-progress, and escalation uploads are
required records. Retry them boundedly and surface a critical visibility
failure; never silently claim dashboard/status is current after a failed
upload.

## Optional dashboard

A dashboard is outside the portable control harness by default. If a
project wants a visual harness view, invoke **`fulcra-harness-dashboard`**
after the Workspace tracking contract exists. That skill is an adapter on
top of **`fulcra-project-dashboard`**: it supplies the harness-specific
flight plan, run timeline, checkpoint, Open Items, refresh contract, and
safe-publication adaptations without replacing the base dashboard shell.

Read only durable Workspace summaries/progress, publish only explicitly
curated data, never raw inboxes/verdict archives/credentials/private repo
contents, and make deployment history reviewable. A dashboard publish
failure must not mask the actual harness result.

## What this skill deliberately does NOT do

- It does not duplicate requirements gathering; that remains
  `fulcra-prototype-grill-me`.
- It does not treat the bundled Gemini adapter as the only provider choice.
- It does not turn a single runtime-agent loop into a claim of independently
  evaluated work; use the operational Generator/Evaluator contract above.
- It does not permit unattended verifier/maintainer roles to edit
  deliverable code/tests or user-owned requirements directly.
- It does not use a general-purpose templating engine for every scaffold
  file; `scripts/scaffold.py` intentionally uses plain substitution.
