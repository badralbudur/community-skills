---
name: fulcra-vault
description: "Manage a durable, Obsidian-like shared markdown knowledge vault using Open Knowledge Format (OKF) stored in Fulcra, enabling persistent shared memory across all agents."
homepage: "https://github.com/ashfulcra/fulcra-tools/tree/main/packages/fulcra-vault"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📓" } }
---

# fulcra-vault

You manage the user's shared markdown knowledge vault stored persistently in their Fulcra account, directly applying Open Knowledge Format (OKF) conventions. **You** natively execute operations on this vault using standard file manipulation tools (`read`, `write`, `edit`, `exec`).

The vault holds the durable prose memory for projects, people, decisions, corrections, and domain notes, interconnected via `[[wikilinks]]`. Because the vault is stored in Fulcra, it acts as a shared memory layer accessible to all the user's authorized agents.

If the user does not already have a Fulcra account, invoke the `fulcra-connect` skill (from `fulcradynamics/agent-skills`) to set one up before proceeding.

## Vault Structure (OKF Compliant)

- **Root:** `vault/` (stored in Fulcra)
- **Metadata:** `vault/meta.json` (schema and exclusion paths)
- **Navigation:**
  - `vault/index.md` (The full map of the vault)
  - `vault/log.md` (The top-level audit trail)
- **Notes:** Individual `.md` files containing:
  - OKF YAML frontmatter (`type`, `tags`, `updated_at`, etc.)
  - Owned sections fenced by HTML comments
  - An append-only `## Log` section at the end

## Operations (Native Agent Execution)

Execute the following actions natively:

### 1. Initialization (`init`)
When starting in an empty vault:
- Create `vault/meta.json` with an initial structure spec.
- Scaffold default directories (`projects/`, `people/`, `domains/`).
- Create `vault/index.md` and `vault/log.md` with proper OKF frontmatter.

### 2. Reading (`read` / `backlinks`)
- Use the `read` tool to inspect `vault/index.md` to find relevant concepts.
- To read a note, load the `.md` file.
- To find backlinks, use `exec` with `grep` (e.g., `grep -ri "\[\[Note Name\]\]" vault/`) to find references natively.

### 3. Writing and Updating (`write-section` / `append-log`)
When writing durable context back:
- **Owned Sections:** Use the `edit` tool to apply targeted mutations to sections owned by your agent ID (e.g., `<!-- section:summary owner:openclaw -->...<!-- /section:summary -->`). Do not rewrite the entire file unless replacing your own content.
- **Shared Logs:** Use `edit` or shell tools to append a single dated line (e.g., `- 2026-08-21T12:00:00Z openclaw: Captured preferences`) to the `## Log` section of the relevant note.
- **Frontmatter:** Ensure any YAML frontmatter updates leave the file as valid OKF. 
- ALWAYS append an audit line to `vault/log.md` when you mutate any note.

### 4. Indexing and Maintenance (`map` / `reindex` / `rename`)
- When you create a new note, explicitly update `vault/index.md` to map it properly.
- If you rename a note, you must natively search and rewrite all `[[old-name]]` inbound wikilinks to `[[new-name]]` across the vault to avoid dangling edges.
- Respect `exclusions` in `meta.json`.

## Safe Mutation

You are the engine. You enforce safety:
- **No Path Traversal:** Only write inside `vault/`.
- **Preserve Others' Data:** Never mutate an owned section belonging to a different agent.
- **Append Only Logs:** Never rewrite history in `## Log` or `vault/log.md`.
- **Deterministic Validation:** After editing, read the file back to verify OKF compliance and structural integrity.

See `references/fulcra-vault-cli.md` and `references/fulcra-vault-mcp.md` for guidance on interacting with the Fulcra datastore via CLI or MCP.
