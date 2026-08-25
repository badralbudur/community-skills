# Decision Request Schema

```text
decision_request: true
id: <stable-slug>
from: <role>
milestone: <M#>
priority: blocking | before-next-milestone | informational
question: <exactly one concise user question>
context: <factual evidence>
options:
  - <option + consequence>
recommended_default: <optional, explicitly labeled>
```

Coordinator persists this in `team/<team>/decision/`, writes a
`DECISION REQUIRED` status/dashboard item, reports to origin, and pauses
blocking work. User answer is appended to `decisions.md` before any spec
revision/resumption.
