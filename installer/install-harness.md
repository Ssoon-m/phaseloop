# install-harness

Use this document in the target repository session.

Install Phaseharness into another repository. The files to copy are stored in this repository's `.phaseharness/` directory.

## Source

Default repository:

```text
https://github.com/Ssoon-m/phaseharness.git
```

Optional overrides:

- `HARNESS_SOURCE`: absolute path to a local checkout of this repository
- `HARNESS_REPO_URL`: alternate git URL

## Preflight

Run from the target repository root:

```bash
pwd
git rev-parse --is-inside-work-tree
git status --short
command -v python3
```

Stop if this is not a git repository or `python3` is unavailable. Dirty worktree is allowed, but do not overwrite unrelated user changes.

An initial commit is not required for normal installation or a single Phaseharness run. It is required only when using `start-new-in-worktree`, because git worktrees need a commit ref such as `HEAD`.

## Resolve The Source Repository

If `HARNESS_SOURCE` is set:

```bash
test -d "$HARNESS_SOURCE/.phaseharness"
test -f "$HARNESS_SOURCE/.phaseharness/bin/phaseharness-state.py"
test -f "$HARNESS_SOURCE/.phaseharness/bin/phaseharness-hook.py"
test -f "$HARNESS_SOURCE/.phaseharness/bin/phaseharness-sync-bridges.py"
test -f "$HARNESS_SOURCE/.phaseharness/bin/phaseharness-update.py"
test -f "$HARNESS_SOURCE/.phaseharness/bin/phaseharness-worktree.py"
test -f "$HARNESS_SOURCE/.phaseharness/manifest.json"
test -f "$HARNESS_SOURCE/.phaseharness/settings.example.json"
test -f "$HARNESS_SOURCE/.phaseharness/context.example.json"
test -f "$HARNESS_SOURCE/.phaseharness/context.schema.json"
test -f "$HARNESS_SOURCE/.phaseharness/skills/phaseharness/SKILL.md"
test -f "$HARNESS_SOURCE/.phaseharness/skills/phaseharness-dashboard/SKILL.md"
test -f "$HARNESS_SOURCE/.phaseharness/skills/commit/SKILL.md"
test -f "$HARNESS_SOURCE/.phaseharness/skills/context-gather/scripts/render-context-config.py"
test -f "$HARNESS_SOURCE/.phaseharness/skills/evaluate/scripts/render-evaluation-config.py"
test -f "$HARNESS_SOURCE/.phaseharness/skills/phaseharness-dashboard/scripts/render-dashboard.py"
```

If `HARNESS_SOURCE` is not set:

```bash
HARNESS_REPO_URL="${HARNESS_REPO_URL:-https://github.com/Ssoon-m/phaseharness.git}"
HARNESS_SOURCE="$(mktemp -d)/phaseharness"
git clone --depth=1 "$HARNESS_REPO_URL" "$HARNESS_SOURCE"
```

## Copy Phaseharness Files

```bash
mkdir -p .phaseharness
cp -R "$HARNESS_SOURCE/.phaseharness/." .phaseharness/
chmod +x .phaseharness/bin/*.py .phaseharness/hooks/*.sh
```

All installed workflow files live under `.phaseharness/`.

## Connect Tool Files

```bash
python3 .phaseharness/bin/phaseharness-sync-bridges.py
```

This creates or updates:

- `.claude/settings.json` phaseharness `SessionStart` and `Stop` hook entries
- `.codex/config.toml` `[features].hooks = true`
- `.codex/hooks.json` phaseharness `SessionStart` and `Stop` hook entries
- `.claude/skills/{clarify,context-gather,plan,generate,evaluate,commit,phaseharness,phaseharness-dashboard}`
- `.agents/skills/{clarify,context-gather,plan,generate,evaluate,commit,phaseharness,phaseharness-dashboard}`
- `.phaseharness/bin/phaseharness-update.py` for safe SessionStart updates from managed file hashes
- `.phaseharness/bin/phaseharness-worktree.py` for parallel worktree creation
- `.phaseharness/state/active.json`
- `.phaseharness/state/index.json`

Subagents are not predeclared. The `generate` and `evaluate` skills create fresh subagent requests when those stages run.

## Project Context Config

`.phaseharness/context.example.json` and `.phaseharness/context.schema.json` are installed as documentation for project-specific context.

`.phaseharness/settings.example.json` documents project-owned Phaseharness settings. Copy it to `.phaseharness/settings.json` and set `update.enabled` to `false` when the project should not auto-update Phaseharness on SessionStart.

After installing, ask the user to connect any existing project rule documents before the first real phaseharness run:

```bash
cp .phaseharness/context.example.json .phaseharness/context.json
```

The example file is not active configuration. `context-gather` and `evaluate` read `.phaseharness/context.json` only when that file exists.

The active config supports `context-gather.documents`, `evaluate.documents`, and `evaluate.rules`.

## When The Stop Hook Runs

The installed Stop hook is inert for normal questions. It only calls:

```bash
python3 .phaseharness/bin/phaseharness-state.py next --require-auto --reprompt-running --require-session-binding --json
```

The hook may continue work only when `.phaseharness/state/active.json` points to an active auto run created by `phaseharness` and the hook session id matches the run binding.

## Smoke Verification

```bash
python3 .phaseharness/bin/phaseharness-state.py --help
python3 .phaseharness/bin/phaseharness-hook.py --help
python3 .phaseharness/bin/phaseharness-sync-bridges.py --help
python3 .phaseharness/bin/phaseharness-update.py check --source "$HARNESS_SOURCE" --quiet
python3 .phaseharness/bin/phaseharness-worktree.py --help
python3 "$(git rev-parse --show-toplevel)/.phaseharness/skills/context-gather/scripts/render-context-config.py"
python3 "$(git rev-parse --show-toplevel)/.phaseharness/skills/evaluate/scripts/render-evaluation-config.py"
python3 "$(git rev-parse --show-toplevel)/.phaseharness/skills/phaseharness-dashboard/scripts/render-dashboard.py" --help
python3 -m py_compile .phaseharness/bin/*.py
python3 -m py_compile "$(git rev-parse --show-toplevel)/.phaseharness/skills/phaseharness-dashboard/scripts/render-dashboard.py"
python3 .phaseharness/bin/phaseharness-state.py next --require-auto --reprompt-running --require-session-binding --json
```

When no automatic run is active, the output should include `"action": "none"`.

## Required Post-Install Message

After smoke verification, tell the user exactly what was installed and what to do next. Do not assume they will read the README.

Use this message shape:

```text
Phaseharness is installed.

Important: before starting a real phaseharness run, if this project has architecture, coding convention, or other guidance documents, please connect them in `.phaseharness/context.json` using `.phaseharness/context.example.json` as the format reference.

Then start a workflow with:

Use `phaseharness` to implement <task>.
```
