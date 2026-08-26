# fulcra-project-dashboard

A manager's view of what a team of agents has actually been doing.

When several agents share a workspace, the record of their work is real but scattered — logs, task files, handoffs, notes written at three in the morning. Answering "how is this going?" means reading all of it.

This skill builds a dashboard instead. It reads the workspace and renders an executive summary written from the team's own progress files, a log of recent work, how far along things are against what is still pending, a timeline with milestones, and a word map of where effort has actually gone — the shape of the work rather than a transcript of it.

The output is a static page: HTML, Alpine.js, plain CSS, no build step. It is served locally the same way `fulcra-dashboard` serves its own, and it pulls its charting libraries from public CDNs, so it wants a network connection when someone opens it. Hand the link to whoever needs the picture without the raw material.

Built on `fulcra-workspaces`, following the delivery approach of `fulcra-dashboard` (both in fulcradynamics/agent-skills).
