# Roles Manifest

Active roles for this project's control harness. Roles are named by
function, not hardcoded pair names -- add/remove rows as the project's
actual operating shape changes (e.g. a project might run two Generators
against different milestones in parallel, or add a distinct Harness
Maintainer role per HARNESS_GOVERNANCE.md).

| Role | Definition file | Provider adapter env var | Active? |
|---|---|---|---|
| Generator | `roles/generator.md` | `HARNESS_GENERATOR_CMD` | yes |
| Evaluator | `roles/evaluator.md` | `HARNESS_EVALUATOR_CMD` | yes |
| Harness Maintainer | (see HARNESS_GOVERNANCE.md) | n/a (ad hoc, human or agent) | as needed |

Generator and Evaluator MUST be genuinely separate sessions/processes
(non-negotiable per README.md) -- this table records which commands
back them, not a claim that one session can play both roles.
