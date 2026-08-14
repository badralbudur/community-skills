# Coord Upstream Alignment

This skill vendors an adapted subset of `coord-engine`. The executable selection
is `upstream-selection.json`; this document explains how to read it.

## Historical Snapshot

The current public tree identifies itself as engine `1.6.10` and was refreshed
in community-skills commit
`f4d5616a44bb400b9068bf606eddb2a92f4b907e`. That refresh deliberately trimmed
modules and rewrote internal terminology. It did not record the exact upstream
commit, so this repository does not fabricate one. Version `1.6.10` is lineage
evidence, not a content-addressed source pin.

## Comparison Target

The first reproducible comparison target is fulcra-tools commit
`facf745137fa64aa1133d5d02982ee6d24468cef`, coord-engine `1.11.0`. The manifest
classifies every Python module at that commit as included or excluded, with a
public rationale for every exclusion.

An inclusion means the module exists in the current vendored tree. It does not
claim byte identity: public copies may carry compatibility edits and
product-neutral documentation. Later PRs update the comparison commit and
selection only after their tests and behavior are deliberately ported.

## Drift Check

From the skill directory, run:

```bash
python3 scripts/check_upstream_alignment.py --upstream /path/to/fulcra-tools
```

The checker is read-only. It returns deterministic JSON and fails if the
upstream checkout is at a different commit, a vendored module is absent from
the inclusion list, or a current upstream module is neither included nor
explicitly excluded. It never rewrites the vendored tree.

For a refresh:

1. choose and record one full upstream commit;
2. add contract tests before copying behavior;
3. update included and excluded module rationales;
4. run the complete vendored suite and the checker;
5. review the diff for private topology, routing, model, account, and fleet
   policy before changing the comparison pin.

Live machine, cloud, harness, model, identity, and routing mappings belong in
Fulcra. They are never copied into this manifest.

