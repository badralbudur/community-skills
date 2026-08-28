# Sister Cities runtime

Vendored, runnable v1 runtime for the `fulcra-sister-cities` community skill.

The game is **Sister Cities**; its newspaper is **The Daily Manifest**.

## Verify

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python run_tests.py
.venv/bin/python -m playtest.run --check
```

The included `playtest/` data is a synthetic multi-agent regression fixture,
not a real player transcript. Generated editions, site output, deployment
identifiers, runtime cache files, and credentials are intentionally excluded
from this skill package.

Read `SKILL.md` for the agent-facing Workspace, privacy, publication, and
real-player test contract.
