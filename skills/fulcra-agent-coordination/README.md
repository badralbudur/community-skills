# fulcra-agent-coordination

Keeps a group of agents working as a team rather than as strangers sharing a folder.

A shared workspace lets several agents read and write the same files. That is enough right up until it isn't: two agents take the same job, an agent goes quiet and nobody notices, work gets handed over and the context is gone, a review is requested and never answered.

This skill adds the missing pieces:

- **Presence** — who is actually alive right now, not who was configured.
- **Roles** — durable jobs held under a lease, so a role has one holder and a lapsed one is visible instead of silent.
- **Continuity** — an agent can stop mid-task and a different one can pick it up knowing where things stood.
- **Reviews and directed work** — requests that are acknowledged and answered, rather than dropped.
- **Health** — a fleet-wide read of what is working.

The folding is done by a vendored, standard-library-only engine, so the answers are deterministic and the skill has nothing to install.

Builds on `fulcra-workspaces` in fulcradynamics/agent-skills.
