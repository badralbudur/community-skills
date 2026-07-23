---
name: project-dashboard
description: "Builds a management dashboard for an agent-teams workspace, showing progress, logs, a generated summary, timeline/milestone charts, and a word map of agent activities."
homepage: "https://github.com/fulcradynamics/community-skills"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📈" } }
---

# Project Dashboard

This skill generates a highly visual, manager-oriented dashboard to track the work of an Agent Team (built around the `fulcra-agent-teams` structure). It leverages the paradigms established in `fulcra-dashboard`—specifically a lightweight, build-less static triad (HTML, Alpine.js, Vanilla CSS)—and introduces specific management-oriented visualizations.

## Core Objective

Provide a "View" for a human manager that conveys what an agent team has been working on, how far along the project is, and what milestones are approaching.

## Required Dashboard Components

When building or updating the dashboard, you must include the following elements:

1. **Agent-Generated Executive Summary:**
   - Read the root `team/<team-name>/progress.md`, `team/<team-name>/role.md`, and individual member files (`team/<team-name>/member/<name>/progress.md`).
   - Distill these into a cohesive, executive-level text summary of the current project status and recent accomplishments. This should be injected into the dashboard configuration (e.g., `data.json`).

2. **Recent Work Logs:**
   - A scrolling or paginated list of the most recent tasks completed by the team members.

3. **Project Progress & Completion (Overview):**
   - Visual indicators of overall completion. Use Progress Bars, Gauges, or Burn-down charts to show how much work is done versus pending.

4. **Timeline & Milestones Chart:**
   - Use D3.js or Plotly to render a Timeline or Gantt-style chart showing work items and milestones over time. This helps the manager understand the temporal flow of the project.

5. **Activity Word Map (Word Cloud):**
   - Implement a Word Cloud (e.g., using `d3-cloud` or native D3) parsing the text of recent agent actions, commit messages, or task descriptions to visually represent the most frequent focus areas.

## Data Sources

- **Primary Local Files:** Read the workspace directory, specifically targeting the `team/` structure, `task/` files, and `progress.md` tracking files.
- **Secondary Fulcra Annotations:** Use the `fulcra-api` CLI to fetch any relevant tracking annotations, user activity, or agent visibility logs (e.g., "Agent Tasks Completed" annotations) that provide context to the team's work.

## Implementation Workflow

1. **Data Gathering & Parsing:**
   - **Sync Team State:** Before generating or updating the dashboard, you must fetch the latest changes to the team files (e.g., via `git pull` or syncing the workspace) to ensure you are reporting on the most up-to-date progress.
   - Read local team progress files.
   - Run `uv tool run fulcra-api catalog` to locate and extract relevant historical annotations for the workspace.
   - Compile this data into clean `.jsonl` or `.json` files inside a `public/` directory, following the `fulcra-dashboard` data separation rules.

2. **Scaffolding the UI:**
   - Use a clean HTML template initialized with Alpine.js and Vanilla CSS.
   - Include necessary CDN libraries (`d3.js`, `d3-cloud`, `plotly.js`) in `index.html`.
   - Organize the layout into clear sections: Summary, Progress Overview, Timeline, Word Map, and Logs.

3. **Rendering the Visualizations:**
   - Keep chart logic modular inside the `Alpine.data()` block. 
   - Ensure the visualizations respect the CSS theme (use transparent backgrounds, inherit fonts and colors).

4. **Theming & Polish:**
   - The dashboard should look professional yet thematic to the project. Generate a thematic header image if applicable, and ensure the CSS provides a clear, scannable experience for a manager.

## Usage

When the user asks to "build a team dashboard," "show team progress," or "create a management view of the agent team," invoke this skill to aggregate the team's local progress files and Fulcra annotations into a robust static web dashboard.