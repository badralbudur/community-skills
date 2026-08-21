---
type: Reference
title: fulcra-vault
description: A shared, append-only markdown knowledge vault stored in Fulcra Files, establishing durable context for humans and agents.
resource: https://github.com/ashfulcra/fulcra-tools/tree/main/packages/fulcra-vault
tags: [memory, coordination, fulcra, vault, python]
status: active
---

# fulcra-vault

`fulcra-vault` is a shared markdown knowledge vault stored in Fulcra Files — one durable place for humans and agents to record what matters: projects, people, decisions, corrections, domain notes, and links between them. It establishes user-owned context beyond data streams, accessible to multiple independent agents.

The vault relies on ordinary markdown files stored under `vault/`. Notes are compatible with Obsidian-style `[[wikilinks]]`, flat Dataview-friendly frontmatter, owned sections for agent edits, and append-only logs.

## Vault Layout

A vault lives under `/vault` in Fulcra Files. It uses the following structure:

| File / Directory | Purpose |
|------------------|---------|
| `meta.json` | Stores the structure specification and exclusions (write-protected paths). |
| `MAP.md` | A structured, deterministic index of all notes. |
| `HOT.md` | A compact session-start summary, often injected at agent startup. |
| `LOG.md` | The vault-level audit trail for every mutation. |
| `.index/links.json` | Caches wikilink connections, backlink indexes, and rename planning. |
| `.locks/<note>.md.lock`| Advisory per-note locks coordinating agent writes. |
| `*.md` | The actual knowledge concepts and notes (e.g., `Project Alpha.md`). |

## Features Implemented

The package provides a rich set of primitives and commands:

- **Structure Validation**: Validates first-run vault specs and normalizes paths to Fulcra absolute paths.
- **Section Parsing**: Enables owned-section parsing and safe section replacement.
- **Frontmatter**: Flat frontmatter parsing and stable mutation.
- **Links**: Wikilink extraction, backlink indexing, and safe `--force`-gated `rename`/`delete` (locks touched notes, rewrites inbound links).
- **Hooks**: A platform hook installer (`install-hooks`) injecting `HOT.md` at session start for `claude-code` and `codex`. Surgical, reversible (`--uninstall`).
- **Data Transport**: Fulcra Files text store wrapper with advisory per-note locking.
- **Agent Skill**: A packaged agent skill (`skill/SKILL.md` plus references) routing agents by CLI, raw HTTP, or MCP read-only capability.

*Note: Sync operations (vault sync / local mirror) are planned work.*

## Note Structure

Each note is an OKF-compatible document featuring flat frontmatter, standard markdown body text, owned sections for safe agent writes, and a per-note append-only log.

### Example Note

```markdown
---
section: projects
status: seed
title: Project Alpha
updated_at: 2026-06-12T12:00:00+00:00
---
# Project Alpha

<!-- section:projects owner:fulcra-vault -->
Seed note. Replace this with durable context.
<!-- /section:projects -->

## Log
- 2026-06-12T12:00:00+00:00 fulcra-vault: created seed note
```

*Owned sections allow independent agents to update their allocated regions without corrupting unrelated content. The `## Log` is purely append-only.*

## CLI Examples

| Action | Command |
|--------|---------|
| **Read note** | `uv run fulcra-vault read "Project Alpha"` |
| **Read note (with backlinks)** | `uv run fulcra-vault read "Project Alpha" --with-backlinks` |
| **Rewrite section** | `printf 'New context.\n' | uv run fulcra-vault write-section "Project Alpha" --section projects --agent codex-prefs --force` |
| **Append to log** | `uv run fulcra-vault append-log "Project Alpha" --entry "State captured" --agent codex-prefs` |
| **Rebuild link index** | `uv run fulcra-vault reindex --agent codex-prefs` |
| **Render MAP/HOT** | `uv run fulcra-vault map --agent codex-prefs` |
| **Check map (dry-run)** | `uv run fulcra-vault map --check` |

## Safety Model

`fulcra-vault` guarantees integrity and safe mutation through inspectable rules:

1. **Markdown is the source of truth**: Derived files are fully rebuildable.
2. **Pre/Post-Validation**: CLI writes validate frontmatter before and after mutation.
3. **Write Protection**: Paths excluded in `meta.json` gracefully refuse writes.
4. **Advisory Locks**: Agent writes take locks and abort if the note changed between read and pre-write stat.
5. **Global Audit Trail**: Every CLI mutation appends exactly one line to `vault/LOG.md`.
6. **Explicit Deletes**: Deletes and applied renames are distinct, explicit commands—never write side effects.

*Note: Advisory locks coordinate agent activity but do not restrict direct human edits through future local mirrors.*

## Data Classification

`fulcra-vault` is a **plaintext markdown store** backed by the Fulcra Files API. **It is NOT encrypted.**

- **Do NOT store secrets or credentials** (passwords, API keys, private keys). Use a dedicated secrets manager.
- The vault provides **integrity** (locks, validation, traversal defense) but **no confidentiality** beyond standard Fulcra account isolation.
- Treat vault contents as standard *context and memory*. Any agent or session authenticated to the same Fulcra account can read the entire vault.

## Development & Architecture

Most modules are pure and dependency-injected to ensure stability and testing ease:

- `schema.py`: Structure contracts and path helpers.
- `sections.py`: Owned-section mutation and note logs.
- `frontmatter.py`: Flat frontmatter subset.
- `links.py`: Wikilinks, backlinks, and rename planning.
- `map.py`: Deterministic MAP/HOT rendering.
- `vault.py`: Scaffold and restructure planning.
- `store.py`: Fulcra Files text transport.
- `locks.py`: Advisory lock records.
- `cli.py`: Command composition.

**Installation (Local Dev):**
```bash
uv pip install -e packages/fulcra-vault
python3 -m compileall -q packages/fulcra-vault/fulcra_vault
uv run pytest packages/fulcra-vault -q
```