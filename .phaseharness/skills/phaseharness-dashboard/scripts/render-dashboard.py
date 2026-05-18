#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SCHEMA_VERSION = 1
DEFAULT_STALE_THRESHOLD_SECONDS = 1800
DEFAULT_RECENT_LIMIT = 10
HISTORY_GROUP_PAYLOAD_LIMIT = 200
STAGES = ["clarify", "context_gather", "plan", "generate", "evaluate"]
ARTIFACTS = {
    "clarify": "artifacts/clarify.md",
    "context_gather": "artifacts/context.md",
    "plan": "artifacts/plan.md",
    "generate": "artifacts/generate.md",
    "evaluate": "artifacts/evaluate.md",
}
COMMIT_TERMINAL_STATUSES = {"committed", "no_changes", "skipped"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PHASE_ID_RE = re.compile(r"\bphase-\d+\b", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    while current != current.parent:
        if (current / ".phaseharness").is_dir() or (current / ".git").exists():
            return current
        current = current.parent
    raise RuntimeError("could not find project root")


def resolve_root(root_arg: str | None) -> Path:
    root = Path(root_arg).expanduser().resolve() if root_arg else find_project_root()
    if not (root / ".phaseharness").is_dir():
        raise RuntimeError(f"could not find .phaseharness under root: {root}")
    return root


def harness_dir(root: Path) -> Path:
    return root / ".phaseharness"


def runs_dir(root: Path) -> Path:
    return harness_dir(root) / "runs"


def active_path(root: Path) -> Path:
    return harness_dir(root) / "state" / "active.json"


def run_dir(root: Path, run_id: str) -> Path:
    return runs_dir(root) / run_id


def run_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "run.json"


def dashboard_dir(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "dashboard"


def dashboard_path(root: Path, run_id: str, name: str) -> Path:
    return dashboard_dir(root, run_id) / f"{name}.json"


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise RuntimeError(f"unsafe run id: {run_id}")
    return run_id


def load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_object(path: Path) -> dict[str, Any]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso_or_none(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def duration_seconds(start: Any, end: Any) -> int | None:
    left = parse_time(start)
    right = parse_time(end)
    if left is None or right is None:
        return None
    return max(0, int((right - left).total_seconds()))


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def file_info(root: Path, path: Path) -> dict[str, Any]:
    exists = path.exists() and path.is_file()
    nonempty = False
    if exists:
        try:
            nonempty = path.stat().st_size > 0 and bool(path.read_text(encoding="utf-8").strip())
        except OSError:
            nonempty = False
    return {
        "path": relpath(root, path),
        "exists": exists,
        "nonempty": nonempty,
    }


def discover_run_ids(root: Path) -> list[str]:
    base = runs_dir(root)
    if not base.exists():
        return []
    run_ids: list[str] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        if RUN_ID_RE.match(path.name):
            run_ids.append(path.name)
    return run_ids


def discover_phase_ids(root: Path, state: dict[str, Any]) -> list[str]:
    run_id = str(state.get("run_id") or "")
    phase_dir = run_dir(root, run_id) / "phases"
    ids: set[str] = set()
    generate = state.get("generate")
    if isinstance(generate, dict):
        queue = generate.get("queue")
        if isinstance(queue, list):
            ids.update(str(item) for item in queue if PHASE_ID_RE.fullmatch(str(item)))
        statuses = generate.get("phase_status")
        if isinstance(statuses, dict):
            ids.update(str(key) for key in statuses if PHASE_ID_RE.fullmatch(str(key)))
    if phase_dir.exists():
        ids.update(path.stem for path in phase_dir.glob("phase-*.md") if path.is_file())
    return sorted(ids)


def read_events(root: Path, run_id: str) -> list[dict[str, Any]]:
    base = run_dir(root, run_id)
    candidates = [base / "events.jsonl"]
    events_dir = base / "events"
    if events_dir.exists():
        candidates.extend(sorted(events_dir.glob("*.jsonl")))
    events: list[dict[str, Any]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                data.setdefault("_source", relpath(root, path))
                events.append(data)
    return events


def latest_time_value(values: list[Any]) -> datetime | None:
    parsed = [item for item in (parse_time(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


def collect_time_values(state: dict[str, Any], events: list[dict[str, Any]]) -> list[Any]:
    values: list[Any] = [
        state.get("created_at"),
        state.get("updated_at"),
        state.get("completed_at"),
        state.get("failed_at"),
    ]
    stages = state.get("stages")
    if isinstance(stages, dict):
        for stage_state in stages.values():
            if isinstance(stage_state, dict):
                values.extend(stage_state.get(key) for key in ("started_at", "updated_at", "completed_at"))
    generate = state.get("generate")
    if isinstance(generate, dict):
        values.append(generate.get("updated_at"))
    blocked_by = state.get("blocked_by")
    if isinstance(blocked_by, dict):
        values.append(blocked_by.get("created_at"))
    inflight = state.get("inflight")
    if isinstance(inflight, dict):
        values.append(inflight.get("updated_at"))
    commits = state.get("commits")
    if isinstance(commits, dict):
        for commit in commits.values():
            if isinstance(commit, dict):
                values.extend(commit.get(key) for key in ("updated_at", "completed_at"))
    for event in events:
        values.extend(event.get(key) for key in ("time", "timestamp", "created_at", "updated_at"))
    return values


def state_binding(state: dict[str, Any]) -> dict[str, Any] | None:
    binding = state.get("session_binding")
    if isinstance(binding, dict) and binding.get("provider") and binding.get("session_id"):
        return binding
    provider = clean_optional(state.get("provider"))
    session_id = clean_optional(state.get("session_id"))
    if provider and session_id:
        return {"provider": provider, "session_id": session_id}
    return None


def stage_state(state: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = state.get("stages")
    if isinstance(stages, dict):
        item = stages.get(stage)
        if isinstance(item, dict):
            return item
    return {"status": "pending", "artifact": ARTIFACTS.get(stage), "attempts": 0}


def stage_status(state: dict[str, Any], stage: str) -> str:
    return str(stage_state(state, stage).get("status", "pending"))


def artifact_path_for(root: Path, state: dict[str, Any], stage: str) -> Path:
    run_id = str(state.get("run_id") or "")
    artifact = stage_state(state, stage).get("artifact") or ARTIFACTS.get(stage) or f"artifacts/{stage}.md"
    return run_dir(root, run_id) / str(artifact)


def phase_file_path(root: Path, state: dict[str, Any], phase_id: str | None) -> Path | None:
    if not phase_id:
        return None
    return run_dir(root, str(state.get("run_id") or "")) / "phases" / f"{phase_id}.md"


def markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^##\s+", text[match.end() :], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end() : end].strip()


def lines_with(pattern: str, text: str) -> list[str]:
    regex = re.compile(pattern, re.IGNORECASE)
    output: list[str] = []
    for line in text.splitlines():
        clean = line.strip()
        if clean and regex.search(clean):
            output.append(clean)
    return output


def phase_text_summary(path: Path | None) -> dict[str, str | None]:
    if path is None or not path.is_file():
        return {"title": None, "summary": None}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"title": None, "summary": None}
    title: str | None = None
    summary: str | None = None
    in_goal = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
            continue
        if re.match(r"^##\s+goal\s*$", line, re.IGNORECASE):
            in_goal = True
            continue
        if in_goal:
            if line.startswith("#"):
                in_goal = False
            else:
                summary = line.lstrip("-* ").strip()
                break
        if summary is None and not line.startswith("#"):
            summary = line.lstrip("-* ").strip()
    return {"title": title, "summary": summary}


def verdict_from_evaluate(text: str) -> str | None:
    verdict = markdown_section(text, "Verdict")
    candidates = [verdict, *text.splitlines()[:20]]
    for value in candidates:
        match = re.search(r"\b(pass|warn|fail|skipped)\b", value, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def validation_commands_from_text(text: str) -> list[str]:
    commands: list[str] = []
    for line in text.splitlines():
        match = re.search(r"^\s*[-*]\s*command:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            command = match.group(1).strip()
            if command:
                commands.append(command)
    return commands


def detect_failure_categories(text: str) -> list[str]:
    patterns = {
        "requirements": r"\b(requirement|acceptance|success criteria|missing|incomplete)\b",
        "validation": r"\b(test|validation|command|check|type-?check|lint|failed)\b",
        "runtime": r"\b(runtime|exception|traceback|crash|error)\b",
        "type": r"\b(type|typing|schema|contract)\b",
        "boundary": r"\b(boundary|forbidden|scope|out of scope|repository)\b",
        "ux": r"\b(ux|ui|layout|overlap|responsive|accessibility)\b",
    }
    return sorted(name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE))


def phase_ids_in_text(text: str) -> list[str]:
    return sorted({match.group(0).lower() for match in PHASE_ID_RE.finditer(text)})


def pending_phase_ids(state: dict[str, Any], phase_ids: list[str]) -> list[str]:
    generate = state.get("generate")
    statuses = generate.get("phase_status") if isinstance(generate, dict) else {}
    if not isinstance(statuses, dict):
        statuses = {}
    return [
        phase_id
        for phase_id in phase_ids
        if str(statuses.get(phase_id, "pending")) not in ("completed", *COMMIT_TERMINAL_STATUSES)
    ]


def build_summary_view(root: Path, state: dict[str, Any], generated_at: str) -> dict[str, Any]:
    run_id = str(state.get("run_id") or "")
    finished_at = state.get("completed_at") or state.get("failed_at") or state.get("updated_at")
    loop = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    worktree = state.get("worktree") if isinstance(state.get("worktree"), dict) else {}
    generate = state.get("generate") if isinstance(state.get("generate"), dict) else {}
    evaluation = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": run_id,
        "request": state.get("request"),
        "mode": state.get("mode"),
        "status": state.get("status"),
        "current_stage": state.get("current_stage"),
        "current_phase": generate.get("current_phase"),
        "loop": loop,
        "evaluation_status": evaluation.get("status"),
        "commit_mode": state.get("commit_mode"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "completed_at": state.get("completed_at"),
        "failed_at": state.get("failed_at"),
        "duration_seconds": duration_seconds(state.get("created_at"), finished_at),
        "worktree": {
            "root": worktree.get("root"),
            "branch": worktree.get("branch"),
        },
    }


def build_progress_view(root: Path, state: dict[str, Any], generated_at: str) -> dict[str, Any]:
    phase_ids = discover_phase_ids(root, state)
    generate = state.get("generate") if isinstance(state.get("generate"), dict) else {}
    phase_status = generate.get("phase_status") if isinstance(generate.get("phase_status"), dict) else {}
    phase_attempts = generate.get("phase_attempts") if isinstance(generate.get("phase_attempts"), dict) else {}
    phase_messages = generate.get("phase_messages") if isinstance(generate.get("phase_messages"), dict) else {}

    workflow = state.get("workflow") if isinstance(state.get("workflow"), list) else STAGES
    stages: list[dict[str, Any]] = []
    for stage in workflow:
        stage_name = str(stage)
        item = stage_state(state, stage_name)
        path = artifact_path_for(root, state, stage_name)
        stages.append(
            {
                "stage": stage_name,
                "status": item.get("status", "pending"),
                "attempts": int(item.get("attempts", 0) or 0),
                "timing": {
                    "started_at": item.get("started_at"),
                    "updated_at": item.get("updated_at"),
                    "completed_at": item.get("completed_at"),
                    "duration_seconds": duration_seconds(item.get("started_at"), item.get("completed_at") or item.get("updated_at")),
                },
                "artifact": file_info(root, path),
                "message": item.get("message"),
            }
        )

    phases: list[dict[str, Any]] = []
    current_phase = clean_optional(generate.get("current_phase"))
    for phase_id in phase_ids:
        path = phase_file_path(root, state, phase_id)
        phase_text = phase_text_summary(path)
        phases.append(
            {
                "phase_id": phase_id,
                "title": phase_text.get("title"),
                "summary": phase_text.get("summary"),
                "status": str(phase_status.get(phase_id, "pending")),
                "attempts": int(phase_attempts.get(phase_id, 0) or 0),
                "timing": {
                    "updated_at": generate.get("updated_at"),
                },
                "file": file_info(root, path) if path else {"path": None, "exists": False, "nonempty": False},
                "current": phase_id == current_phase,
                "message": phase_messages.get(phase_id),
            }
        )

    commits: list[dict[str, Any]] = []
    raw_commits = state.get("commits")
    if isinstance(raw_commits, dict):
        for key, value in sorted(raw_commits.items()):
            commit = value if isinstance(value, dict) else {}
            paths = commit.get("paths") if isinstance(commit.get("paths"), dict) else {}
            commits.append(
                {
                    "key": key,
                    "status": commit.get("status"),
                    "mode": commit.get("mode"),
                    "implementation_phase": commit.get("implementation_phase"),
                    "eligible_paths": paths.get("eligible_paths", []),
                    "message": commit.get("message"),
                    "updated_at": commit.get("updated_at"),
                    "completed_at": commit.get("completed_at"),
                }
            )

    loop = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": state.get("run_id"),
        "workflow": workflow,
        "current_stage": state.get("current_stage"),
        "current_phase": current_phase,
        "loop": loop,
        "stages": stages,
        "phases": phases,
        "commits": commits,
    }


def build_resume_view(
    root: Path,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    generated_at: str,
    threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone()
    last_event = latest_time_value(collect_time_values(state, events))
    seconds_since = int((now - last_event).total_seconds()) if last_event else None
    current_stage = clean_optional(state.get("current_stage")) or "clarify"
    current_phase = clean_optional((state.get("generate") or {}).get("current_phase")) if isinstance(state.get("generate"), dict) else None
    phase_ids = discover_phase_ids(root, state)
    required_phase_id = current_phase
    if current_stage == "generate" and not required_phase_id:
        pending = pending_phase_ids(state, phase_ids)
        required_phase_id = pending[0] if pending else None
    required_phase = phase_file_path(root, state, required_phase_id)
    status = str(state.get("status") or "unknown")
    current_status = stage_status(state, current_stage)
    is_running = current_status == "running"
    if current_stage == "generate" and required_phase_id:
        generate = state.get("generate") if isinstance(state.get("generate"), dict) else {}
        phase_status = generate.get("phase_status") if isinstance(generate.get("phase_status"), dict) else {}
        is_running = str(phase_status.get(required_phase_id, current_status)) == "running"
    is_stale = bool(status == "active" and is_running and seconds_since is not None and seconds_since >= threshold_seconds)

    blockers: list[dict[str, Any]] = []
    blocked_by = state.get("blocked_by")
    if isinstance(blocked_by, dict):
        blockers.append(blocked_by)
    if current_stage in ARTIFACTS and current_status == "completed":
        artifact = artifact_path_for(root, state, current_stage)
        info = file_info(root, artifact)
        if not info["nonempty"]:
            blockers.append({"kind": "missing_artifact", "path": info["path"], "message": "completed stage artifact is missing or empty"})
    if required_phase and required_phase_id and not required_phase.exists():
        blockers.append({"kind": "missing_phase_file", "path": relpath(root, required_phase), "message": "phase file is missing"})

    next_action, reason, can_resume = compute_next_action(state, current_stage, current_status, required_phase_id, is_stale, blockers)
    binding = state_binding(state) or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": state.get("run_id"),
        "can_resume": can_resume,
        "next_action": next_action,
        "reason": reason,
        "status": status,
        "stage": current_stage,
        "phase_id": required_phase_id,
        "required_artifact": ARTIFACTS.get(current_stage),
        "required_phase_file": relpath(root, required_phase) if required_phase else None,
        "inflight": state.get("inflight"),
        "stale": {
            "is_stale": is_stale,
            "last_event_at": last_event.isoformat(timespec="seconds") if last_event else None,
            "seconds_since_last_event": seconds_since,
            "threshold_seconds": threshold_seconds,
        },
        "session": {
            "provider": binding.get("provider"),
            "bound": bool(binding.get("provider") and binding.get("session_id")),
            "bound_at": binding.get("bound_at"),
            "bound_source": binding.get("bound_source"),
        },
        "blockers": blockers,
    }


def compute_next_action(
    state: dict[str, Any],
    current_stage: str,
    current_status: str,
    phase_id: str | None,
    is_stale: bool,
    blockers: list[dict[str, Any]],
) -> tuple[str, str, bool]:
    status = str(state.get("status") or "unknown")
    if status == "completed":
        return "completed", "run is completed", False
    if status in ("error", "failed"):
        return "failed", f"run status is {status}", False

    blocked_by = state.get("blocked_by")
    if status == "waiting_user":
        if isinstance(blocked_by, dict) and blocked_by.get("kind") == "manual_pause":
            return "resume_manual_pause", str(blocked_by.get("message") or "run is manually paused"), True
        message = blocked_by.get("message") if isinstance(blocked_by, dict) else None
        return "resume_wait_user", str(message or "run is waiting for user input"), False

    if blockers:
        hard_blockers = [item for item in blockers if item.get("kind") not in ("missing_artifact",)]
        if hard_blockers:
            return "blocked", str(hard_blockers[0].get("message") or hard_blockers[0].get("kind") or "run is blocked"), False

    commits = state.get("commits")
    if isinstance(commits, dict):
        for value in commits.values():
            if isinstance(value, dict) and value.get("status") == "pending":
                return "handle_commit_prompt", "a commit prompt is pending", True

    if current_stage == "generate":
        if current_status == "completed":
            return "start_next_stage", "generate is completed; continue to evaluate", True
        if is_stale:
            return "reprompt_running", "running generate phase is stale", True
        if phase_id:
            generate = state.get("generate") if isinstance(state.get("generate"), dict) else {}
            phase_status = generate.get("phase_status") if isinstance(generate.get("phase_status"), dict) else {}
            status_value = str(phase_status.get(phase_id, "pending"))
            if status_value in ("pending", "error", "failed"):
                return "start_next_phase", f"{phase_id} is {status_value}", True
            if status_value == "running":
                return "none", f"{phase_id} is already running", False
        return "start_next_phase", "next generate phase is available", True

    if current_stage == "evaluate" and current_status == "completed":
        evaluation = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
        if evaluation.get("status") in ("pass", "warn"):
            return "start_next_stage", "evaluate completed; state runner can finalize the run", True
        if evaluation.get("status") == "fail":
            return "start_next_stage", "evaluate failed; continue according to loop state", True

    if is_stale:
        return "reprompt_running", f"{current_stage} is stale", True
    if current_status in ("pending", "error"):
        return "start_next_stage", f"{current_stage} is {current_status}", True
    if current_status == "completed":
        return "start_next_stage", f"{current_stage} is completed", True
    if current_status == "running":
        return "none", f"{current_stage} is already running", False
    return "blocked", f"stage status is {current_status}", False


def build_diagnostics_view(root: Path, state: dict[str, Any], generated_at: str) -> dict[str, Any]:
    run_id = str(state.get("run_id") or "")
    evaluate_path = artifact_path_for(root, state, "evaluate")
    evaluate_text = read_text(evaluate_path)
    missing_data: list[str] = []
    if not evaluate_text.strip():
        missing_data.append("evaluate artifact is missing or empty")

    state_eval = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
    verdict = clean_optional(state_eval.get("status")) or verdict_from_evaluate(evaluate_text)
    failed_lines = lines_with(r"\bresult:\s*fail\b|\bfail(ed|ure)?\b", markdown_section(evaluate_text, "Evaluation Checks"))
    checked_sources = []
    for line in lines_with(r"^\s*[-*]?\s*source:", markdown_section(evaluate_text, "Evaluation Checks")):
        checked_sources.append(re.sub(r"^\s*[-*]\s*", "", line))
    findings_section = markdown_section(evaluate_text, "Findings")
    failure_text = "\n".join([findings_section, markdown_section(evaluate_text, "Risks"), markdown_section(evaluate_text, "Validation")])
    followup_section = markdown_section(evaluate_text, "Follow-Up Phases")
    followup_phase_ids = phase_ids_in_text(followup_section)
    loop = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    if not followup_phase_ids and (verdict == "fail" or int(loop.get("current", 1) or 1) > 1):
        pending = pending_phase_ids(state, discover_phase_ids(root, state))
        if pending:
            followup_phase_ids = pending
            missing_data.append("follow-up phases inferred from pending phase state because evaluate artifact did not list them explicitly")

    validation_sources = [
        read_text(artifact_path_for(root, state, "plan")),
        evaluate_text,
    ]
    for phase_id in discover_phase_ids(root, state):
        path = phase_file_path(root, state, phase_id)
        if path:
            validation_sources.append(read_text(path))
    commands = sorted({command for text in validation_sources for command in validation_commands_from_text(text)})
    validation_text = markdown_section(evaluate_text, "Validation")
    failed_commands = lines_with(r"\b(fail|failed|error|non-?zero)\b", validation_text)
    skipped_commands = lines_with(r"\b(skip|skipped|not run|not executed)\b", validation_text)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": run_id,
        "intent_alignment": {
            "status": verdict or "unknown",
            "failed_requirements": failed_lines,
            "summary": verdict or "evaluate verdict unavailable",
            "missing_data": missing_data[:] if not verdict else [],
        },
        "guidance_compliance": {
            "status": "fail" if failed_lines else (verdict or "unknown"),
            "checked_sources": checked_sources,
            "violations": failed_lines,
            "missing_data": ["evaluation checks not found"] if evaluate_text.strip() and not checked_sources else [],
        },
        "failure_analysis": {
            "categories": detect_failure_categories(failure_text),
            "findings": lines_with(r"\b(fail|failed|error|missing|risk|violation|broken)\b", failure_text),
            "followup_phases": followup_phase_ids,
        },
        "validation": {
            "commands_found": commands,
            "failed_commands": failed_commands,
            "skipped_commands": skipped_commands,
        },
        "missing_data": missing_data,
    }


def explicit_feedback_events(events: list[dict[str, Any]], completed_at: Any) -> list[dict[str, Any]]:
    completed = parse_time(completed_at)
    output: list[dict[str, Any]] = []
    for event in events:
        marker = " ".join(str(event.get(key, "")) for key in ("type", "kind", "category", "name")).lower()
        if "feedback" not in marker:
            continue
        explicit_post_completion = "post_completion" in marker or "post-completion" in marker
        event_time = latest_time_value([event.get("time"), event.get("timestamp"), event.get("created_at"), event.get("updated_at")])
        if completed:
            if event_time is None and not explicit_post_completion:
                continue
            if event_time and event_time < completed and not explicit_post_completion:
                continue
        elif not explicit_post_completion:
            continue
        output.append(
            {
                "type": event.get("type"),
                "kind": event.get("kind"),
                "category": event.get("category"),
                "message": event.get("message") or event.get("summary"),
                "created_at": event.get("created_at") or event.get("time") or event.get("timestamp"),
                "source": event.get("_source"),
            }
        )
    return output


def build_feedback_view(
    root: Path,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    loop = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    loop_current = int(loop.get("current", 1) or 1)
    loop_retries = max(0, loop_current - 1)
    evaluation = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
    evaluate_status = clean_optional(evaluation.get("status"))
    evaluate_text = read_text(artifact_path_for(root, state, "evaluate"))
    artifact_fail = verdict_from_evaluate(evaluate_text) == "fail"
    current_fail = 1 if evaluate_status == "fail" or (loop_retries == 0 and artifact_fail) else 0
    followup_phases = diagnostics.get("failure_analysis", {}).get("followup_phases", [])
    explicit_feedback = explicit_feedback_events(events, state.get("completed_at"))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_id": state.get("run_id"),
        "counts": {
            "evaluate_failures": loop_retries + current_fail,
            "followup_phases": len(followup_phases) if isinstance(followup_phases, list) else 0,
            "loop_retries": loop_retries,
            "explicit_post_completion_feedback": len(explicit_feedback),
        },
        "explicit_feedback": explicit_feedback,
        "notes": [
            "Counts are conservative and use explicit events plus current run/evaluate state only.",
            "No git-diff-based inferred correction count is produced.",
        ],
    }


def build_views(root: Path, state: dict[str, Any], generated_at: str | None = None) -> dict[str, dict[str, Any]]:
    stamp = generated_at or now_iso()
    run_id = str(state.get("run_id") or "")
    events = read_events(root, run_id)
    diagnostics = build_diagnostics_view(root, state, stamp)
    return {
        "summary": build_summary_view(root, state, stamp),
        "resume": build_resume_view(root, state, events, stamp),
        "progress": build_progress_view(root, state, stamp),
        "diagnostics": diagnostics,
        "feedback": build_feedback_view(root, state, events, diagnostics, stamp),
    }


def run_outputs(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    run_id = str(state.get("run_id") or "")
    progress = build_progress_view(root, state, now_iso())
    return {
        "artifacts": [item["artifact"] | {"stage": item["stage"]} for item in progress["stages"]],
        "phases": [item["file"] | {"phase_id": item["phase_id"], "status": item["status"]} for item in progress["phases"]],
    }


def refresh_run(root: Path, run_id: str) -> dict[str, Any]:
    state = load_json_object(run_path(root, run_id))
    state_run_id = clean_optional(state.get("run_id"))
    if state_run_id and state_run_id != run_id:
        raise RuntimeError(f"run id mismatch in run.json: expected {run_id}, got {state_run_id}")
    state["run_id"] = run_id
    views = build_views(root, state)
    for name, view in views.items():
        save_json(dashboard_path(root, run_id, name), view)
    return {"run_id": run_id, "dashboard": relpath(root, dashboard_dir(root, run_id))}


def load_run_views(root: Path, run_id: str, generated_at: str, refresh: bool = True) -> dict[str, Any]:
    state = load_json_object(run_path(root, run_id))
    state_run_id = clean_optional(state.get("run_id"))
    if state_run_id and state_run_id != run_id:
        raise RuntimeError(f"run id mismatch in run.json: expected {run_id}, got {state_run_id}")
    state["run_id"] = run_id
    views = build_views(root, state, generated_at=generated_at)
    if refresh:
        for name, view in views.items():
            save_json(dashboard_path(root, run_id, name), view)
    return {
        "state": state,
        "views": views,
        "outputs": run_outputs(root, state),
        "dashboard": relpath(root, dashboard_dir(root, run_id)),
    }


def load_generated_or_raw_summary(root: Path, run_id: str, generated_at: str) -> dict[str, Any]:
    path = dashboard_path(root, run_id, "summary")
    if path.exists():
        try:
            data = load_json_object(path)
            data.setdefault("run_id", run_id)
            return data
        except (OSError, json.JSONDecodeError, RuntimeError):
            pass
    state = load_json_object(run_path(root, run_id))
    state_run_id = clean_optional(state.get("run_id"))
    if not state_run_id:
        state["run_id"] = run_id
    return build_summary_view(root, state, generated_at)


def run_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("run_id") or ""))


def marker(info: dict[str, Any]) -> str:
    if info.get("nonempty"):
        return "ok"
    if info.get("exists"):
        return "empty"
    return "missing"


def load_recent_summaries(root: Path, generated_at: str, limit: int | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    summaries: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for run_id in discover_run_ids(root):
        try:
            summaries.append(load_generated_or_raw_summary(root, run_id, generated_at))
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            issues.append({"run_id": run_id, "reason": str(exc)})
    summaries.sort(key=run_sort_key, reverse=True)
    if limit is not None:
        summaries = summaries[:limit]
    return summaries, issues


def load_feedback_counts(root: Path, run_id: str, generated_at: str) -> dict[str, int]:
    path = dashboard_path(root, run_id, "feedback")
    data: dict[str, Any] | None = None
    if path.exists():
        try:
            data = load_json_object(path)
        except (OSError, json.JSONDecodeError, RuntimeError):
            data = None
    if data is None:
        state = load_json_object(run_path(root, run_id))
        state["run_id"] = run_id
        events = read_events(root, run_id)
        diagnostics = build_diagnostics_view(root, state, generated_at)
        data = build_feedback_view(root, state, events, diagnostics, generated_at)
    counts = data.get("counts") if isinstance(data, dict) else {}
    if not isinstance(counts, dict):
        counts = {}
    return {
        "evaluate_failures": int(counts.get("evaluate_failures") or 0),
        "followup_phases": int(counts.get("followup_phases") or 0),
        "loop_retries": int(counts.get("loop_retries") or 0),
        "explicit_post_completion_feedback": int(counts.get("explicit_post_completion_feedback") or 0),
    }


def status_bucket(status: Any) -> str:
    value = str(status or "").lower()
    if value == "completed":
        return "completed"
    if value in ("error", "failed", "fail"):
        return "error"
    if value == "waiting_user":
        return "waiting_user"
    if value in ("active", "running"):
        return "active"
    return "other"


def failure_reason(root: Path, run_id: str, generated_at: str) -> str:
    try:
        state = load_json_object(run_path(root, run_id))
    except (OSError, json.JSONDecodeError, RuntimeError):
        return "Could not read run state"
    blocked_by = state.get("blocked_by")
    if isinstance(blocked_by, dict):
        message = clean_optional(blocked_by.get("message"))
        if message:
            return message
    for stage in STAGES:
        item = stage_state(state, stage)
        if str(item.get("status") or "").lower() == "error":
            message = clean_optional(item.get("message"))
            if message:
                return message
            return f"{stage} stage failed"
    generate = state.get("generate") if isinstance(state.get("generate"), dict) else {}
    phase_status = generate.get("phase_status") if isinstance(generate.get("phase_status"), dict) else {}
    phase_messages = generate.get("phase_messages") if isinstance(generate.get("phase_messages"), dict) else {}
    for phase_id, status in phase_status.items():
        if str(status).lower() in ("error", "failed"):
            message = clean_optional(phase_messages.get(phase_id))
            if message:
                return message
            return f"{phase_id} failed"
    try:
        diagnostics = build_diagnostics_view(root, state | {"run_id": run_id}, generated_at)
    except (OSError, RuntimeError):
        diagnostics = {}
    findings = diagnostics.get("failure_analysis", {}).get("findings") if isinstance(diagnostics, dict) else None
    if isinstance(findings, list):
        for finding in findings:
            text = clean_optional(finding)
            if text:
                return text
    evaluation = state.get("evaluation") if isinstance(state.get("evaluation"), dict) else {}
    if evaluation.get("status") == "fail":
        return "Evaluation failed"
    return "No failure reason recorded"


def history_item(summary: dict[str, Any], detail: str | None = None) -> dict[str, Any]:
    stage = clean_optional(summary.get("current_stage"))
    phase = clean_optional(summary.get("current_phase"))
    detail_parts = [part for part in (stage.replace("_", " ") if stage else None, phase) if part]
    return {
        "run_id": str(summary.get("run_id") or ""),
        "status": str(summary.get("status") or "unknown"),
        "stage": stage,
        "phase": phase,
        "updated_at": summary.get("updated_at") or summary.get("created_at"),
        "detail": detail or " · ".join(detail_parts) or str(summary.get("status") or "unknown"),
    }


def build_history_totals(root: Path, summaries: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    status = {"active": 0, "waiting_user": 0, "completed": 0, "error": 0, "other": 0}
    mode = {"auto": 0, "manual": 0, "other": 0}
    stages = {stage: 0 for stage in STAGES}
    feedback = {
        "evaluate_failures": 0,
        "followup_phases": 0,
        "loop_retries": 0,
        "explicit_post_completion_feedback": 0,
    }
    failures: list[dict[str, str]] = []
    groups: dict[str, list[dict[str, Any]]] = {"all": [], "running": [], "resumable": [], "failed": []}
    resumable = 0
    for summary in summaries:
        bucket = status_bucket(summary.get("status"))
        status[bucket] = status.get(bucket, 0) + 1
        mode_value = str(summary.get("mode") or "other").lower()
        mode[mode_value if mode_value in mode else "other"] += 1
        stage = str(summary.get("current_stage") or "")
        if stage in stages:
            stages[stage] += 1
        item = history_item(summary)
        run_id = clean_optional(summary.get("run_id"))
        if run_id:
            groups["all"].append(item)
            if bucket == "active":
                groups["running"].append(item)
        if bucket not in ("completed", "error"):
            resumable += 1
            if run_id:
                groups["resumable"].append(item)
        if bucket == "error" and run_id:
            reason = failure_reason(root, run_id, generated_at)
            failure = history_item(summary, reason) | {"reason": reason}
            failures.append({"run_id": run_id, "reason": reason})
            groups["failed"].append(failure)
        if run_id:
            try:
                counts = load_feedback_counts(root, run_id, generated_at)
            except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
                counts = {}
            for key in feedback:
                feedback[key] += int(counts.get(key) or 0)
    latest = summaries[0] if summaries else None
    return {
        "total": len(summaries),
        "resumable": resumable,
        "status": status,
        "mode": mode,
        "stages": stages,
        "feedback": feedback,
        "failures": failures[:5],
        "groups": {key: value[:HISTORY_GROUP_PAYLOAD_LIMIT] for key, value in groups.items()},
        "group_limit": HISTORY_GROUP_PAYLOAD_LIMIT,
        "latest": {
            "run_id": latest.get("run_id"),
            "updated_at": latest.get("updated_at") or latest.get("created_at"),
        } if latest else None,
    }


def build_dashboard_payload(root: Path) -> dict[str, Any]:
    generated_at = now_iso()
    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for run_id in discover_run_ids(root):
        try:
            refreshed.append(refresh_run(root, run_id))
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            skipped.append({"run_id": run_id, "reason": str(exc)})

    active = load_json(active_path(root), {"schema_version": 1, "active_run": None, "status": "inactive"})
    if not isinstance(active, dict):
        active = {"schema_version": 1, "active_run": None, "status": "inactive"}
    active_run = clean_optional(active.get("active_run"))
    current: dict[str, Any] | None = None
    current_issue: str | None = None
    if active_run:
        validate_run_id(active_run)
        if run_path(root, active_run).exists():
            current = load_run_views(root, active_run, generated_at, refresh=False)
        else:
            current_issue = f"active run file is missing: {active_run}"

    all_summaries, issues = load_recent_summaries(root, generated_at, None)
    recent = all_summaries[:DEFAULT_RECENT_LIMIT]
    history = build_history_totals(root, all_summaries, generated_at)
    run_details: dict[str, Any] = {}
    for summary in recent:
        run_id = clean_optional(summary.get("run_id"))
        if not run_id:
            continue
        try:
            run_details[run_id] = load_run_views(root, run_id, generated_at, refresh=False)
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            skipped.append({"run_id": run_id, "reason": str(exc)})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "poll_interval_ms": 2000,
        "root": str(root),
        "active": active,
        "active_run": active_run,
        "current": current,
        "current_issue": current_issue,
        "history": history,
        "recent_runs": recent,
        "run_details": run_details,
        "issues": issues,
        "refresh": {
            "refreshed": refreshed,
            "skipped": skipped,
            "count": len(refreshed),
        },
    }


def dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Phaseharness Dashboard</title>
  <script>
    (function () {
      try {
        var choice = localStorage.getItem("phaseharness-dashboard-theme") || "auto";
        document.documentElement.dataset.themeChoice = choice;
        if (choice === "light" || choice === "dark") {
          document.documentElement.dataset.theme = choice;
        }
        var languageChoice = localStorage.getItem("phaseharness-dashboard-language") || "auto";
        var resolvedLanguage = languageChoice === "ko" || languageChoice === "en"
          ? languageChoice
          : (/^ko\b/i.test(navigator.language || "") ? "ko" : "en");
        document.documentElement.dataset.languageChoice = languageChoice;
        document.documentElement.lang = resolvedLanguage;
      } catch (error) {}
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --color-background: #f6f7f9;
      --color-panel: #ffffff;
      --color-panel-muted: #fbfcfe;
      --color-panel-subtle: #fafbfc;
      --color-border: #d8dde6;
      --color-border-strong: #aeb7c5;
      --color-border-soft: #e6e9ef;
      --color-border-faint: #edf0f4;
      --color-divider: #eef1f5;
      --color-text: #1f2933;
      --color-muted: #667085;
      --color-muted-soft: #98a2b3;
      --color-slate: #3f4b5f;
      --color-code: #334155;
      --color-blue: #2563eb;
      --color-blue-strong: #1d4ed8;
      --color-blue-line: #84a9ff;
      --color-blue-line-soft: #93c5fd;
      --color-blue-focus: #3b82f6;
      --color-blue-soft: #f7faff;
      --color-blue-subtle: #f8fbff;
      --color-green: #16835f;
      --color-green-dot: #10b981;
      --color-green-line: #b7dfcf;
      --color-green-line-strong: #9ed7c1;
      --color-green-soft: #f1fbf7;
      --color-green-subtle: #f5fcf8;
      --color-amber: #b7791f;
      --color-amber-dot: #d59a25;
      --color-amber-line: #ead19a;
      --color-amber-soft: #fff9ea;
      --color-red: #c2413a;
      --color-red-line: #f1bbb6;
      --color-red-line-soft: #f1a7a0;
      --color-red-line-strong: #ee9d97;
      --color-red-soft: #fff2f1;
      --color-red-subtle: #fff8f7;
      --color-neutral-dot: #94a3b8;
      --color-arrow: #c3cad5;
      --color-badge-dot: #cbd5e1;
      --color-track: #edf1f7;
      --color-copy-border: #cbd5e1;
      --color-copy-text: #475569;
      --color-resume-bg: #f5f8ff;
      --color-resume-border: #dbe4ff;
      --color-resume-text: #243b76;
      --color-resume-code: #1d4ed8;
      --shadow-active: 0 0 0 3px rgba(37, 99, 235, 0.12), 0 8px 22px rgba(31, 41, 51, 0.08);
      --shadow-active-soft: 0 0 0 3px rgba(37, 99, 235, 0.1);
      --shadow-border-flow-low: 0 0 0 3px rgba(37, 99, 235, 0.08);
      --shadow-border-flow-high: 0 0 0 5px rgba(37, 99, 235, 0.18);
      --shadow-status-pulse-low: 0 0 0 3px rgba(37, 99, 235, 0.12);
      --shadow-status-pulse-high: 0 0 0 5px rgba(37, 99, 235, 0.2);
      --shadow-neutral-dot: 0 0 0 3px rgba(148, 163, 184, 0.12);
      --shadow-green-dot: 0 0 0 3px rgba(16, 185, 129, 0.12);
      --shadow-red-dot: 0 0 0 3px rgba(194, 65, 58, 0.12);
      --gradient-arrow-flow: linear-gradient(90deg, rgba(37, 99, 235, 0.12), rgba(37, 99, 235, 0.75), rgba(37, 99, 235, 0.12));

      --bg: var(--color-background);
      --panel: var(--color-panel);
      --line: var(--color-border);
      --line-strong: var(--color-border-strong);
      --text: var(--color-text);
      --muted: var(--color-muted);
      --blue: var(--color-blue);
      --green: var(--color-green);
      --amber: var(--color-amber);
      --red: var(--color-red);
      --slate: var(--color-slate);
    }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        color-scheme: dark;
        --color-background: #0f1115;
        --color-panel: #171a21;
        --color-panel-muted: #1d212a;
        --color-panel-subtle: #141820;
        --color-border: #2c3440;
        --color-border-strong: #465364;
        --color-border-soft: #26303b;
        --color-border-faint: #26303b;
        --color-divider: #242c37;
        --color-text: #e5e7eb;
        --color-muted: #9aa4b2;
        --color-muted-soft: #7f8a99;
        --color-slate: #c5cfdd;
        --color-code: #d6e1f0;
        --color-blue: #7aa2ff;
        --color-blue-strong: #9bb7ff;
        --color-blue-line: #5278d8;
        --color-blue-line-soft: #5f8cf0;
        --color-blue-focus: #8bb4ff;
        --color-blue-soft: #16233b;
        --color-blue-subtle: #1d2531;
        --color-green: #65d6a4;
        --color-green-dot: #22c88a;
        --color-green-line: #2f7c62;
        --color-green-line-strong: #3c9a79;
        --color-green-soft: #11271f;
        --color-green-subtle: #12261e;
        --color-amber: #f0b85a;
        --color-amber-dot: #d99a2b;
        --color-amber-line: #8f6a2a;
        --color-amber-soft: #2b2413;
        --color-red: #ff8b82;
        --color-red-line: #8f4542;
        --color-red-line-soft: #a85450;
        --color-red-line-strong: #b85a56;
        --color-red-soft: #311a1b;
        --color-red-subtle: #2d1719;
        --color-neutral-dot: #64748b;
        --color-arrow: #536174;
        --color-badge-dot: #64748b;
        --color-track: #2a3340;
        --color-copy-border: #3b4655;
        --color-copy-text: #b8c2cf;
        --color-resume-bg: #17213a;
        --color-resume-border: #334d86;
        --color-resume-text: #d8e3ff;
        --color-resume-code: #9bb7ff;
        --shadow-active: 0 0 0 3px rgba(122, 162, 255, 0.18), 0 8px 22px rgba(0, 0, 0, 0.22);
        --shadow-active-soft: 0 0 0 3px rgba(122, 162, 255, 0.16);
        --shadow-border-flow-low: 0 0 0 3px rgba(122, 162, 255, 0.14);
        --shadow-border-flow-high: 0 0 0 5px rgba(122, 162, 255, 0.24);
        --shadow-status-pulse-low: 0 0 0 3px rgba(122, 162, 255, 0.18);
        --shadow-status-pulse-high: 0 0 0 5px rgba(122, 162, 255, 0.28);
        --shadow-neutral-dot: 0 0 0 3px rgba(100, 116, 139, 0.2);
        --shadow-green-dot: 0 0 0 3px rgba(34, 200, 138, 0.18);
        --shadow-red-dot: 0 0 0 3px rgba(255, 139, 130, 0.18);
        --gradient-arrow-flow: linear-gradient(90deg, rgba(122, 162, 255, 0.16), rgba(122, 162, 255, 0.78), rgba(122, 162, 255, 0.16));
      }
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --color-background: #0f1115;
      --color-panel: #171a21;
      --color-panel-muted: #1d212a;
      --color-panel-subtle: #141820;
      --color-border: #2c3440;
      --color-border-strong: #465364;
      --color-border-soft: #26303b;
      --color-border-faint: #26303b;
      --color-divider: #242c37;
      --color-text: #e5e7eb;
      --color-muted: #9aa4b2;
      --color-muted-soft: #7f8a99;
      --color-slate: #c5cfdd;
      --color-code: #d6e1f0;
      --color-blue: #7aa2ff;
      --color-blue-strong: #9bb7ff;
      --color-blue-line: #5278d8;
      --color-blue-line-soft: #5f8cf0;
      --color-blue-focus: #8bb4ff;
      --color-blue-soft: #16233b;
      --color-blue-subtle: #1d2531;
      --color-green: #65d6a4;
      --color-green-dot: #22c88a;
      --color-green-line: #2f7c62;
      --color-green-line-strong: #3c9a79;
      --color-green-soft: #11271f;
      --color-green-subtle: #12261e;
      --color-amber: #f0b85a;
      --color-amber-dot: #d99a2b;
      --color-amber-line: #8f6a2a;
      --color-amber-soft: #2b2413;
      --color-red: #ff8b82;
      --color-red-line: #8f4542;
      --color-red-line-soft: #a85450;
      --color-red-line-strong: #b85a56;
      --color-red-soft: #311a1b;
      --color-red-subtle: #2d1719;
      --color-neutral-dot: #64748b;
      --color-arrow: #536174;
      --color-badge-dot: #64748b;
      --color-track: #2a3340;
      --color-copy-border: #3b4655;
      --color-copy-text: #b8c2cf;
      --color-resume-bg: #17213a;
      --color-resume-border: #334d86;
      --color-resume-text: #d8e3ff;
      --color-resume-code: #9bb7ff;
      --shadow-active: 0 0 0 3px rgba(122, 162, 255, 0.18), 0 8px 22px rgba(0, 0, 0, 0.22);
      --shadow-active-soft: 0 0 0 3px rgba(122, 162, 255, 0.16);
      --shadow-border-flow-low: 0 0 0 3px rgba(122, 162, 255, 0.14);
      --shadow-border-flow-high: 0 0 0 5px rgba(122, 162, 255, 0.24);
      --shadow-status-pulse-low: 0 0 0 3px rgba(122, 162, 255, 0.18);
      --shadow-status-pulse-high: 0 0 0 5px rgba(122, 162, 255, 0.28);
      --shadow-neutral-dot: 0 0 0 3px rgba(100, 116, 139, 0.2);
      --shadow-green-dot: 0 0 0 3px rgba(34, 200, 138, 0.18);
      --shadow-red-dot: 0 0 0 3px rgba(255, 139, 130, 0.18);
      --gradient-arrow-flow: linear-gradient(90deg, rgba(122, 162, 255, 0.16), rgba(122, 162, 255, 0.78), rgba(122, 162, 255, 0.16));
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 20px 28px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--color-panel);
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .subtitle {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      max-width: 900px;
      word-break: break-word;
    }
    .header-actions {
      display: grid;
      justify-items: end;
      gap: 9px;
      min-width: 260px;
    }
    .header-controls {
      display: flex;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 8px;
    }
    .theme-control,
    .language-control {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px;
      background: var(--color-panel-muted);
    }
    .theme-btn,
    .language-btn {
      appearance: none;
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-size: 11px;
      font-weight: 600;
      line-height: 1;
      padding: 6px 9px;
    }
    .theme-btn:hover,
    .language-btn:hover {
      color: var(--text);
      background: var(--color-blue-subtle);
    }
    .theme-btn.selected,
    .language-btn.selected {
      color: var(--blue);
      background: var(--color-blue-soft);
    }
    .theme-btn:focus-visible,
    .language-btn:focus-visible {
      outline: 2px solid var(--color-blue-focus);
      outline-offset: 2px;
    }
    main {
      width: min(1440px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 20px 0 32px;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    main > .stack { min-width: 0; }
    .wide-section {
      grid-column: 1 / -1;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .section-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h2 {
      margin: 0;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .elapsed-counter {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      font-size: 12px;
      line-height: 1.4;
      min-width: 0;
      max-width: 100%;
    }
    .elapsed-label {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 420px;
    }
    .elapsed-time {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--color-panel-muted);
      color: var(--slate);
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      line-height: 1;
      padding: 5px 8px;
      white-space: nowrap;
    }
    .elapsed-time::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: currentColor;
    }
    .elapsed-time.running {
      border-color: var(--color-blue-line-soft);
      background: var(--color-blue-soft);
      color: var(--blue);
    }
    .elapsed-time.done {
      border-color: var(--color-green-line);
      background: var(--color-green-soft);
      color: var(--green);
    }
    .elapsed-time.done::before {
      background: var(--color-green-dot);
    }
    .elapsed-time.idle {
      color: var(--slate);
    }
    .section-body { padding: 16px; }
    .flow-board {
      background: var(--color-panel-muted);
      border-radius: 8px;
      border: 1px solid var(--color-border-soft);
      padding: 18px;
    }
    .flow-inner {
      display: grid;
      gap: 18px;
      min-width: 0;
    }
    .workflow-context {
      border: 1px solid var(--color-border-soft);
      border-radius: 8px;
      background: var(--color-panel-muted);
      padding: 10px 12px;
      margin-bottom: 12px;
      display: grid;
      gap: 8px;
    }
    .workflow-context-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }
    .workflow-run {
      min-width: 0;
      display: grid;
      gap: 7px;
    }
    .workflow-title-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .workflow-run-id {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 15px;
      line-height: 1.25;
      font-weight: 700;
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .workflow-run-line {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      align-items: center;
    }
    .meta-chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      background: var(--color-panel);
      color: var(--slate);
      white-space: nowrap;
      display: inline-flex;
      align-items: baseline;
      gap: 5px;
      max-width: 100%;
    }
    .meta-chip span {
      color: var(--muted);
      font-size: 11px;
    }
    .meta-chip strong {
      font-size: 12px;
      font-weight: 500;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .workflow-resume {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) 28px;
      gap: 8px;
      align-items: center;
      padding: 8px 9px;
    }
    .workflow-resume span {
      font-weight: 600;
    }
    .run-result {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 8px;
      font-size: 12px;
      line-height: 1.45;
      background: var(--color-panel);
    }
    .run-result-title {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 10px;
    }
    .run-result-title strong {
      font-size: 12px;
    }
    .run-result-title span {
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }
    .run-result-reason {
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .run-result-list {
      margin: 0;
      padding-left: 16px;
      display: grid;
      gap: 3px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .run-result-list li {
      padding-left: 2px;
    }
    .run-result.error {
      border-color: var(--color-red-line);
      background: var(--color-red-subtle);
    }
    .run-result.error .run-result-title strong {
      color: var(--red);
    }
    .run-result.completed {
      border-color: var(--color-green-line);
      background: var(--color-green-subtle);
    }
    .run-result.completed .run-result-title strong {
      color: var(--green);
    }
    .review-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .review-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--color-panel);
      padding: 12px;
      display: grid;
      gap: 10px;
      min-width: 0;
    }
    .review-card h3 {
      margin: 0;
      font-size: 13px;
      line-height: 1.2;
    }
    .review-list {
      display: grid;
      gap: 9px;
    }
    .review-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
    }
    .review-label {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .review-label strong {
      font-size: 12px;
      line-height: 1.25;
    }
    .review-label span {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .review-value {
      font-size: 12px;
      font-weight: 700;
      line-height: 1.2;
      color: var(--slate);
      white-space: nowrap;
    }
    .review-value.fail,
    .review-value.error {
      color: var(--red);
    }
    .review-value.pass,
    .review-value.warn {
      color: var(--green);
    }
    .stage-lane {
      --stage-gap: clamp(32px, 3vw, 58px);
      display: grid;
      grid-template-columns: repeat(5, minmax(104px, 1fr));
      gap: var(--stage-gap);
      align-items: center;
      position: relative;
    }
    .node {
      position: relative;
      min-width: 0;
      height: 90px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: var(--color-panel);
      padding: 10px;
      z-index: 1;
      padding-bottom: 28px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 4px;
    }
    .node::after {
      content: "";
      position: absolute;
      top: 50%;
      right: calc(-1 * (var(--stage-gap) - 13px));
      width: calc(var(--stage-gap) - 24px);
      height: 2px;
      background: var(--color-arrow);
      transform: translateY(-50%);
      z-index: 2;
    }
    .node::before {
      content: "";
      position: absolute;
      top: calc(50% - 5px);
      right: calc(-1 * (var(--stage-gap) - 6px));
      border-left: 8px solid var(--color-arrow);
      border-top: 5px solid transparent;
      border-bottom: 5px solid transparent;
      z-index: 3;
    }
    .node.last::before,
    .node.last::after { display: none; }
    .node.active { border-color: var(--blue); box-shadow: var(--shadow-active); }
    .node.completed { border-color: var(--color-green-line-strong); }
    .node.running { border-color: var(--blue); }
    .node.error, .node.failed { border-color: var(--color-red-line-strong); }
    .node.pending { border-style: dashed; }
    .node.flowing {
      border-color: var(--blue);
      animation: active-border 1.8s ease-in-out infinite;
    }
    .node.flowing::after {
      background: var(--gradient-arrow-flow);
      background-size: 200% 100%;
      animation: arrow-flow 1.8s linear infinite;
    }
    .node.flowing::before {
      border-left-color: var(--blue);
      animation: arrow-pulse 1.3s ease-in-out infinite;
    }
    @keyframes active-border {
      0%, 100% { box-shadow: var(--shadow-border-flow-low); }
      50% { box-shadow: var(--shadow-border-flow-high); }
    }
    @keyframes arrow-flow {
      from { background-position: 200% 0; }
      to { background-position: 0 0; }
    }
    @keyframes arrow-pulse {
      0%, 100% { opacity: 0.55; }
      50% { opacity: 1; }
    }
    @keyframes status-pulse {
      0%, 100% { box-shadow: var(--shadow-status-pulse-low); }
      50% { box-shadow: var(--shadow-status-pulse-high); }
    }
    .node-title {
      font-size: 12px;
      font-weight: 700;
      margin: 0;
      min-height: 16px;
      line-height: 1.25;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .node-meta {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .node-duration {
      width: max-content;
      max-width: 100%;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      padding: 0;
      min-height: 11px;
      margin: 0;
      overflow-wrap: anywhere;
    }
    .node-duration.empty {
      visibility: hidden;
    }
    .node-duration.running {
      color: var(--slate);
      font-weight: 600;
    }
    .phase-lane {
      border-top: 1px solid var(--color-border-soft);
      padding-top: 16px;
      display: grid;
      gap: 10px;
    }
    .phase-title {
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .phase-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
    }
    .phase-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: var(--color-panel);
    }
    .phase-card.active { border-color: var(--blue); box-shadow: var(--shadow-active-soft); }
    .phase-name { font-size: 13px; font-weight: 700; margin-bottom: 4px; }
    .phase-meta { font-size: 12px; color: var(--muted); line-height: 1.4; }
    .phase-summary {
      margin-top: 6px;
      font-size: 12px;
      color: var(--slate);
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      width: max-content;
      max-width: 100%;
      min-height: 18px;
      padding: 0;
      margin-top: 8px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .node .badge {
      position: absolute;
      left: 10px;
      bottom: 8px;
      margin: 0;
    }
    .badge::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--color-badge-dot);
      box-shadow: var(--shadow-neutral-dot);
      flex: 0 0 auto;
    }
    .badge.completed { color: var(--green); }
    .badge.completed::before {
      background: var(--color-green-dot);
      box-shadow: var(--shadow-green-dot);
    }
    .badge.running { color: var(--blue); }
    .badge.running::before {
      background: var(--blue);
      animation: status-pulse 1.8s ease-in-out infinite;
    }
    .badge.error, .badge.failed { color: var(--red); }
    .badge.error::before,
    .badge.failed::before {
      background: var(--red);
      box-shadow: var(--shadow-red-dot);
    }
    .badge.pending { color: var(--muted); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: var(--color-panel);
    }
    .metric-label { font-size: 11px; color: var(--muted); margin-bottom: 5px; }
    .metric-value { font-size: 14px; font-weight: 700; overflow-wrap: anywhere; }
    .stack {
      display: grid;
      gap: 14px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      padding: 9px 8px;
      border-bottom: 1px solid var(--color-border-faint);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th { color: var(--muted); font-weight: 600; }
    tr.run-row {
      cursor: pointer;
    }
    tr.run-row:hover {
      background: var(--color-blue-subtle);
    }
    tr.run-row.selected {
      background: var(--color-blue-soft);
      box-shadow: inset 3px 0 0 var(--blue);
    }
    .empty {
      color: var(--muted);
      font-size: 13px;
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--color-panel-subtle);
    }
    .path-list {
      display: grid;
      gap: 7px;
      font-size: 12px;
    }
    .path-row {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      gap: 8px;
      align-items: baseline;
    }
    .check-list .path-row {
      grid-template-columns: 120px minmax(0, 1fr);
    }
    .history-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .overview-layout {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .overview-chart {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--color-panel-muted);
      display: grid;
      align-content: center;
      gap: 14px;
      min-width: 0;
    }
    .overview-chart-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      font-size: 12px;
      color: var(--muted);
    }
    .overview-chart-head strong {
      color: var(--text);
      font-size: 13px;
    }
    .overview-distribution {
      display: grid;
      gap: 9px;
    }
    .overview-status-row {
      display: grid;
      grid-template-columns: minmax(104px, 150px) minmax(0, 1fr) minmax(74px, auto);
      gap: 10px;
      align-items: center;
      min-width: 0;
    }
    .overview-status-label {
      display: flex;
      align-items: center;
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      min-width: 0;
    }
    .overview-status-label span:last-child {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .overview-track {
      height: 10px;
      border-radius: 999px;
      background: var(--color-track);
      overflow: hidden;
      min-width: 0;
    }
    .overview-fill {
      height: 100%;
      min-width: 0;
      border-radius: 999px;
      background: var(--color-neutral-dot);
    }
    .overview-fill.active { background: var(--blue); }
    .overview-fill.completed { background: var(--green); }
    .overview-fill.waiting { background: var(--color-amber-dot); }
    .overview-fill.error { background: var(--red); }
    .overview-fill.other { background: var(--color-neutral-dot); }
    .overview-status-value {
      display: grid;
      grid-template-columns: auto 34px;
      justify-content: end;
      gap: 8px;
      align-items: baseline;
      white-space: nowrap;
    }
    .overview-status-value strong { font-size: 13px; line-height: 1; }
    .overview-status-value span {
      color: var(--muted);
      font-size: 11px;
      text-align: right;
    }
    .overview-dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--color-neutral-dot);
    }
    .overview-dot.active { background: var(--blue); }
    .overview-dot.completed { background: var(--green); }
    .overview-dot.waiting { background: var(--color-amber-dot); }
    .overview-dot.error { background: var(--red); }
    .overview-dot.other { background: var(--color-neutral-dot); }
    .history-stat {
      appearance: none;
      width: 100%;
      min-height: 72px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: var(--color-panel);
      color: inherit;
      font: inherit;
      text-align: left;
      display: grid;
      align-content: start;
      gap: 4px;
    }
    .history-stat.clickable {
      cursor: pointer;
    }
    .history-stat.clickable:hover:not(:disabled),
    .history-stat.selected {
      border-color: var(--color-blue-line);
      background: var(--color-blue-soft);
    }
    .history-stat.danger.selected {
      border-color: var(--color-red-line-soft);
      background: var(--color-red-subtle);
    }
    .history-stat:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .history-stat:focus-visible,
    .history-detail-item:focus-visible,
    .recent-filter:focus-visible {
      outline: 2px solid var(--color-blue-focus);
      outline-offset: 2px;
    }
    .history-label {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.25;
    }
    .history-value {
      font-size: 20px;
      line-height: 1.1;
      font-weight: 700;
    }
    .history-action {
      margin-top: 2px;
      color: var(--blue);
      font-size: 11px;
      font-weight: 600;
      line-height: 1.2;
    }
    .history-stat.danger .history-action {
      color: var(--red);
    }
    .history-stat:disabled .history-action {
      color: var(--muted);
    }
    .history-action::after {
      content: " ->";
    }
    .history-stat:disabled .history-action::after {
      content: "";
    }
    .history-issues {
      display: grid;
      gap: 0;
      margin-top: 12px;
    }
    .recent-filter-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }
    .recent-filter {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--color-panel);
      color: var(--slate);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      line-height: 1;
      padding: 7px 10px;
    }
    .recent-filter:hover,
    .recent-filter.selected {
      border-color: var(--color-blue-line);
      background: var(--color-blue-soft);
      color: var(--blue);
    }
    .recent-filter.danger.selected {
      border-color: var(--color-red-line-soft);
      background: var(--color-red-subtle);
      color: var(--red);
    }
    .run-detail {
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .history-detail {
      display: grid;
      gap: 8px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--color-divider);
    }
    .history-detail-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
      font-size: 12px;
    }
    .history-detail-head strong {
      font-size: 12px;
    }
    .history-detail-head span {
      color: var(--muted);
      font-size: 11px;
    }
    .history-detail-list {
      display: grid;
      gap: 6px;
      max-height: 360px;
      overflow-y: auto;
      padding-right: 2px;
    }
    .history-detail-item {
      appearance: none;
      width: 100%;
      border: 1px solid var(--color-border);
      border-radius: 8px;
      padding: 8px 9px;
      background: var(--color-panel);
      color: inherit;
      cursor: pointer;
      font: inherit;
      text-align: left;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    .history-detail-item:hover,
    .history-detail-item.selected {
      border-color: var(--color-blue-line);
      background: var(--color-blue-soft);
    }
    .history-detail-item.danger:hover,
    .history-detail-item.danger.selected {
      border-color: var(--color-red-line-soft);
      background: var(--color-red-subtle);
    }
    .history-detail-main {
      min-width: 0;
      display: grid;
      gap: 3px;
    }
    .history-detail-main code {
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .history-detail-item.danger .history-detail-main code {
      color: var(--red);
    }
    .history-detail-main span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .history-detail-meta {
      display: grid;
      justify-items: end;
      gap: 3px;
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }
    .history-detail-status {
      color: var(--text);
      font-weight: 700;
    }
    .history-detail-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
      padding-left: 8px;
    }
    .history-more-btn {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--color-panel);
      color: var(--slate);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      line-height: 1;
      padding: 7px 10px;
    }
    .history-more-btn:hover {
      border-color: var(--color-blue-line);
      background: var(--color-blue-soft);
      color: var(--blue);
    }
    .history-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 12px;
      padding: 7px 0;
      border-top: 1px solid var(--color-divider);
    }
    .history-row span {
      color: var(--muted);
    }
    .resume-hint {
      padding: 9px 10px;
      border: 1px solid var(--color-resume-border);
      border-radius: 8px;
      background: var(--color-resume-bg);
      color: var(--color-resume-text);
      font-size: 12px;
      line-height: 1.45;
    }
    .resume-hint code {
      color: var(--color-resume-code);
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .resume-action {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 28px;
      gap: 8px;
      align-items: start;
    }
    .copy-btn {
      width: 28px;
      height: 28px;
      border: 1px solid var(--color-copy-border);
      border-radius: 7px;
      background: var(--color-panel);
      color: var(--color-copy-text);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      flex: 0 0 auto;
    }
    .copy-btn:hover {
      border-color: var(--color-blue-line-soft);
      color: var(--blue);
      background: var(--color-blue-subtle);
    }
    .copy-btn.copied {
      border-color: var(--color-green-line-strong);
      color: var(--green);
      background: var(--color-green-soft);
    }
    .copy-btn svg {
      width: 15px;
      height: 15px;
      stroke-width: 2;
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      color: var(--color-code);
    }
    @media (max-width: 980px) {
      header { flex-direction: column; }
      .header-actions { justify-items: flex-start; min-width: 0; width: 100%; }
      .header-controls { justify-content: flex-start; }
      .theme-control, .language-control { max-width: 100%; }
      .section-head { flex-wrap: wrap; }
      .elapsed-counter { justify-content: flex-start; width: 100%; }
      .elapsed-label { max-width: 100%; }
      main { width: calc(100vw - 28px); grid-template-columns: 1fr; padding: 14px 0; }
      .stage-lane { grid-template-columns: 1fr; gap: 10px; }
      .stage-lane .node::before, .stage-lane .node::after { display: none; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .history-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .overview-layout { grid-template-columns: 1fr; }
      .overview-status-row { grid-template-columns: minmax(86px, 112px) minmax(0, 1fr) minmax(64px, auto); }
      .review-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1 id="app-title">Phaseharness Dashboard</h1>
      <div class="subtitle" id="subtitle">Loading run state...</div>
    </div>
    <div class="header-actions">
      <div class="header-controls">
        <div class="language-control" id="language-control" aria-label="Language"></div>
        <div class="theme-control" id="theme-control" aria-label="Theme"></div>
      </div>
    </div>
  </header>
  <main>
    <section class="wide-section">
      <div class="section-head"><h2 id="run-overview-title">Run Overview</h2></div>
      <div class="section-body">
        <div id="history"></div>
      </div>
    </section>
    <div class="stack">
      <section>
        <div class="section-head">
          <h2 id="current-workflow-title">Current Workflow</h2>
          <span class="elapsed-counter" id="elapsed-counter" aria-live="polite">idle</span>
        </div>
        <div class="section-body">
          <div class="workflow-context" id="workflow-context"></div>
          <div class="flow-board">
            <div class="flow-inner" id="flow"></div>
          </div>
          <div class="metrics" id="metrics"></div>
        </div>
      </section>
      <section id="run-review-section" hidden>
        <div class="section-head"><h2 id="run-review-title">Run Review</h2></div>
        <div class="section-body" id="run-review"></div>
      </section>
      <section>
        <div class="section-head"><h2 id="outputs-title">Outputs</h2></div>
        <div class="section-body" id="outputs"></div>
      </section>
    </div>
    <section class="wide-section" id="runs-section">
      <div class="section-head"><h2 id="runs-title">Runs</h2></div>
      <div class="section-body" id="recent"></div>
    </section>
  </main>
  <script>
    const STAGES = ["clarify", "context_gather", "plan", "generate", "evaluate"];
    const STAGE_LABELS = {
      clarify: "Clarify",
      context_gather: "Context Gather",
      plan: "Plan",
      generate: "Generate",
      evaluate: "Evaluate"
    };
    let pollTimer = null;
    let elapsedTimer = null;
    let latestPayload = null;
    let selectedRunId = null;
    let historyFilter = "all";
    let historyVisibleCounts = {};
    const HISTORY_INITIAL_VISIBLE = 10;
    const HISTORY_PAGE_SIZE = 10;
    const HISTORY_FILTER_LABELS = {
      all: "history.all",
      running: "history.running",
      resumable: "history.resumable",
      failed: "history.failed"
    };
    const THEME_STORAGE_KEY = "phaseharness-dashboard-theme";
    const THEME_OPTIONS = [
      ["auto", "theme.auto"],
      ["light", "theme.light"],
      ["dark", "theme.dark"]
    ];
    const LANGUAGE_STORAGE_KEY = "phaseharness-dashboard-language";
    const LANGUAGE_OPTIONS = [
      ["auto", "language.auto"],
      ["en", "language.en"],
      ["ko", "language.ko"]
    ];
    const TRANSLATIONS = {
      en: {
        "app.title": "Phaseharness Dashboard",
        "app.loading": "Loading run state...",
        "section.runOverview": "Run Overview",
        "section.currentWorkflow": "Current Workflow",
        "section.runReview": "Run Review",
        "section.outputs": "Outputs",
        "section.runs": "Runs",
        "control.theme": "Theme",
        "control.language": "Language",
        "theme.auto": "Auto",
        "theme.light": "Light",
        "theme.dark": "Dark",
        "language.auto": "Auto",
        "language.en": "EN",
        "language.ko": "KO",
        "stage.clarify": "Clarify",
        "stage.context_gather": "Context Gather",
        "stage.plan": "Plan",
        "stage.generate": "Generate",
        "stage.evaluate": "Evaluate",
        "status.active": "active",
        "status.running": "running",
        "status.pending": "pending",
        "status.completed": "completed",
        "status.error": "error",
        "status.failed": "failed",
        "status.fail": "fail",
        "status.pass": "pass",
        "status.warn": "warn",
        "status.waiting_user": "waiting",
        "status.committed": "committed",
        "status.no_changes": "no changes",
        "status.skipped": "skipped",
        "status.inactive": "inactive",
        "status.unknown": "unknown",
        "empty.noActiveRun": "No active run",
        "empty.noActivePhaseharnessRun": "No active phaseharness run.",
        "empty.noGeneratePhaseFiles": "No generate phase files found.",
        "empty.noRunsInGroup": "No runs in this group.",
        "empty.noRunHistory": "No Phaseharness run history.",
        "empty.noActiveOutputs": "No active outputs.",
        "empty.noRecentRuns": "No recent runs.",
        "empty.noRunsInFilter": "No runs in this filter.",
        "elapsed.idle": "idle",
        "elapsed.duration": "duration",
        "elapsed.running": "running",
        "elapsed.total": "total",
        "common.none": "none",
        "common.unknown": "unknown",
        "common.done": "done",
        "copy.resume": "Copy resume request",
        "copy.copied": "Copied",
        "copy.failed": "Copy failed",
        "error.runDetailLoad": "Run detail load failed",
        "error.dashboardRefresh": "Dashboard refresh failed",
        "phase.generatePhases": "Generate phases",
        "file.empty": "file empty",
        "file.missing": "file missing",
        "result.whyFailed": "Why it failed",
        "result.noFailureReason": "No failure reason recorded.",
        "result.completed": "Completed",
        "result.runCompleted": "Run completed",
        "result.evaluation": "Evaluation",
        "resume.label": "Resume",
        "meta.mode": "Mode",
        "meta.branch": "Branch",
        "metric.stage": "Stage",
        "metric.phase": "Phase",
        "metric.loop": "Loop",
        "metric.evaluation": "Evaluation",
        "history.all": "All runs",
        "history.running": "Running now",
        "history.resumable": "Can continue",
        "history.failed": "Failed",
        "history.showing": "Showing in Runs",
        "history.viewList": "View list",
        "history.noRuns": "No runs",
        "history.showMore": "Show 10 more",
        "history.left": "left",
        "history.collapse": "Collapse",
        "overview.statusDistribution": "Status distribution",
        "overview.total": "total",
        "overview.error": "Failed",
        "overview.active": "Running",
        "overview.waiting": "Waiting",
        "overview.completed": "Completed",
        "overview.other": "Other",
        "output.kind": "Kind",
        "output.name": "Name",
        "output.state": "State",
        "output.path": "Path",
        "kind.artifact": "artifact",
        "kind.phase": "phase",
        "fileState.ok": "ok",
        "fileState.empty": "empty",
        "fileState.missing": "missing",
        "review.checks": "Review Checks",
        "review.feedback": "Feedback",
        "review.requirements": "Requirements",
        "review.requirementsDesc": "Evaluation against the original request.",
        "review.guidance": "Guidance",
        "review.guidanceDesc": "Whether harness guidance and constraints were followed.",
        "review.checksLabel": "Checks",
        "review.checksDesc": "Validation commands detected in plan/evaluate artifacts.",
        "review.failed": "Failed",
        "review.failedDesc": "Validation commands recorded as failed.",
        "review.evaluateFailures": "Evaluate failures",
        "review.evaluateFailuresDesc": "Times evaluate produced a fail result.",
        "review.followups": "Follow-ups",
        "review.followupsDesc": "Follow-up phases created after evaluation.",
        "review.loopRetries": "Loop retries",
        "review.loopRetriesDesc": "Retry loops used by the run.",
        "review.postCompletion": "Post completion",
        "review.postCompletionDesc": "Explicit feedback after completion.",
        "runs.run": "Run",
        "runs.status": "Status",
        "runs.stage": "Stage",
        "runs.phase": "Phase",
        "runs.updated": "Updated",
        "runs.resume": "Resume",
        "filter.all": "All",
        "filter.running": "Running",
        "filter.resumable": "Can continue",
        "filter.failed": "Failed"
      },
      ko: {
        "app.title": "Phaseharness 대시보드",
        "app.loading": "run 상태를 불러오는 중...",
        "section.runOverview": "Run 개요",
        "section.currentWorkflow": "현재 Workflow",
        "section.runReview": "Run 리뷰",
        "section.outputs": "결과물",
        "section.runs": "Runs",
        "control.theme": "테마",
        "control.language": "언어",
        "theme.auto": "자동",
        "theme.light": "라이트",
        "theme.dark": "다크",
        "language.auto": "자동",
        "language.en": "EN",
        "language.ko": "KO",
        "stage.clarify": "Clarify",
        "stage.context_gather": "Context Gather",
        "stage.plan": "Plan",
        "stage.generate": "Generate",
        "stage.evaluate": "Evaluate",
        "status.active": "진행 중",
        "status.running": "진행 중",
        "status.pending": "대기",
        "status.completed": "완료",
        "status.error": "오류",
        "status.failed": "실패",
        "status.fail": "실패",
        "status.pass": "통과",
        "status.warn": "주의",
        "status.waiting_user": "대기 중",
        "status.committed": "커밋됨",
        "status.no_changes": "변경 없음",
        "status.skipped": "건너뜀",
        "status.inactive": "비활성",
        "status.unknown": "알 수 없음",
        "empty.noActiveRun": "진행 중인 run 없음",
        "empty.noActivePhaseharnessRun": "진행 중인 phaseharness run이 없습니다.",
        "empty.noGeneratePhaseFiles": "generate phase 파일이 없습니다.",
        "empty.noRunsInGroup": "이 그룹에 run이 없습니다.",
        "empty.noRunHistory": "Phaseharness run 히스토리가 없습니다.",
        "empty.noActiveOutputs": "진행 중인 결과물이 없습니다.",
        "empty.noRecentRuns": "최근 run이 없습니다.",
        "empty.noRunsInFilter": "이 필터에 해당하는 run이 없습니다.",
        "elapsed.idle": "대기",
        "elapsed.duration": "소요 시간",
        "elapsed.running": "진행 중",
        "elapsed.total": "전체",
        "common.none": "없음",
        "common.unknown": "알 수 없음",
        "common.done": "완료",
        "copy.resume": "재개 요청 복사",
        "copy.copied": "복사됨",
        "copy.failed": "복사 실패",
        "error.runDetailLoad": "Run 상세 로드 실패",
        "error.dashboardRefresh": "대시보드 갱신 실패",
        "phase.generatePhases": "Generate phases",
        "file.empty": "파일 비어 있음",
        "file.missing": "파일 없음",
        "result.whyFailed": "실패 이유",
        "result.noFailureReason": "기록된 실패 이유가 없습니다.",
        "result.completed": "완료",
        "result.runCompleted": "Run 완료",
        "result.evaluation": "평가",
        "resume.label": "재개",
        "meta.mode": "모드",
        "meta.branch": "브랜치",
        "metric.stage": "단계",
        "metric.phase": "Phase",
        "metric.loop": "Loop",
        "metric.evaluation": "평가",
        "history.all": "전체 run",
        "history.running": "진행 중",
        "history.resumable": "재개 가능",
        "history.failed": "실패",
        "history.showing": "Runs에 표시 중",
        "history.viewList": "목록 보기",
        "history.noRuns": "없음",
        "history.showMore": "10개 더 보기",
        "history.left": "개 남음",
        "history.collapse": "접기",
        "overview.statusDistribution": "상태 분포",
        "overview.total": "전체",
        "overview.error": "실패",
        "overview.active": "진행 중",
        "overview.waiting": "대기",
        "overview.completed": "완료",
        "overview.other": "기타",
        "output.kind": "종류",
        "output.name": "이름",
        "output.state": "상태",
        "output.path": "경로",
        "kind.artifact": "artifact",
        "kind.phase": "phase",
        "fileState.ok": "ok",
        "fileState.empty": "비어 있음",
        "fileState.missing": "없음",
        "review.checks": "Review Checks",
        "review.feedback": "Feedback",
        "review.requirements": "요구사항",
        "review.requirementsDesc": "원래 요청 기준 평가 결과입니다.",
        "review.guidance": "가이드",
        "review.guidanceDesc": "harness 지침과 제약을 지켰는지 확인합니다.",
        "review.checksLabel": "검증",
        "review.checksDesc": "plan/evaluate 산출물에서 찾은 검증 명령 수입니다.",
        "review.failed": "실패",
        "review.failedDesc": "실패로 기록된 검증 명령 수입니다.",
        "review.evaluateFailures": "Evaluate 실패",
        "review.evaluateFailuresDesc": "evaluate 단계에서 fail로 잡힌 횟수입니다.",
        "review.followups": "Follow-up",
        "review.followupsDesc": "평가 후 생성된 follow-up phase 수입니다.",
        "review.loopRetries": "재시도",
        "review.loopRetriesDesc": "run에서 사용한 retry loop 횟수입니다.",
        "review.postCompletion": "완료 후 피드백",
        "review.postCompletionDesc": "완료 후 명시적으로 남긴 피드백 수입니다.",
        "runs.run": "Run",
        "runs.status": "상태",
        "runs.stage": "단계",
        "runs.phase": "Phase",
        "runs.updated": "갱신",
        "runs.resume": "재개",
        "filter.all": "전체",
        "filter.running": "진행 중",
        "filter.resumable": "재개 가능",
        "filter.failed": "실패"
      }
    };
    let currentLanguage = "en";

    function normalizeLanguageChoice(choice) {
      return ["auto", "en", "ko"].includes(choice) ? choice : "auto";
    }
    function browserLanguage() {
      return /^ko\b/i.test(navigator.language || "") ? "ko" : "en";
    }
    function storedLanguageChoice() {
      try {
        return normalizeLanguageChoice(localStorage.getItem(LANGUAGE_STORAGE_KEY) || "auto");
      } catch (error) {
        return "auto";
      }
    }
    function resolveLanguageChoice(choice) {
      const normalized = normalizeLanguageChoice(choice);
      return normalized === "auto" ? browserLanguage() : normalized;
    }
    function t(key, values = {}, fallback = key) {
      let output = TRANSLATIONS[currentLanguage]?.[key] || TRANSLATIONS.en[key] || fallback;
      for (const [name, value] of Object.entries(values)) {
        output = output.replaceAll(`{${name}}`, String(value));
      }
      return output;
    }
    function renderLanguageControl() {
      const target = document.getElementById("language-control");
      if (!target) return;
      target.innerHTML = LANGUAGE_OPTIONS.map(([value, labelKey]) => (
        `<button type="button" class="language-btn" data-language-option="${value}" aria-pressed="false" onclick="applyLanguageChoice('${value}')">${t(labelKey)}</button>`
      )).join("");
    }
    function updateLanguageControl(choice) {
      const current = normalizeLanguageChoice(choice || storedLanguageChoice());
      document.querySelectorAll("[data-language-option]").forEach(button => {
        const selected = button.dataset.languageOption === current;
        button.classList.toggle("selected", selected);
        button.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }
    function localizeStaticLabels() {
      document.title = t("app.title");
      const labels = {
        "app-title": "app.title",
        "subtitle": "app.loading",
        "run-overview-title": "section.runOverview",
        "current-workflow-title": "section.currentWorkflow",
        "run-review-title": "section.runReview",
        "outputs-title": "section.outputs",
        "runs-title": "section.runs"
      };
      for (const [id, key] of Object.entries(labels)) {
        const element = document.getElementById(id);
        if (element && (id !== "subtitle" || !latestPayload)) element.textContent = t(key);
      }
      document.getElementById("theme-control")?.setAttribute("aria-label", t("control.theme"));
      document.getElementById("language-control")?.setAttribute("aria-label", t("control.language"));
    }
    function applyLanguageChoice(choice) {
      const normalized = normalizeLanguageChoice(choice);
      currentLanguage = resolveLanguageChoice(normalized);
      try {
        localStorage.setItem(LANGUAGE_STORAGE_KEY, normalized);
      } catch (error) {}
      document.documentElement.dataset.languageChoice = normalized;
      document.documentElement.lang = currentLanguage;
      localizeStaticLabels();
      renderLanguageControl();
      updateLanguageControl(normalized);
      renderThemeControl();
      updateThemeControl(storedThemeChoice());
      if (latestPayload) renderDashboard(latestPayload);
    }
    function initLanguageControl() {
      renderLanguageControl();
      applyLanguageChoice(storedLanguageChoice());
    }
    function stageLabel(stage) {
      return t(`stage.${stage}`, {}, text(stage, ""));
    }
    function displayStatus(status, fallback = "common.none") {
      const value = text(status, "").toLowerCase();
      return value ? t(`status.${value}`, {}, value) : t(fallback);
    }
    function displayFileState(state) {
      const value = state || "missing";
      return t(`fileState.${value}`, {}, value);
    }
    function displayKind(kind) {
      return t(`kind.${kind}`, {}, kind);
    }
    function hiddenCountLabel(count) {
      return currentLanguage === "ko" ? `${count}${t("history.left")}` : `${count} ${t("history.left")}`;
    }

    function normalizeThemeChoice(choice) {
      return ["auto", "light", "dark"].includes(choice) ? choice : "auto";
    }
    function storedThemeChoice() {
      try {
        return normalizeThemeChoice(localStorage.getItem(THEME_STORAGE_KEY) || "auto");
      } catch (error) {
        return "auto";
      }
    }
    function renderThemeControl() {
      const target = document.getElementById("theme-control");
      if (!target) return;
      target.innerHTML = THEME_OPTIONS.map(([value, labelKey]) => (
        `<button type="button" class="theme-btn" data-theme-option="${value}" aria-pressed="false" onclick="applyThemeChoice('${value}')">${t(labelKey)}</button>`
      )).join("");
    }
    function updateThemeControl(choice) {
      const current = normalizeThemeChoice(choice || storedThemeChoice());
      document.querySelectorAll("[data-theme-option]").forEach(button => {
        const selected = button.dataset.themeOption === current;
        button.classList.toggle("selected", selected);
        button.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }
    function applyThemeChoice(choice) {
      const normalized = normalizeThemeChoice(choice);
      try {
        localStorage.setItem(THEME_STORAGE_KEY, normalized);
      } catch (error) {}
      document.documentElement.dataset.themeChoice = normalized;
      if (normalized === "auto") {
        document.documentElement.removeAttribute("data-theme");
      } else {
        document.documentElement.dataset.theme = normalized;
      }
      updateThemeControl(normalized);
    }
    function initThemeControl() {
      renderThemeControl();
      applyThemeChoice(storedThemeChoice());
    }
    function text(value, fallback = "none") {
      return value === undefined || value === null || value === "" ? fallback : String(value);
    }
    function cls(status) {
      const value = text(status, "pending").toLowerCase();
      if (["active", "running"].includes(value)) return "running";
      if (["completed", "pass", "warn", "committed", "no_changes", "skipped"].includes(value)) return "completed";
      if (["error", "failed", "fail"].includes(value)) return "error";
      return "pending";
    }
    function escapeHtml(value) {
      return text(value, "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function metaChip(label, value) {
      return `<span class="meta-chip"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></span>`;
    }
    function badge(status) {
      return `<span class="badge ${cls(status)}">${escapeHtml(displayStatus(status, "status.pending"))}</span>`;
    }
    function fileState(info) {
      if (!info) return "missing";
      if (info.nonempty) return "ok";
      if (info.exists) return "empty";
      return "missing";
    }
    function pad2(value) {
      return String(value).padStart(2, "0");
    }
    function formatLocalDateTime(value) {
      const raw = text(value, "");
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) return raw || t("common.unknown");
      return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
    }
    function secondsSince(value) {
      const time = Date.parse(text(value, ""));
      if (!Number.isFinite(time)) return null;
      return Math.max(0, Math.floor((Date.now() - time) / 1000));
    }
    function formatDuration(seconds) {
      const total = Math.max(0, Number(seconds || 0));
      const days = Math.floor(total / 86400);
      const hours = Math.floor((total % 86400) / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      const parts = [];
      if (days) parts.push(`${days}d`);
      if (hours) parts.push(`${hours}h`);
      if (minutes) parts.push(`${minutes}m`);
      if (secs || !parts.length) parts.push(`${secs}s`);
      return parts.join(" ");
    }
    function formatStageDuration(seconds) {
      const total = Math.max(0, Number(seconds || 0));
      if (total < 60) return `${total}s`;
      const days = Math.floor(total / 86400);
      const hours = Math.floor((total % 86400) / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const parts = [];
      if (days) parts.push(`${days}d`);
      if (hours) parts.push(`${hours}h`);
      if (minutes || !parts.length) parts.push(`${minutes}m`);
      return parts.join(" ");
    }
    function terminalStatus(status) {
      return ["completed", "error"].includes(text(status, "").toLowerCase());
    }
    function stageTiming(stage) {
      return stage?.timing || {};
    }
    function stageStartedAt(stage) {
      const timing = stageTiming(stage);
      return timing.started_at || stage?.started_at || null;
    }
    function stageDuration(stage, summary, isActive) {
      const status = text(stage?.status, "pending").toLowerCase();
      const timing = stageTiming(stage);
      if (status === "running") {
        const elapsed = secondsSince(stageStartedAt(stage) || summary.created_at);
        return {
          label: elapsed === null ? t("elapsed.running") : formatStageDuration(elapsed),
          live: elapsed !== null,
          tone: "running"
        };
      }
      const stored = timing.duration_seconds;
      if (stored !== undefined && stored !== null) {
        return { label: formatStageDuration(stored), live: false, tone: status };
      }
      if (isActive && !terminalStatus(summary.status)) {
        const elapsed = secondsSince(stageStartedAt(stage) || summary.created_at);
        if (elapsed !== null) return { label: formatStageDuration(elapsed), live: false, tone: status };
      }
      return null;
    }
    function setElapsed(label, duration, tone = "") {
      const target = document.getElementById("elapsed-counter");
      if (!target) return;
      const toneClass = tone ? ` ${tone}` : "";
      target.innerHTML = `<span class="elapsed-label">${escapeHtml(label)}</span><span class="elapsed-time${toneClass}">${escapeHtml(duration)}</span>`;
    }
    function renderElapsed(payload = latestPayload) {
      const target = document.getElementById("elapsed-counter");
      if (!target) return;
      const current = currentData(payload || {});
      if (!current) {
        target.textContent = t("elapsed.idle");
        return;
      }
      const summary = current.views.summary || {};
      const progress = current.views.progress || {};
      const stages = stageMap(progress);
      const stage = stages[summary.current_stage] || {};
      const stageName = stageLabel(summary.current_stage) || text(summary.current_stage, t("elapsed.running"));
      const phase = summary.current_phase ? ` · ${summary.current_phase}` : "";
      if (terminalStatus(summary.status)) {
        setElapsed(t("elapsed.duration"), formatDuration(summary.duration_seconds || 0), "done");
        document.querySelectorAll("[data-elapsed-live]").forEach(item => {
          item.textContent = `${formatDuration(summary.duration_seconds || 0)} ${t("elapsed.total")}`;
        });
        return;
      }
      const elapsed = secondsSince(stageStartedAt(stage) || summary.created_at);
      setElapsed(`${stageName}${phase}`, elapsed === null ? t("elapsed.running") : formatDuration(elapsed), "running");
      document.querySelectorAll("[data-stage-live]").forEach(item => {
        item.textContent = elapsed === null ? t("elapsed.running") : formatStageDuration(elapsed);
      });
      document.querySelectorAll("[data-elapsed-live]").forEach(item => {
        item.textContent = elapsed === null ? t("elapsed.running") : formatDuration(elapsed);
      });
    }
    function canResumeRun(run) {
      const status = text(run?.status, "").toLowerCase();
      return Boolean(run?.run_id) && !["completed", "error"].includes(status);
    }
    function resumeRequest(runId) {
      return `Use phaseharness to resume run ${runId}.`;
    }
    function copyIcon() {
      return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <rect x="9" y="9" width="13" height="13" rx="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>`;
    }
    function copyButton(value) {
      return `<button type="button" class="copy-btn" title="${escapeHtml(t("copy.resume"))}" aria-label="${escapeHtml(t("copy.resume"))}" data-copy="${escapeHtml(value)}" onclick="copyResume(event, this)">${copyIcon()}</button>`;
    }
    async function copyResume(event, button) {
      event.stopPropagation();
      const value = button.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(value);
        button.classList.add("copied");
        button.title = t("copy.copied");
        setTimeout(() => {
          button.classList.remove("copied");
          button.title = t("copy.resume");
        }, 1200);
      } catch (error) {
        button.title = t("copy.failed");
      }
    }
    function currentData(payload) {
      if (selectedRunId && payload.run_details?.[selectedRunId]) return payload.run_details[selectedRunId];
      return payload.current || null;
    }
    function selectedSummary(payload) {
      return currentData(payload)?.views?.summary || null;
    }
    async function selectRun(runId) {
      if (!latestPayload || !runId) return;
      if (!latestPayload.run_details) latestPayload.run_details = {};
      if (!latestPayload.run_details[runId]) {
        try {
          const response = await fetch(`/api/run?run_id=${encodeURIComponent(runId)}`, { cache: "no-store" });
          if (!response.ok) throw new Error(`run detail request failed: ${response.status}`);
          latestPayload.run_details[runId] = await response.json();
        } catch (error) {
          document.getElementById("subtitle").textContent = `${t("error.runDetailLoad")}: ${error}`;
          return;
        }
      }
      selectedRunId = runId;
      renderDashboard(latestPayload);
    }
    function stageMap(progress) {
      const map = {};
      for (const stage of progress?.stages || []) map[stage.stage] = stage;
      return map;
    }
    function renderHeader(payload) {
      const current = currentData(payload);
      const summary = current?.views?.summary;
      document.getElementById("subtitle").textContent = current
        ? `${summary.request || ""} · ${summary.run_id || ""}`
        : `${t("empty.noActiveRun")} · ${payload.root}`;
    }
    function node(stage, status, meta, active, options = {}) {
      const classes = ["node", cls(status)];
      if (active) classes.push("active");
      if (options.flowing) classes.push("flowing");
      if (options.last) classes.push("last");
      const duration = options.duration;
      const durationHtml = duration
        ? `<div class="node-duration ${escapeHtml(duration.tone || "")}"${duration.live ? " data-stage-live" : ""}>${escapeHtml(duration.label)}</div>`
        : `<div class="node-duration empty" aria-hidden="true">0m</div>`;
      return `<div class="${classes.join(" ")}">
        <div class="node-title">${escapeHtml(stage)}</div>
        ${durationHtml}
        ${badge(status)}
      </div>`;
    }
    function renderFlow(payload) {
      const current = currentData(payload);
      const progress = current?.views?.progress || {};
      const summary = current?.views?.summary || {};
      const stages = stageMap(progress);
      let stageNodes = "";
      for (let i = 0; i < STAGES.length; i++) {
        const stage = STAGES[i];
        const state = stages[stage] || { status: "pending", attempts: 0 };
        const isActive = summary.current_stage === stage;
        const isFlowing = isActive && i < STAGES.length - 1;
        stageNodes += node(stageLabel(stage), state.status, "", isActive, {
          flowing: isFlowing,
          last: i === STAGES.length - 1,
          duration: stageDuration(state, summary, isActive)
        });
      }
      const phases = progress.phases || [];
      const phaseCards = phases.length
        ? phases.map(phase => {
          const state = fileState(phase.file);
          const fileNote = state === "ok" ? "" : ` · ${displayFileState(state)}`;
          return `<div class="phase-card ${phase.current ? "active" : ""}">
            <div class="phase-name">${escapeHtml(phase.title || phase.phase_id)}</div>
            <div class="phase-meta">${escapeHtml(phase.phase_id)}${escapeHtml(fileNote)}</div>
            ${phase.summary ? `<div class="phase-summary">${escapeHtml(phase.summary)}</div>` : ""}
            ${badge(phase.status)}
          </div>`;
        }).join("")
        : `<div class="empty">${escapeHtml(t("empty.noGeneratePhaseFiles"))}</div>`;
      document.getElementById("flow").innerHTML = `
        <div class="stage-lane">${stageNodes}</div>
        <div class="phase-lane">
          <div class="phase-title">${escapeHtml(t("phase.generatePhases"))}</div>
          <div class="phase-grid">${phaseCards}</div>
        </div>`;
    }
    function renderMetrics(payload) {
      const current = currentData(payload);
      const summary = current?.views?.summary || {};
      const resume = current?.views?.resume || {};
      const metrics = [
        [t("metric.stage"), summary.current_stage ? stageLabel(summary.current_stage) : t("common.none")],
        [t("metric.phase"), summary.current_phase || t("common.none")],
        [t("metric.loop"), summary.loop ? `${summary.loop.current || 1}/${summary.loop.max || 1}` : t("common.none")],
        [t("metric.evaluation"), summary.evaluation_status ? displayStatus(summary.evaluation_status, "common.none") : t("common.none")]
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value]) => `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(text(value, t("common.none")))}</div></div>`).join("");
    }
    function findHistoryItem(payload, runId, preferredGroup = null) {
      const groups = payload.history?.groups || {};
      const order = [preferredGroup, "failed", "resumable", "running", "all"].filter(Boolean);
      for (const group of order) {
        for (const item of groups[group] || []) {
          if (item.run_id === runId) return item;
        }
      }
      return null;
    }
    function uniqueNonEmpty(items) {
      const seen = new Set();
      const result = [];
      for (const item of items || []) {
        const value = text(item, "").replace(/^[-*]\s*/, "").trim();
        if (!value || seen.has(value)) continue;
        seen.add(value);
        result.push(value);
      }
      return result;
    }
    function failureEvidence(diagnostics) {
      const intent = diagnostics.intent_alignment || {};
      const guidance = diagnostics.guidance_compliance || {};
      const failure = diagnostics.failure_analysis || {};
      const validation = diagnostics.validation || {};
      return uniqueNonEmpty([
        ...(intent.failed_requirements || []),
        ...(guidance.violations || []),
        ...(failure.findings || []),
        ...(validation.failed_commands || []),
        ...(diagnostics.missing_data || [])
      ]).slice(0, 3);
    }
    function resultBanner(payload, current, summary) {
      const status = text(summary.status, "").toLowerCase();
      if (["error", "failed", "fail"].includes(status)) {
        const item = findHistoryItem(payload, summary.run_id, "failed");
        const diagnostics = current.views?.diagnostics || {};
        const findings = diagnostics.failure_analysis?.findings || [];
        const reason = item?.reason || item?.detail || findings[0] || t("result.noFailureReason");
        const evidence = failureEvidence(diagnostics).filter(item => item !== reason);
        return `<div class="run-result error">
          <div class="run-result-title"><strong>${escapeHtml(t("result.whyFailed"))}</strong><span>${escapeHtml(stageLabel(summary.current_stage) || "run")}</span></div>
          <div class="run-result-reason">${escapeHtml(reason)}</div>
          ${evidence.length ? `<ul class="run-result-list">${evidence.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
        </div>`;
      }
      if (status === "completed") {
        const evaluation = summary.evaluation_status ? `${t("result.evaluation")} ${displayStatus(summary.evaluation_status)}` : t("result.runCompleted");
        return `<div class="run-result completed"><div class="run-result-title"><strong>${escapeHtml(t("result.completed"))}</strong><span>${escapeHtml(evaluation)}</span></div></div>`;
      }
      return "";
    }
    function renderWorkflowContext(payload) {
      const target = document.getElementById("workflow-context");
      if (!target) return;
      const current = currentData(payload);
      if (!current) {
        target.innerHTML = `<div class="empty">${escapeHtml(t("empty.noActivePhaseharnessRun"))}</div>`;
        return;
      }
      const summary = current.views.summary;
      const resume = current.views.resume;
      const request = resumeRequest(summary.run_id);
      const hint = canResumeRun(summary)
        ? `<div class="resume-hint workflow-resume"><span>${escapeHtml(t("resume.label"))}</span><code>${escapeHtml(request)}</code>${copyButton(request)}</div>`
        : "";
      target.innerHTML = `
        <div class="workflow-context-top">
          <div class="workflow-run">
            <div class="workflow-title-line">
              <span class="workflow-run-id">${escapeHtml(summary.run_id)}</span>
            </div>
            <div class="workflow-run-line">
              ${metaChip(t("meta.mode"), summary.mode || t("common.unknown"))}
              ${metaChip(t("meta.branch"), summary.worktree?.branch || t("common.unknown"))}
            </div>
          </div>
        </div>
        ${resultBanner(payload, current, summary)}
        ${hint}`;
    }
    function historyCard(key, label, value, options = {}) {
      const count = Number(value || 0);
      const selected = historyFilter === key ? " selected" : "";
      const danger = options.danger ? " danger" : "";
      const action = count ? (selected ? t("history.showing") : t("history.viewList")) : t("history.noRuns");
      return `<button type="button" class="history-stat clickable${selected}${danger}" onclick="selectHistoryFilter('${escapeHtml(key)}', true)" ${count ? "" : "disabled"}>
        <div class="history-label">${escapeHtml(label)}</div>
        <div class="history-value">${escapeHtml(count)}</div>
        <div class="history-action">${escapeHtml(action)}</div>
      </button>`;
    }
    function selectHistoryFilter(key, scrollToRuns = false) {
      historyFilter = key || "all";
      if (!historyVisibleCounts[historyFilter]) {
        historyVisibleCounts[historyFilter] = HISTORY_INITIAL_VISIBLE;
      }
      renderDashboard(latestPayload);
      if (scrollToRuns) {
        setTimeout(() => document.getElementById("runs-section")?.scrollIntoView({ block: "start", behavior: "smooth" }), 0);
      }
    }
    function showMoreHistory(key) {
      historyVisibleCounts[key] = (historyVisibleCounts[key] || HISTORY_INITIAL_VISIBLE) + HISTORY_PAGE_SIZE;
      renderDashboard(latestPayload);
    }
    function collapseHistory(key) {
      historyVisibleCounts[key] = HISTORY_INITIAL_VISIBLE;
      renderDashboard(latestPayload);
    }
    function renderHistoryDetailItem(item, tone = "") {
      const runId = text(item.run_id, "");
      const selected = selectedRunId === runId ? " selected" : "";
      const detail = text(item.reason || item.detail, t("result.noFailureReason"));
      return `<button type="button" class="history-detail-item${tone}${selected}" onclick="selectRun('${escapeHtml(runId)}')">
        <div class="history-detail-main">
          <code>${escapeHtml(runId)}</code>
          <span>${escapeHtml(detail)}</span>
        </div>
        <div class="history-detail-meta">
          <span class="history-detail-status">${escapeHtml(displayStatus(item.status, "common.unknown"))}</span>
          <time>${escapeHtml(formatLocalDateTime(item.updated_at))}</time>
        </div>
      </button>`;
    }
    function renderHistoryDetails(history) {
      if (!historyFilter) return "";
      const groups = history.groups || {};
      const items = groups[historyFilter] || [];
      const label = t(HISTORY_FILTER_LABELS[historyFilter] || historyFilter);
      const totals = {
        all: history.total || 0,
        running: history.status?.active || 0,
        resumable: history.resumable || 0,
        failed: history.status?.error || 0
      };
      const total = totals[historyFilter] || items.length;
      const visibleCount = Math.min(historyVisibleCounts[historyFilter] || HISTORY_INITIAL_VISIBLE, items.length);
      const visibleItems = items.slice(0, visibleCount);
      const suffix = `${visibleCount}/${total}`;
      const hiddenCount = Math.max(0, Math.min(total, items.length) - visibleCount);
      const tone = historyFilter === "failed" ? " danger" : "";
      return `<div class="history-detail">
        <div class="history-detail-head"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(suffix)}</span></div>
        ${items.length
          ? `<div class="history-detail-list">${visibleItems.map(item => renderHistoryDetailItem(item, tone)).join("")}</div>`
          : `<div class="empty">${escapeHtml(t("empty.noRunsInGroup"))}</div>`}
        ${items.length ? `<div class="history-detail-actions">
          ${visibleCount < items.length ? `<button type="button" class="history-more-btn" onclick="showMoreHistory('${escapeHtml(historyFilter)}')">${escapeHtml(t("history.showMore"))}${hiddenCount ? ` (${escapeHtml(hiddenCountLabel(hiddenCount))})` : ""}</button>` : ""}
          ${visibleCount > HISTORY_INITIAL_VISIBLE ? `<button type="button" class="history-more-btn" onclick="collapseHistory('${escapeHtml(historyFilter)}')">${escapeHtml(t("history.collapse"))}</button>` : ""}
        </div>` : ""}
      </div>`;
    }
    function renderHistoryChart(history) {
      const status = history.status || {};
      const total = Math.max(0, Number(history.total || 0));
      const rows = [
        ["error", t("overview.error"), Number(status.error || 0)],
        ["active", t("overview.active"), Number(status.active || 0)],
        ["waiting", t("overview.waiting"), Number(status.waiting_user || 0)],
        ["completed", t("overview.completed"), Number(status.completed || 0)],
        ["other", t("overview.other"), Number(status.other || 0)]
      ];
      const distribution = rows.map(([key, label, count]) => {
        const percent = total ? Math.round((count / total) * 100) : 0;
        const width = count > 0 ? Math.max(2, percent) : 0;
        return `<div class="overview-status-row">
          <div class="overview-status-label"><span class="overview-dot ${escapeHtml(key)}"></span><span>${escapeHtml(label)}</span></div>
          <div class="overview-track"><div class="overview-fill ${escapeHtml(key)}" style="width: ${width}%"></div></div>
          <div class="overview-status-value"><strong>${escapeHtml(count)}</strong><span>${escapeHtml(percent)}%</span></div>
        </div>`;
      }).join("");
      return `<div class="overview-chart">
        <div class="overview-chart-head"><strong>${escapeHtml(t("overview.statusDistribution"))}</strong><span>${escapeHtml(total)} ${escapeHtml(t("overview.total"))}</span></div>
        <div class="overview-distribution" aria-label="${escapeHtml(t("overview.statusDistribution"))}">${distribution}</div>
      </div>`;
    }
    function renderHistory(payload) {
      const history = payload.history || {};
      const status = history.status || {};
      if (!history.total) {
        document.getElementById("history").innerHTML = `<div class="empty">${escapeHtml(t("empty.noRunHistory"))}</div>`;
        return;
      }
      const activeTotal = status.active || 0;
      document.getElementById("history").innerHTML = `
        <div class="overview-layout">
          ${renderHistoryChart(history)}
          <div class="history-grid">
            ${historyCard("all", t("history.all"), history.total)}
            ${historyCard("running", t("history.running"), activeTotal)}
            ${historyCard("resumable", t("history.resumable"), history.resumable || 0)}
            ${historyCard("failed", t("history.failed"), status.error || 0, { danger: true })}
          </div>
        </div>`;
    }
    function renderOutputs(payload) {
      const current = currentData(payload);
      if (!current) {
        document.getElementById("outputs").innerHTML = `<div class="empty">${escapeHtml(t("empty.noActiveOutputs"))}</div>`;
        return;
      }
      const rows = [];
      for (const item of current.outputs.artifacts || []) rows.push(["artifact", stageLabel(item.stage) || item.stage, fileState(item), item.path]);
      for (const item of current.outputs.phases || []) rows.push(["phase", item.phase_id, fileState(item), item.path]);
      document.getElementById("outputs").innerHTML = `<table><thead><tr><th>${escapeHtml(t("output.kind"))}</th><th>${escapeHtml(t("output.name"))}</th><th>${escapeHtml(t("output.state"))}</th><th>${escapeHtml(t("output.path"))}</th></tr></thead><tbody>${rows.map(row => `<tr><td>${escapeHtml(displayKind(row[0]))}</td><td>${escapeHtml(row[1])}</td><td>${escapeHtml(displayFileState(row[2]))}</td><td><code>${escapeHtml(row[3])}</code></td></tr>`).join("")}</tbody></table>`;
    }
    function reviewValueClass(value) {
      const status = text(value, "").toLowerCase();
      if (["fail", "failed", "error"].includes(status)) return "fail";
      if (["pass", "warn", "completed"].includes(status)) return status;
      return "";
    }
    function reviewRow(label, value, description, toneValue = value) {
      return `<div class="review-row">
        <div class="review-label"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(description)}</span></div>
        <div class="review-value ${escapeHtml(reviewValueClass(toneValue))}">${escapeHtml(text(value, t("common.none")))}</div>
      </div>`;
    }
    function renderRunReview(payload) {
      const current = currentData(payload);
      const section = document.getElementById("run-review-section");
      const target = document.getElementById("run-review");
      if (!section || !target || !current) return;
      const summary = current.views?.summary || {};
      const diagnostics = current.views?.diagnostics || {};
      const feedback = current.views?.feedback?.counts || {};
      const validation = diagnostics.validation || {};
      const status = text(summary.status, "").toLowerCase();
      const hasReview =
        ["error", "failed", "fail"].includes(status) ||
        text(diagnostics.intent_alignment?.status, "pending") !== "pending" ||
        text(diagnostics.guidance_compliance?.status, "pending") !== "pending" ||
        Object.values(feedback).some(value => Number(value || 0) > 0);
      section.hidden = !hasReview;
      if (!hasReview) {
        target.innerHTML = "";
        return;
      }
      const reviewChecks = [
        reviewRow(t("review.requirements"), diagnostics.intent_alignment?.status ? displayStatus(diagnostics.intent_alignment?.status) : t("common.none"), t("review.requirementsDesc"), diagnostics.intent_alignment?.status),
        reviewRow(t("review.guidance"), diagnostics.guidance_compliance?.status ? displayStatus(diagnostics.guidance_compliance?.status) : t("common.none"), t("review.guidanceDesc"), diagnostics.guidance_compliance?.status),
        reviewRow(t("review.checksLabel"), (validation.commands_found || []).length, t("review.checksDesc")),
        reviewRow(t("review.failed"), (validation.failed_commands || []).length, t("review.failedDesc"))
      ].join("");
      const feedbackRows = [
        reviewRow(t("review.evaluateFailures"), feedback.evaluate_failures || 0, t("review.evaluateFailuresDesc")),
        reviewRow(t("review.followups"), feedback.followup_phases || 0, t("review.followupsDesc")),
        reviewRow(t("review.loopRetries"), feedback.loop_retries || 0, t("review.loopRetriesDesc")),
        reviewRow(t("review.postCompletion"), feedback.explicit_post_completion_feedback || 0, t("review.postCompletionDesc"))
      ].join("");
      target.innerHTML = `<div class="review-grid">
        <div class="review-card"><h3>${escapeHtml(t("review.checks"))}</h3><div class="review-list">${reviewChecks}</div></div>
        <div class="review-card"><h3>${escapeHtml(t("review.feedback"))}</h3><div class="review-list">${feedbackRows}</div></div>
      </div>`;
    }
    function historyFilterButton(key, label, count, options = {}) {
      const selected = historyFilter === key ? " selected" : "";
      const danger = options.danger ? " danger" : "";
      return `<button type="button" class="recent-filter${selected}${danger}" onclick="selectHistoryFilter('${escapeHtml(key)}')">${escapeHtml(label)} ${escapeHtml(count)}</button>`;
    }
    function renderRecent(payload) {
      const history = payload.history || {};
      const status = history.status || {};
      const groups = history.groups || {};
      const filter = historyFilter || "all";
      const runs = groups[filter] || [];
      const selected = selectedSummary(payload);
      const selectedId = selected?.run_id || payload.active_run;
      if (!history.total) {
        document.getElementById("recent").innerHTML = `<div class="empty">${escapeHtml(t("empty.noRecentRuns"))}</div>`;
        return;
      }
      const totals = {
        all: history.total || 0,
        running: status.active || 0,
        resumable: history.resumable || 0,
        failed: status.error || 0
      };
      const visibleCount = Math.min(historyVisibleCounts[filter] || HISTORY_INITIAL_VISIBLE, runs.length);
      const visibleRuns = runs.slice(0, visibleCount);
      const hiddenCount = Math.max(0, runs.length - visibleCount);
      const controls = `<div class="recent-filter-row">
        ${historyFilterButton("all", t("filter.all"), totals.all)}
        ${historyFilterButton("running", t("filter.running"), totals.running)}
        ${historyFilterButton("resumable", t("filter.resumable"), totals.resumable)}
        ${historyFilterButton("failed", t("filter.failed"), totals.failed, { danger: true })}
      </div>`;
      const table = visibleRuns.length
        ? `<table><thead><tr><th>${escapeHtml(t("runs.run"))}</th><th>${escapeHtml(t("runs.status"))}</th><th>${escapeHtml(t("runs.stage"))}</th><th>${escapeHtml(t("runs.phase"))}</th><th>${escapeHtml(t("runs.updated"))}</th><th>${escapeHtml(t("runs.resume"))}</th></tr></thead><tbody>${visibleRuns.map(run => {
        const resume = resumeRequest(run.run_id);
        const action = canResumeRun(run)
          ? `<div class="resume-action"><code>${escapeHtml(resume)}</code>${copyButton(resume)}</div>`
          : t("common.done");
        const classes = ["run-row"];
        if (run.run_id === selectedId) classes.push("selected");
        const stage = run.current_stage || run.stage;
        const phase = run.current_phase || run.phase;
        const detail = run.reason || run.detail;
        return `<tr class="${classes.join(" ")}" onclick="selectRun('${escapeHtml(run.run_id)}')"><td><code>${escapeHtml(run.run_id)}</code>${detail ? `<div class="run-detail">${escapeHtml(detail)}</div>` : ""}</td><td>${escapeHtml(displayStatus(run.status, "common.unknown"))}</td><td>${escapeHtml(stageLabel(stage) || stage || t("common.none"))}</td><td>${escapeHtml(phase || t("common.none"))}</td><td>${escapeHtml(formatLocalDateTime(run.updated_at || run.created_at))}</td><td>${action}</td></tr>`;
      }).join("")}</tbody></table>`
        : `<div class="empty">${escapeHtml(t("empty.noRunsInFilter"))}</div>`;
      const actions = runs.length ? `<div class="history-detail-actions">
        ${visibleCount < runs.length ? `<button type="button" class="history-more-btn" onclick="showMoreHistory('${escapeHtml(filter)}')">${escapeHtml(t("history.showMore"))}${hiddenCount ? ` (${escapeHtml(hiddenCountLabel(hiddenCount))})` : ""}</button>` : ""}
        ${visibleCount > HISTORY_INITIAL_VISIBLE ? `<button type="button" class="history-more-btn" onclick="collapseHistory('${escapeHtml(filter)}')">${escapeHtml(t("history.collapse"))}</button>` : ""}
      </div>` : "";
      document.getElementById("recent").innerHTML = `${controls}${table}${actions}`;
    }
    function renderDashboard(payload) {
      if (!payload) return;
      if (selectedRunId && !payload.run_details?.[selectedRunId]) selectedRunId = null;
      renderHeader(payload);
      renderWorkflowContext(payload);
      renderFlow(payload);
      renderMetrics(payload);
      renderHistory(payload);
      renderRunReview(payload);
      renderOutputs(payload);
      renderRecent(payload);
      renderElapsed(payload);
    }
    async function loadDashboard() {
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        const payload = await response.json();
        payload.run_details = {
          ...(latestPayload?.run_details || {}),
          ...(payload.run_details || {})
        };
        latestPayload = payload;
        renderDashboard(payload);
      } catch (error) {
        document.getElementById("subtitle").textContent = `${t("error.dashboardRefresh")}: ${error}`;
      }
    }
    initLanguageControl();
    initThemeControl();
    loadDashboard();
    pollTimer = setInterval(loadDashboard, 2000);
    elapsedTimer = setInterval(() => renderElapsed(), 1000);
  </script>
</body>
</html>"""


def json_response(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, body_text: str) -> None:
    body = body_text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("", "/", "/index.html"):
                html_response(self, dashboard_html())
                return
            if parsed.path == "/api/dashboard":
                try:
                    json_response(self, 200, build_dashboard_payload(root))
                except Exception as exc:
                    json_response(self, 500, {"error": str(exc), "generated_at": now_iso()})
                return
            if parsed.path == "/api/run":
                params = parse_qs(parsed.query)
                run_id = clean_optional((params.get("run_id") or [None])[0])
                if not run_id:
                    json_response(self, 400, {"error": "missing run_id", "generated_at": now_iso()})
                    return
                try:
                    validate_run_id(run_id)
                    if not run_path(root, run_id).exists():
                        json_response(self, 404, {"error": "run not found", "run_id": run_id, "generated_at": now_iso()})
                        return
                    json_response(self, 200, load_run_views(root, run_id, now_iso(), refresh=False))
                except Exception as exc:
                    json_response(self, 500, {"error": str(exc), "run_id": run_id, "generated_at": now_iso()})
                return
            json_response(self, 404, {"error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def run_server() -> int:
    root = resolve_root(None)
    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, 0), make_handler(root))
    port = int(server.server_port)
    url = f"http://{host}:{port}/"
    print(f"Phaseharness dashboard running at {url}", flush=True)
    print("Polling run state every 2 seconds. Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("usage: render-dashboard.py")
        print()
        print("Start one Phaseharness dashboard page for the current worktree.")
        return 0
    if len(sys.argv) > 1:
        print("error: render-dashboard.py does not accept options", file=sys.stderr)
        return 2

    try:
        return run_server()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
