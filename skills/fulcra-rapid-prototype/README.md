# fulcra-rapid-prototype

`fulcra-rapid-prototype` scaffolds two complementary pieces for a new Fulcra
project:

1. **Runtime harness** — the generated project's inner model → tool →
   feedback loop, sandboxed tools, provider adapters, prompts, and app
   skeleton.
2. **Control harness** — the separate outer project loop that owns
   milestones, independent Generator/Evaluator work, Workspace state,
   decisions, branch/PR gates, and recovery.

Use `SKILL.md` as the operational flow. It composes
`fulcra-prototype-grill-me`, `fulcra-workspaces`, the runtime scaffold, the
control harness, and `fulcra-harness-dashboard`; this README is a compact
reference for the bundled assets.

## Quick path

```text
Grill-Me Intake/Interview + Step 1b Workspace
  → Grill-Me Architecture/Plan
  → scripts/scaffold.py --dry-run
  → generated project
  → scripts/scaffold_control_harness.py --dry-run
  → Workspace bootstrap + doctor
  → harness dashboard
  → runtime verification
  → milestone operation through control harness
```

The control plane must exist before running an inner `harness.run_task` task.
The same `team/prototype-<project>/` Workspace tracks Grill-Me, control
harness, runtime progress, decisions, verdicts, and dashboard state.

## Scaffold the runtime project

After approved Grill-Me artifacts exist:

```bash
python scripts/scaffold.py \
  --project-name "My Project" \
  --rapid-prototype-dir /path/to/grill-me-project \
  --output-dir /path/to/new-project \
  --dry-run
```

Review the dry-run manifest, then rerun without `--dry-run`. Use
`--history=auto` (default) to preserve Grill-Me git history when the source
is a git working tree. Use `--history=preserve` only when lack of history is
a hard error; use `--history=copy` to flatten intentionally.

## Verify the generated runtime

Follow the generated project README and `.env.example`. Configure a provider
only when runtime verification begins; prefer already-authenticated
Claude OAuth or Gemini ADC where available, then fall back to provider API
keys. If `python -m venv` lacks `ensurepip`, use:

```bash
uv venv --clear .venv
uv pip install --python .venv/bin/python -e .
```

Run the generated smoke tests before declaring the runtime ready. The exact
commands live in `SKILL.md` and the generated README.

## Bundle layout

```text
SKILL.md                     orchestration flow
engine/                      provider-agnostic runtime harness copied to projects
templates/                   project-specific hydrated files
scripts/scaffold.py          runtime project scaffolder
control_harness_templates/   portable outer control-harness templates
scripts/scaffold_control_harness.py
                             outer control-harness scaffolder
scripts/tests/               executable scaffold regression tests
```

`engine/` is copied verbatim into generated `harness/`; use prompts, app
context, or explicit tools for project-specific behavior rather than editing
it casually. `templates/` are the small set of files hydrated per project.

## Provider adapters

The runtime supports Gemini, Anthropic, and OpenAI through isolated adapters
in `engine/providers/`. The shared loop uses normalized messages/tool calls;
provider-specific credential, message, and tool-result translation stays in
its adapter. See each adapter module and generated `.env.example` for current
provider/auth details.

## Continuity and recovery

- Grill-Me phase history is preserved in the generated project when possible.
- The generated git tool makes best-effort Fulcra bundles after commits;
  see `engine/tools/git_tool.py` for exact behavior.
- The outer control harness owns durable Workspace status, decisions,
  milestones, evaluator records, recovery, and dashboard publication.
- Never discard interrupted source work: resume it on the current milestone
  branch or preserve unrelated work in a named stash with durable handoff.

## Verification and caveats

Run the shipped pytest suites after changing scaffold/runtime code:

```bash
uvx --with pytest pytest scripts/tests/
```

Provider selection and multi-tool-call translation have dedicated smoke
modules under `engine/providers/`. Live provider calls require real user
credentials; the Anthropic OAuth live call remains intentionally documented
as requiring a real subscription/session rather than being claimed as
universally verified.

For exact provider edge cases, tool ID handling, git history behavior, or
bytecode-cache caveats, read the owning module/test rather than duplicating
long implementation narratives here.
