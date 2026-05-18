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
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde6;
      --line-strong: #aeb7c5;
      --text: #1f2933;
      --muted: #667085;
      --blue: #2563eb;
      --green: #16835f;
      --amber: #b7791f;
      --red: #c2413a;
      --slate: #3f4b5f;
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
      background: #fff;
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
    .statusbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      min-width: 260px;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      background: #fff;
      color: var(--slate);
      white-space: nowrap;
    }
    .chip.live { border-color: #b7dfcf; color: var(--green); background: #f1fbf7; }
    .chip.warn { border-color: #ead19a; color: var(--amber); background: #fff9ea; }
    .chip.bad { border-color: #f1bbb6; color: var(--red); background: #fff2f1; }
    main {
      width: min(1440px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 20px 0 32px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 18px;
      align-items: start;
    }
    main > .stack, main > aside { min-width: 0; }
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
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      white-space: nowrap;
    }
    .section-body { padding: 16px; }
    .flow-board {
      background: #fbfcfe;
      border-radius: 8px;
      border: 1px solid #e6e9ef;
      padding: 18px;
    }
    .flow-inner {
      display: grid;
      gap: 18px;
      min-width: 0;
    }
    .stage-lane {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 58px;
      align-items: stretch;
      position: relative;
    }
    .node {
      position: relative;
      min-width: 0;
      min-height: 88px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      z-index: 1;
    }
    .node::after {
      content: "";
      position: absolute;
      top: 50%;
      right: -45px;
      width: 34px;
      height: 2px;
      background: #c3cad5;
      transform: translateY(-50%);
      z-index: 2;
    }
    .node::before {
      content: "";
      position: absolute;
      top: calc(50% - 5px);
      right: -52px;
      border-left: 8px solid #c3cad5;
      border-top: 5px solid transparent;
      border-bottom: 5px solid transparent;
      z-index: 3;
    }
    .node.last::before,
    .node.last::after { display: none; }
    .node.active { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12), 0 8px 22px rgba(31, 41, 51, 0.08); }
    .node.completed { border-color: #9ed7c1; }
    .node.running { border-color: var(--blue); }
    .node.error, .node.failed { border-color: #ee9d97; }
    .node.pending { border-style: dashed; }
    .node.flowing {
      border-color: var(--blue);
      animation: active-border 1.8s ease-in-out infinite;
    }
    .node.flowing::after {
      background: linear-gradient(90deg, rgba(37, 99, 235, 0.12), rgba(37, 99, 235, 0.75), rgba(37, 99, 235, 0.12));
      background-size: 200% 100%;
      animation: arrow-flow 1.8s linear infinite;
    }
    .node.flowing::before {
      border-left-color: var(--blue);
      animation: arrow-pulse 1.3s ease-in-out infinite;
    }
    @keyframes active-border {
      0%, 100% { box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.08); }
      50% { box-shadow: 0 0 0 5px rgba(37, 99, 235, 0.18); }
    }
    @keyframes arrow-flow {
      from { background-position: 200% 0; }
      to { background-position: 0 0; }
    }
    @keyframes arrow-pulse {
      0%, 100% { opacity: 0.55; }
      50% { opacity: 1; }
    }
    .node-title {
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 7px;
      overflow-wrap: anywhere;
    }
    .node-meta {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .phase-lane {
      border-top: 1px solid #e6e9ef;
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
      background: #fff;
    }
    .phase-card.active { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1); }
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
      height: 22px;
      border-radius: 999px;
      padding: 0 8px;
      font-size: 11px;
      border: 1px solid var(--line);
      margin-top: 8px;
      color: var(--slate);
    }
    .badge.completed { border-color: #9ed7c1; color: var(--green); background: #f1fbf7; }
    .badge.running { border-color: #adc7ff; color: var(--blue); background: #f2f6ff; }
    .badge.error, .badge.failed { border-color: #ee9d97; color: var(--red); background: #fff2f1; }
    .badge.pending { color: var(--muted); background: #f8fafc; }
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
      background: #fff;
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
      border-bottom: 1px solid #edf0f4;
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th { color: var(--muted); font-weight: 600; }
    tr.run-row {
      cursor: pointer;
    }
    tr.run-row:hover {
      background: #f8fbff;
    }
    tr.run-row.selected {
      background: #f3f7ff;
      box-shadow: inset 3px 0 0 var(--blue);
    }
    .empty {
      color: var(--muted);
      font-size: 13px;
      padding: 18px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fafbfc;
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
    .current-card {
      display: grid;
      gap: 12px;
    }
    .current-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }
    .eyebrow {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 3px;
    }
    .run-id {
      display: block;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .run-request {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .run-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .next-box {
      border-top: 1px solid #eef1f5;
      padding-top: 10px;
      display: grid;
      gap: 4px;
      font-size: 12px;
    }
    .next-box strong {
      font-size: 12px;
    }
    .next-box span {
      color: var(--muted);
      line-height: 1.4;
    }
    .history-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .history-stat {
      appearance: none;
      width: 100%;
      min-height: 64px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: #fff;
      color: inherit;
      font: inherit;
      text-align: left;
      display: grid;
      align-content: center;
      gap: 3px;
    }
    .history-stat.clickable {
      cursor: pointer;
    }
    .history-stat.clickable:hover:not(:disabled),
    .history-stat.selected {
      border-color: #84a9ff;
      background: #f7faff;
    }
    .history-stat.danger.selected {
      border-color: #f1a7a0;
      background: #fff8f7;
    }
    .history-stat:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .history-stat:focus-visible,
    .history-detail-item:focus-visible {
      outline: 2px solid #3b82f6;
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
    .history-issues {
      display: grid;
      gap: 0;
      margin-top: 12px;
    }
    .history-detail {
      display: grid;
      gap: 8px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid #eef1f5;
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
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 8px 9px;
      background: #fff;
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
      border-color: #84a9ff;
      background: #f7faff;
    }
    .history-detail-item.danger:hover,
    .history-detail-item.danger.selected {
      border-color: #f1a7a0;
      background: #fff8f7;
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
    }
    .history-more-btn {
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--slate);
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      line-height: 1;
      padding: 7px 10px;
    }
    .history-more-btn:hover {
      border-color: #84a9ff;
      background: #f7faff;
      color: var(--blue);
    }
    .history-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 12px;
      padding: 7px 0;
      border-top: 1px solid #eef1f5;
    }
    .history-row span {
      color: var(--muted);
    }
    .resume-hint {
      padding: 9px 10px;
      border: 1px solid #dbe4ff;
      border-radius: 8px;
      background: #f5f8ff;
      color: #243b76;
      font-size: 12px;
      line-height: 1.45;
    }
    .resume-hint code {
      color: #1d4ed8;
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
      border: 1px solid #cbd5e1;
      border-radius: 7px;
      background: #fff;
      color: #475569;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      flex: 0 0 auto;
    }
    .copy-btn:hover {
      border-color: #93c5fd;
      color: var(--blue);
      background: #f8fbff;
    }
    .copy-btn.copied {
      border-color: #9ed7c1;
      color: var(--green);
      background: #f1fbf7;
    }
    .copy-btn svg {
      width: 15px;
      height: 15px;
      stroke-width: 2;
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      color: #334155;
    }
    @media (max-width: 980px) {
      header { flex-direction: column; }
      .statusbar { justify-content: flex-start; }
      main { width: calc(100vw - 28px); grid-template-columns: 1fr; padding: 14px 0; }
      .stage-lane { grid-template-columns: 1fr; gap: 10px; }
      .stage-lane .node::before, .stage-lane .node::after { display: none; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Phaseharness Dashboard</h1>
      <div class="subtitle" id="subtitle">Loading run state...</div>
    </div>
    <div class="statusbar" id="statusbar"></div>
  </header>
  <main>
    <div class="stack">
      <section>
        <div class="section-head">
          <h2>Workflow</h2>
          <span class="elapsed-counter" id="elapsed-counter">idle</span>
        </div>
        <div class="section-body">
          <div class="flow-board">
            <div class="flow-inner" id="flow"></div>
          </div>
          <div class="metrics" id="metrics"></div>
        </div>
      </section>
      <section>
        <div class="section-head"><h2>Outputs</h2></div>
        <div class="section-body" id="outputs"></div>
      </section>
      <section>
        <div class="section-head"><h2>Recent Runs</h2></div>
        <div class="section-body" id="recent"></div>
      </section>
    </div>
    <aside class="stack">
      <section>
        <div class="section-head"><h2 id="run-details-title">Current Run</h2></div>
        <div class="section-body" id="current"></div>
      </section>
      <section>
        <div class="section-head"><h2>Run History</h2></div>
        <div class="section-body" id="history"></div>
      </section>
      <section id="diagnostics-section" hidden>
        <div class="section-head"><h2>Review Checks</h2></div>
        <div class="section-body" id="diagnostics"></div>
      </section>
      <section id="feedback-section" hidden>
        <div class="section-head"><h2>Feedback</h2></div>
        <div class="section-body" id="feedback"></div>
      </section>
    </aside>
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
    let historyFilter = null;
    let historyVisibleCounts = {};
    const HISTORY_INITIAL_VISIBLE = 5;
    const HISTORY_PAGE_SIZE = 10;
    const HISTORY_FILTER_LABELS = {
      all: "All runs",
      running: "Running now",
      resumable: "Can continue",
      failed: "Failed"
    };

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
    function chip(label, status) {
      return `<span class="chip ${cls(status)}">${escapeHtml(label)}</span>`;
    }
    function badge(status) {
      return `<span class="badge ${cls(status)}">${escapeHtml(text(status, "pending"))}</span>`;
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
      if (Number.isNaN(date.getTime())) return raw || "unknown";
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
    function terminalStatus(status) {
      return ["completed", "error"].includes(text(status, "").toLowerCase());
    }
    function renderElapsed(payload = latestPayload) {
      const target = document.getElementById("elapsed-counter");
      if (!target) return;
      const current = currentData(payload || {});
      if (!current) {
        target.textContent = "idle";
        return;
      }
      const summary = current.views.summary || {};
      const progress = current.views.progress || {};
      const stages = stageMap(progress);
      const stage = stages[summary.current_stage] || {};
      const stageLabel = STAGE_LABELS[summary.current_stage] || text(summary.current_stage, "running");
      const phase = summary.current_phase ? ` · ${summary.current_phase}` : "";
      if (terminalStatus(summary.status)) {
        target.textContent = `duration ${formatDuration(summary.duration_seconds || 0)}`;
        document.querySelectorAll("[data-elapsed-live]").forEach(item => {
          item.textContent = `${formatDuration(summary.duration_seconds || 0)} total`;
        });
        return;
      }
      const elapsed = secondsSince(stage.started_at || summary.created_at);
      const label = elapsed === null
        ? `${stageLabel}${phase} running`
        : `${stageLabel}${phase} ${formatDuration(elapsed)}`;
      target.textContent = label;
      document.querySelectorAll("[data-elapsed-live]").forEach(item => {
        item.textContent = elapsed === null ? "running" : formatDuration(elapsed);
      });
    }
    function canResumeRun(run) {
      const status = text(run?.status, "").toLowerCase();
      return Boolean(run?.run_id) && !["completed", "error"].includes(status);
    }
    function resumeRequest(runId) {
      return `phaseharness로 ${runId} 이어서 진행해줘.`;
    }
    function copyIcon() {
      return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <rect x="9" y="9" width="13" height="13" rx="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>`;
    }
    function copyButton(value) {
      return `<button type="button" class="copy-btn" title="Copy resume request" aria-label="Copy resume request" data-copy="${escapeHtml(value)}" onclick="copyResume(event, this)">${copyIcon()}</button>`;
    }
    async function copyResume(event, button) {
      event.stopPropagation();
      const value = button.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(value);
        button.classList.add("copied");
        button.title = "Copied";
        setTimeout(() => {
          button.classList.remove("copied");
          button.title = "Copy resume request";
        }, 1200);
      } catch (error) {
        button.title = "Copy failed";
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
          document.getElementById("subtitle").textContent = `Run detail load failed: ${error}`;
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
      const resume = current?.views?.resume;
      const activeSelected = summary?.run_id && summary.run_id === payload.active_run;
      document.getElementById("subtitle").textContent = current
        ? `${summary.request || ""} · ${summary.run_id || ""}`
        : `No active run · ${payload.root}`;
      const stale = resume?.stale?.is_stale;
      document.getElementById("statusbar").innerHTML = [
        chip(current ? (activeSelected ? "active run" : "selected run") : "inactive", current ? summary.status : "pending"),
        chip(`updated ${formatLocalDateTime(payload.generated_at)}`, "pending"),
        chip(stale ? "stale" : "fresh", stale ? "warn" : "completed")
      ].join("");
    }
    function node(stage, status, meta, active, options = {}) {
      const classes = ["node", cls(status)];
      if (active) classes.push("active");
      if (options.flowing) classes.push("flowing");
      if (options.last) classes.push("last");
      return `<div class="${classes.join(" ")}">
        <div class="node-title">${escapeHtml(stage)}</div>
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
        stageNodes += node(STAGE_LABELS[stage], state.status, "", isActive, { flowing: isFlowing, last: i === STAGES.length - 1 });
      }
      const phases = progress.phases || [];
      const phaseCards = phases.length
        ? phases.map(phase => `<div class="phase-card ${phase.current ? "active" : ""}">
            <div class="phase-name">${escapeHtml(phase.title || phase.phase_id)}</div>
            <div class="phase-meta">${escapeHtml(phase.phase_id)} · ${escapeHtml(fileState(phase.file))}</div>
            ${phase.summary ? `<div class="phase-summary">${escapeHtml(phase.summary)}</div>` : ""}
            ${badge(phase.status)}
          </div>`).join("")
        : `<div class="empty">No generate phase files found.</div>`;
      document.getElementById("flow").innerHTML = `
        <div class="stage-lane">${stageNodes}</div>
        <div class="phase-lane">
          <div class="phase-title">Generate phases</div>
          <div class="phase-grid">${phaseCards}</div>
        </div>`;
    }
    function renderMetrics(payload) {
      const current = currentData(payload);
      const summary = current?.views?.summary || {};
      const resume = current?.views?.resume || {};
      const metrics = [
        ["Status", summary.status],
        ["Next Action", resume.next_action],
        ["Loop", summary.loop ? `${summary.loop.current || 1}/${summary.loop.max || 1}` : "none"],
        ["Evaluation", summary.evaluation_status]
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value]) => `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${escapeHtml(text(value))}</div></div>`).join("");
    }
    function renderCurrent(payload) {
      const current = currentData(payload);
      if (!current) {
        document.getElementById("current").innerHTML = `<div class="empty">No active phaseharness run.</div>`;
        return;
      }
      const summary = current.views.summary;
      const resume = current.views.resume;
      document.getElementById("run-details-title").textContent = summary.run_id === payload.active_run ? "Current Run" : "Run Details";
      const hint = canResumeRun(summary)
        ? `<div class="resume-hint">Resume request
            <div class="resume-action"><code>${escapeHtml(resumeRequest(summary.run_id))}</code>${copyButton(resumeRequest(summary.run_id))}</div>
          </div>`
        : "";
      document.getElementById("current").innerHTML = `<div class="current-card">
        <div class="current-top">
          <div>
            <div class="eyebrow">${escapeHtml(summary.mode || "run")}</div>
            <code class="run-id">${escapeHtml(summary.run_id)}</code>
          </div>
          ${badge(summary.status)}
        </div>
        <div class="run-request">${escapeHtml(summary.request || "No request recorded.")}</div>
        <div class="run-meta">
          ${chip(summary.worktree?.branch || "unknown branch", "pending")}
        </div>
        <div class="next-box">
          <strong>${escapeHtml(resume.next_action || "none")}</strong>
          <span>${escapeHtml(resume.reason || "No pending action.")}</span>
        </div>
        ${hint}
      </div>`;
    }
    function historyCard(key, label, value, options = {}) {
      const count = Number(value || 0);
      const selected = historyFilter === key ? " selected" : "";
      const danger = options.danger ? " danger" : "";
      return `<button type="button" class="history-stat clickable${selected}${danger}" data-history-filter="${escapeHtml(key)}" onclick="selectHistoryFilter('${escapeHtml(key)}')" ${count ? "" : "disabled"}>
        <div class="history-label">${escapeHtml(label)}</div>
        <div class="history-value">${escapeHtml(count)}</div>
      </button>`;
    }
    function selectHistoryFilter(key) {
      historyFilter = historyFilter === key ? null : key;
      if (historyFilter && !historyVisibleCounts[historyFilter]) {
        historyVisibleCounts[historyFilter] = HISTORY_INITIAL_VISIBLE;
      }
      renderDashboard(latestPayload);
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
      const detail = text(item.reason || item.detail, "No detail recorded.");
      return `<button type="button" class="history-detail-item${tone}${selected}" onclick="selectRun('${escapeHtml(runId)}')">
        <div class="history-detail-main">
          <code>${escapeHtml(runId)}</code>
          <span>${escapeHtml(detail)}</span>
        </div>
        <div class="history-detail-meta">
          <span class="history-detail-status">${escapeHtml(text(item.status, "unknown"))}</span>
          <time>${escapeHtml(formatLocalDateTime(item.updated_at))}</time>
        </div>
      </button>`;
    }
    function renderHistoryDetails(history) {
      if (!historyFilter) return "";
      const groups = history.groups || {};
      const items = groups[historyFilter] || [];
      const label = HISTORY_FILTER_LABELS[historyFilter] || historyFilter;
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
          : `<div class="empty">No runs in this group.</div>`}
        ${items.length ? `<div class="history-detail-actions">
          ${visibleCount < items.length ? `<button type="button" class="history-more-btn" onclick="showMoreHistory('${escapeHtml(historyFilter)}')">Show 10 more${hiddenCount ? ` (${hiddenCount} left)` : ""}</button>` : ""}
          ${visibleCount > HISTORY_INITIAL_VISIBLE ? `<button type="button" class="history-more-btn" onclick="collapseHistory('${escapeHtml(historyFilter)}')">Collapse</button>` : ""}
        </div>` : ""}
      </div>`;
    }
    function renderHistory(payload) {
      const history = payload.history || {};
      const status = history.status || {};
      if (!history.total) {
        document.getElementById("history").innerHTML = `<div class="empty">No Phaseharness run history.</div>`;
        return;
      }
      const activeTotal = status.active || 0;
      document.getElementById("history").innerHTML = `
        <div class="history-grid">
          ${historyCard("all", "All runs", history.total)}
          ${historyCard("running", "Running now", activeTotal)}
          ${historyCard("resumable", "Can continue", history.resumable || 0)}
          ${historyCard("failed", "Failed", status.error || 0, { danger: true })}
        </div>
        ${renderHistoryDetails(history)}`;
    }
    function renderOutputs(payload) {
      const current = currentData(payload);
      if (!current) {
        document.getElementById("outputs").innerHTML = `<div class="empty">No active outputs.</div>`;
        return;
      }
      const rows = [];
      for (const item of current.outputs.artifacts || []) rows.push(["artifact", item.stage, fileState(item), item.path]);
      for (const item of current.outputs.phases || []) rows.push(["phase", item.phase_id, fileState(item), item.path]);
      document.getElementById("outputs").innerHTML = `<table><thead><tr><th>Kind</th><th>Name</th><th>State</th><th>Path</th></tr></thead><tbody>${rows.map(row => `<tr><td>${escapeHtml(row[0])}</td><td>${escapeHtml(row[1])}</td><td>${escapeHtml(row[2])}</td><td><code>${escapeHtml(row[3])}</code></td></tr>`).join("")}</tbody></table>`;
    }
    function renderDiagnostics(payload) {
      const diagnostics = currentData(payload)?.views?.diagnostics;
      const section = document.getElementById("diagnostics-section");
      if (!diagnostics) {
        section.hidden = true;
        document.getElementById("diagnostics").innerHTML = `<div class="empty">No diagnostics.</div>`;
        return;
      }
      const validation = diagnostics.validation || {};
      const hasDiagnostics =
        text(diagnostics.intent_alignment?.status, "pending") !== "pending" ||
        text(diagnostics.guidance_compliance?.status, "pending") !== "pending" ||
        (validation.commands_found || []).length > 0 ||
        (validation.failed_commands || []).length > 0;
      section.hidden = !hasDiagnostics;
      if (!hasDiagnostics) return;
      document.getElementById("diagnostics").innerHTML = `<div class="path-list check-list">
        <div class="path-row"><strong>Requirements</strong><span>${escapeHtml(diagnostics.intent_alignment?.status)}</span></div>
        <div class="path-row"><strong>Guidance</strong><span>${escapeHtml(diagnostics.guidance_compliance?.status)}</span></div>
        <div class="path-row"><strong>Checks</strong><span>${(validation.commands_found || []).length}</span></div>
        <div class="path-row"><strong>Failed</strong><span>${(validation.failed_commands || []).length}</span></div>
      </div>`;
    }
    function renderFeedback(payload) {
      const counts = currentData(payload)?.views?.feedback?.counts;
      const section = document.getElementById("feedback-section");
      if (!counts) {
        section.hidden = true;
        document.getElementById("feedback").innerHTML = `<div class="empty">No feedback counts.</div>`;
        return;
      }
      const hasFeedback = Boolean(
        (counts.evaluate_failures || 0) ||
        (counts.followup_phases || 0) ||
        (counts.loop_retries || 0) ||
        (counts.explicit_post_completion_feedback || 0)
      );
      section.hidden = !hasFeedback;
      if (!hasFeedback) return;
      document.getElementById("feedback").innerHTML = `<div class="path-list">
        <div class="path-row"><strong>Evaluate failures</strong><span>${counts.evaluate_failures || 0}</span></div>
        <div class="path-row"><strong>Follow-ups</strong><span>${counts.followup_phases || 0}</span></div>
        <div class="path-row"><strong>Loop retries</strong><span>${counts.loop_retries || 0}</span></div>
        <div class="path-row"><strong>Post completion</strong><span>${counts.explicit_post_completion_feedback || 0}</span></div>
      </div>`;
    }
    function renderRecent(payload) {
      const runs = payload.recent_runs || [];
      const selected = selectedSummary(payload);
      const selectedId = selected?.run_id || payload.active_run;
      if (!runs.length) {
        document.getElementById("recent").innerHTML = `<div class="empty">No recent runs.</div>`;
        return;
      }
      document.getElementById("recent").innerHTML = `<table><thead><tr><th>Run</th><th>Status</th><th>Stage</th><th>Phase</th><th>Updated</th><th>Resume</th></tr></thead><tbody>${runs.map(run => {
        const resume = resumeRequest(run.run_id);
        const action = canResumeRun(run)
          ? `<div class="resume-action"><code>${escapeHtml(resume)}</code>${copyButton(resume)}</div>`
          : "done";
        const classes = ["run-row"];
        if (run.run_id === selectedId) classes.push("selected");
        return `<tr class="${classes.join(" ")}" onclick="selectRun('${escapeHtml(run.run_id)}')"><td><code>${escapeHtml(run.run_id)}</code></td><td>${escapeHtml(run.status)}</td><td>${escapeHtml(run.current_stage)}</td><td>${escapeHtml(run.current_phase || "none")}</td><td>${escapeHtml(formatLocalDateTime(run.updated_at || run.created_at))}</td><td>${action}</td></tr>`;
      }).join("")}</tbody></table>`;
    }
    function renderDashboard(payload) {
      if (!payload) return;
      if (selectedRunId && !payload.run_details?.[selectedRunId]) selectedRunId = null;
      renderHeader(payload);
      renderFlow(payload);
      renderMetrics(payload);
      renderCurrent(payload);
      renderHistory(payload);
      renderOutputs(payload);
      renderDiagnostics(payload);
      renderFeedback(payload);
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
        document.getElementById("subtitle").textContent = `Dashboard refresh failed: ${error}`;
      }
    }
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
