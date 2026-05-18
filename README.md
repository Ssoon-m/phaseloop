# Phaseharness

Phaseharness is a harness system that helps AI coding agents process work in stages.

Rather than providing a universal harness for every project, it focuses on helping you
build a custom harness that fits each project's guidelines and workflow.

Users can connect architecture documents, coding rules, review criteria, team tacit
knowledge, and other project-specific guidance to Phaseharness. This helps the agent
actively reflect project context while planning, implementing, and reviewing work.

The `context-gather` stage records which documents were referenced and what was read
from them in `.phaseharness/runs/*/artifacts/context.md`. This makes it easier to see
what the plan was based on.

The more you use `phaseharness`, the easier it becomes to identify which guidance was
missing when results do not match expectations. Over time, this creates a natural loop
for improving the project documentation.

## Workflow

Phaseharness makes the agent follow this sequence:

```text
clarify -> context-gather -> plan -> generate -> evaluate
```

- `clarify`: organize the goal, scope, success criteria, and required decisions.
- `context-gather`: inspect relevant code and project guidance.
- `plan`: split the work into steps that are easier to implement and review.
- `generate`: implement the planned steps.
- `evaluate`: review whether the final diff satisfies the original request and criteria.

You can still talk to the agent normally while the workflow is running. If requirements change, tell the agent what changed.
If you want it to stop, ask it to pause or stop.

## Install

Open Codex or Claude in the repository where you want to use Phaseharness, then paste:

```text
Install phaseharness from this installer document:
https://github.com/Ssoon-m/phaseharness/blob/main/installer/install-harness.md
```

The target project must be a git repository and must be able to run `python3`.
An initial commit is not required for normal use, but it is required when creating a parallel worktree.

## Quick Start

Ask the agent to use Phaseharness for the task:

```text
Use `phaseharness` to implement <task>.
```

Before starting phaseharness, choose two options:

- `loop count`: how many times implementation can be retried if review finds problems.
- `commit mode`: whether to request commits during the workflow.

Defaults:

```text
loop count: 2
commit mode: none
```

`commit mode` controls when Phaseharness asks for commits.

- `none`: do not ask for commits during the workflow.
- `phase`: ask for a commit whenever a phase from `plan` is completed in `generate`.
- `final`: do not commit after each phase; ask for one final commit when `evaluate` passes or has only warnings.

Commits are not pushed automatically. Ask the agent to push separately when you want that.

## Dashboard Views

You can check the current task, previous task history, and generated outputs in one place on the dashboard.
Ask the agent like this:

```text
Use `phaseharness-dashboard` to show the dashboard.
```

## Important: Connect Project Guidance

If your project has guidance documents the agent should follow, such as architecture documents, coding rules, or review criteria, connect them before starting the first real task.

As models improve, the agent often finds task-relevant documents by inspecting the repository during `context-gather`. Still, explicitly listing important guidance makes it more likely to be reflected consistently in the work plan and review criteria.

> Continuously maintaining and connecting project-specific guidance is one of the best ways to use Phaseharness well and the first step toward building a harness that fits your project.

After installing `phaseharness`, copy the example file:

```bash
cp .phaseharness/context.example.json .phaseharness/context.json
```

Then edit `.phaseharness/context.json` for your project.

- Put documents that affect implementation planning under `context-gather.documents`.
- Put documents that should guide code review under `evaluate.documents`.
- Put additional review rules under `evaluate.rules`.

Use these priority values:

- `required`: must be checked when relevant.
- `recommended`: considered when relevant.
- `optional`: used only when clearly relevant.

If the project has no separate guidance documents, you can skip this step.

## Customize Stage Prompts

If connecting project guidance as documents is not enough, you can directly edit the prompt for each stage.

Stage prompts such as `clarify`, `context-gather`, `plan`, `generate`, and `evaluate` live under `.phaseharness/skills`. Edit these files when you want to make the workflow or review criteria more specific to your project.

Modified skill files are synced to the Codex and Claude Code skill directories on SessionStart. Do not edit `.agents/skills` or `.claude/skills` directly; manage `.phaseharness/skills` as the SSOT (Single Source of Truth).

## AGENTS.md / CLAUDE.md Guide

Keep only the minimum guidance that is always required before running Phaseharness in `AGENTS.md` or `CLAUDE.md`.

Manage stage-specific workflow, review criteria, and project-specific rules in `.phaseharness/skills` and `.phaseharness/context.json`. This keeps the global instructions that agents always read lightweight, while allowing detailed Phaseharness guidance to improve gradually inside the harness.

## Resume After A Session Ends

> Here, worktree means a git worktree.

If a session ends during work and you reopen Codex or Claude in the same project folder, the agent will detect the in-progress Phaseharness task and ask what to do.

- `resume`: continue the existing task.
- `start-new`: pause the existing task and start a new task in the same worktree.
- `start-new-in-worktree`: keep the existing task as-is and start the new task in a separate git worktree.

Choose `resume` to continue the previous task.
Choose `start-new-in-worktree` when you want to keep two tasks separate.

If you choose `start-new-in-worktree`, Phaseharness creates a new worktree and branch, then tells you the path. It does not automatically continue that work in the current session. Open a new Codex or Claude session at the provided worktree path and ask it to continue the Phaseharness task.

This separation exists because using one session across multiple worktrees can mix up file paths, git state, and Phaseharness run state. Each worktree is safer to handle in its own session.

## Run Only One Stage

If the full workflow is too much for a small task, or you only need help from one stage, you can run an individual skill directly.

```text
Use `clarify` for <task>.
Use `context-gather` for <task>.
Use `plan` for <task>.
Use `generate` for phase-001.
Use `evaluate` for the current diff.
```

Individual skills run only the requested stage once and then stop. Use `phaseharness` when you want to hand off a large task end to end; choose individual skills when you only need part of the workflow.

- Use `clarify` when you only want to organize requirements or scope first.
- Use `context-gather` when you only want to collect relevant code and document context before implementing.
- Use `plan` when you only want an implementation plan and phase split.
- Use `evaluate` when implementation is already done and you want the current diff reviewed.
- Do not use `generate` by itself as a general implementation request. Use it only when there is a phase file produced by `plan`, and you want to implement one specific phase.

## Updating

Phaseharness updates are mostly handled automatically on SessionStart.

Updates apply only to Phaseharness-managed files recorded in `.phaseharness/manifest.json`. Files with local edits are skipped instead of being overwritten automatically.

If you customize Phaseharness-managed files for your project, those files can block part of an update. In that case, SessionStart prints the skipped files and asks whether to overwrite them with the new Phaseharness version.

To disable automatic SessionStart updates for this project, copy `.phaseharness/settings.example.json` to `.phaseharness/settings.json`, then set `update.enabled` to `false`.
