---
name: phaseharness-dashboard
description: Use when the user explicitly invokes phaseharness-dashboard, or asks what Phaseharness is currently doing, to show the dashboard, or to inspect Phaseharness runs.
---

# Phaseharness Dashboard

Phaseharness dashboard starts a local page that inspects durable run files under `.phaseharness/runs/<run-id>/`.
It does not change workflow state, prompts, artifacts, product code, commits, or hooks.

## Rules

- Run commands from the repository root.
- Do not ask the user to run the dashboard command manually when you can run it yourself.
- Run `python3 .phaseharness/skills/phaseharness-dashboard/scripts/render-dashboard.py` to start the dashboard server.
- The command keeps running. Keep it open while the user wants the dashboard page.
- Open the printed localhost URL in the browser.
- The page polls `/api/dashboard` every 2 seconds and updates automatically.
- The page is read-only. Resume controls should show copyable `phaseharness로 <run-id> 이어서 진행해줘.` requests, not mutate run state directly.
- If no runs exist, say that no Phaseharness runs were found in this worktree.
- Remember that dashboard data is per worktree. Runs from another git worktree must be inspected from that worktree.

## Run

```bash
python3 .phaseharness/skills/phaseharness-dashboard/scripts/render-dashboard.py
```

This single command serves a page that shows the current active run first, then progress, outputs, all-run history totals, and recent runs. Diagnostics and feedback details appear only when there is meaningful data. If there is no active run, the page reports that and still shows history/recent runs when available. Resumable runs include a copy button for the resume request.

## When To Refresh

The server refreshes dashboard JSON files on each poll. It writes generated views under:

```text
.phaseharness/runs/<run-id>/dashboard/
```

## Reporting

For a normal "show previous runs" request, report:

- run id
- status
- current stage
- current phase, if any
- updated time

For a normal "show dashboard" request, start the server and open the page. The page shows the active run's `run_id`, request, status, current stage, current phase, `next_action`, output paths, all-run history totals, and recent run list. Diagnostics and feedback details are conditional.

For a resume/stale request, run the dashboard script and report `can_resume`, `next_action`, `reason`, and stale timing from the generated `dashboard/resume.json` when available.
