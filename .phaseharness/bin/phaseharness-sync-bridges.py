#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
from pathlib import Path
from typing import Any


SKILLS = [
    "clarify",
    "context-gather",
    "plan",
    "generate",
    "evaluate",
    "commit",
    "phaseharness",
    "phaseharness-dashboard",
]
SKILL_ROOTS = [
    Path(".claude") / "skills",
    Path(".agents") / "skills",
]
HOOK_MARKER = ".phaseharness"


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    while current != current.parent:
        if (current / ".phaseharness").is_dir() or (current / ".git").is_dir():
            return current
        current = current.parent
    raise RuntimeError("could not find project root")


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def make_executable(path: Path) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def command_for(runtime: str, event: str) -> str:
    script = f"{runtime}-{event}.sh"
    if runtime == "claude":
        return (
            "sh -c 'root=\"$(git -C \"${CLAUDE_PROJECT_DIR:-$PWD}\" rev-parse --show-toplevel 2>/dev/null || printf %s \"${CLAUDE_PROJECT_DIR:-$PWD}\")\"; "
            f"f=\"$root/.phaseharness/hooks/{script}\"; "
            "[ -x \"$f\" ] && exec \"$f\"; "
            "exit 0'"
        )
    if runtime == "codex":
        return (
            "sh -c 'root=\"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\"; "
            f"f=\"$root/.phaseharness/hooks/{script}\"; "
            "[ -x \"$f\" ] && exec \"$f\"; "
            "exit 0'"
        )
    raise ValueError(runtime)


def hook_entry(runtime: str, event: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "command",
        "command": command_for(runtime, event),
        "timeout": 30,
    }
    if runtime == "codex" and event == "stop":
        entry["statusMessage"] = "Checking phaseharness state"
    if runtime == "codex" and event == "session-start":
        entry["statusMessage"] = "Syncing phaseharness bridges"
    return entry


def command_is_phaseharness(value: Any) -> bool:
    return isinstance(value, dict) and HOOK_MARKER in str(value.get("command", ""))


def merge_hook(data: dict[str, Any], event: str, matcher: str, entry: dict[str, Any]) -> None:
    hooks_root = data.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        raise RuntimeError("hooks must be an object")
    groups = hooks_root.setdefault(event, [])
    if not isinstance(groups, list):
        raise RuntimeError(f"hooks.{event} must be a list")
    target: dict[str, Any] | None = None
    for group in groups:
        if not isinstance(group, dict):
            continue
        existing = group.get("hooks", [])
        if isinstance(existing, list):
            existing[:] = [item for item in existing if not command_is_phaseharness(item)]
        if str(group.get("matcher", "")) == matcher:
            target = group
    if target is None:
        target = {"hooks": []}
        if matcher:
            target["matcher"] = matcher
        groups.append(target)
    entries = target.setdefault("hooks", [])
    if not isinstance(entries, list):
        raise RuntimeError(f"hooks.{event}[].hooks must be a list")
    entries.append(entry)


def install_claude_hooks(root: Path) -> list[Path]:
    make_executable(root / ".phaseharness" / "hooks" / "claude-stop.sh")
    make_executable(root / ".phaseharness" / "hooks" / "claude-session-start.sh")
    path = root / ".claude" / "settings.json"
    data = load_json_object(path)
    merge_hook(data, "SessionStart", "startup|resume|clear|compact", hook_entry("claude", "session-start"))
    merge_hook(data, "Stop", "", hook_entry("claude", "stop"))
    write_json(path, data)
    return [path]


def ensure_codex_feature_flag(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    feature_header = re.compile(r"^\s*\[features\]\s*$")
    section_header = re.compile(r"^\s*\[[^\]]+\]\s*$")
    for index, line in enumerate(lines):
        if not feature_header.match(line):
            continue
        cursor = index + 1
        while cursor < len(lines) and not section_header.match(lines[cursor]):
            if re.match(r"^\s*hooks\s*=", lines[cursor]):
                lines[cursor] = "hooks = true"
                path.write_text("\n".join(lines).rstrip() + "\n")
                return
            cursor += 1
        lines.insert(index + 1, "hooks = true")
        path.write_text("\n".join(lines).rstrip() + "\n")
        return
    prefix = "\n\n" if text.strip() else ""
    path.write_text(text.rstrip() + f"{prefix}[features]\nhooks = true\n")


def install_codex_hooks(root: Path) -> list[Path]:
    make_executable(root / ".phaseharness" / "hooks" / "codex-stop.sh")
    make_executable(root / ".phaseharness" / "hooks" / "codex-session-start.sh")
    config_path = root / ".codex" / "config.toml"
    hooks_path = root / ".codex" / "hooks.json"
    ensure_codex_feature_flag(config_path)
    data = load_json_object(hooks_path)
    merge_hook(data, "SessionStart", "startup|resume|clear", hook_entry("codex", "session-start"))
    merge_hook(data, "Stop", "", hook_entry("codex", "stop"))
    write_json(hooks_path, data)
    return [config_path, hooks_path]


def discover_skill_dirs(root: Path) -> list[Path]:
    skills_root = root / ".phaseharness" / "skills"
    seen: set[str] = set()
    skill_dirs: list[Path] = []
    for skill_name in SKILLS:
        source = skills_root / skill_name
        if not source.exists():
            raise RuntimeError(f"missing skill: {source}")
        if not (source / "SKILL.md").is_file():
            raise RuntimeError(f"missing skill entrypoint: {source / 'SKILL.md'}")
        skill_dirs.append(source)
        seen.add(skill_name)
    for source in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        if source.name in seen:
            continue
        if (source / "SKILL.md").is_file():
            skill_dirs.append(source)
    return skill_dirs


def copy_skill(source: Path, target: Path) -> Path:
    if not source.exists():
        raise RuntimeError(f"missing skill: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source.resolve():
            target.unlink()
        else:
            raise RuntimeError(f"skill target is a symlink to another path: {target}")
    if target.exists():
        if target.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
            return target
        raise RuntimeError(f"skill target exists and is not a directory: {target}")
    shutil.copytree(source, target)
    return target


def install_skill_bridges(root: Path) -> list[Path]:
    changed: list[Path] = []
    for source in discover_skill_dirs(root):
        for skill_root in SKILL_ROOTS:
            changed.append(copy_skill(source, root / skill_root / source.name))
    return changed


def ensure_state_files(root: Path) -> list[Path]:
    state_dir = root / ".phaseharness" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    active = state_dir / "active.json"
    if not active.exists():
        write_json(
            active,
            {
                "schema_version": 1,
                "active_run": None,
                "activation_source": None,
                "mode": None,
                "status": "inactive",
                "provider": None,
                "session_id": None,
                "bound_at": None,
                "bound_source": None,
                "worktree_root": None,
            },
        )
        changed.append(active)
    index = state_dir / "index.json"
    if not index.exists():
        write_json(index, {"schema_version": 1, "runs": []})
        changed.append(index)
    (root / ".phaseharness" / "runs").mkdir(parents=True, exist_ok=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync phaseharness hooks and skill bridges.")
    parser.add_argument("--runtime", choices=["all", "claude", "codex"], default="all")
    parser.add_argument("--skip-skills", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    root = find_project_root()
    changed: list[Path] = ensure_state_files(root)
    if not args.skip_skills:
        changed.extend(install_skill_bridges(root))
    if args.runtime in ("all", "claude"):
        changed.extend(install_claude_hooks(root))
    if args.runtime in ("all", "codex"):
        changed.extend(install_codex_hooks(root))
    if not args.quiet:
        for path in changed:
            print(path.relative_to(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
