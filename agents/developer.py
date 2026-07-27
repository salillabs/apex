"""
Developer — executes the technical spec via Claude Code. Makes zero design decisions.
See docs/contracts/developer.md for the full contract.
"""
import re
from pathlib import Path

from models.domain import AgentResult, ImplementationReport, Issue, SimpleTask, TechnicalSpec
import services.claude_code as claude_code
import services.db as db_service


def _task_id(issue: Issue) -> str:
    slug = issue.repo.replace("/", "-").replace("_", "-")
    return f"task-{issue.number}-{slug}"


def _repo_dir(repo: str) -> Path:
    project = db_service.get_project(repo)
    if not project or not project.local_path:
        raise ValueError(f"No local_path configured for '{repo}' in projects.yaml")
    return Path(project.local_path)


def implement(spec: TechnicalSpec) -> ImplementationReport:
    """Trigger Claude Code with the spec and monitor execution to completion."""
    cwd = _repo_dir(spec.issue.repo)
    prompt = _build_spec_prompt(spec)
    branch = _branch_for_issue(spec.issue)

    output, success = _run_with_retry(prompt, cwd)

    files = _extract_files(output)
    report = ImplementationReport(
        issue=spec.issue,
        branch=branch,
        files_changed=files,
        summary=_extract_summary(output),
        success=success,
        error=None if success else output[:500],
    )
    db_service.save_implementation(
        task_id=_task_id(spec.issue),
        branch=branch,
        files_changed=files,
        summary=report.summary,
        success=success,
        error=report.error,
    )
    return report


def implement_simple(task: SimpleTask) -> ImplementationReport:
    """For trivial tasks — trigger Claude Code without a full spec."""
    cwd = _repo_dir(task.issue.repo)
    prompt = (
        f"Make this single change and nothing else:\n{task.description}\n\n"
        f"Branch: {task.branch}\n\n"
        "After making the change:\n"
        "Run: npx tsc --noEmit\n"
        "Report whether it passed.\n"
        "Report which file you changed and what you changed."
    )

    output, success = _run_with_retry(prompt, cwd)

    files = _extract_files(output)
    report = ImplementationReport(
        issue=task.issue,
        branch=task.branch,
        files_changed=files,
        summary=_extract_summary(output),
        success=success,
        error=None if success else output[:500],
    )
    db_service.save_implementation(
        task_id=_task_id(task.issue),
        branch=task.branch,
        files_changed=files,
        summary=report.summary,
        success=success,
        error=report.error,
    )
    return report


def retry(report: ImplementationReport, error_context: str) -> ImplementationReport:
    """Retry implementation once after a failure with error context added."""
    cwd = _repo_dir(report.issue.repo)
    prompt = (
        f"The previous implementation failed. Fix only these errors:\n\n{error_context}\n\n"
        "Do not change anything unrelated to these errors.\n"
        "After fixing, run: npx tsc --noEmit\n"
        "Report which files you changed and the result."
    )

    try:
        output = claude_code.execute(prompt, cwd)
        success = _is_success(output)
        files = _extract_files(output)
        new_report = ImplementationReport(
            issue=report.issue,
            branch=report.branch,
            files_changed=files,
            summary=_extract_summary(output),
            success=success,
            error=None if success else output[:500],
        )
        db_service.save_implementation(
            task_id=_task_id(report.issue),
            branch=report.branch,
            files_changed=files,
            summary=new_report.summary,
            success=success,
            error=new_report.error,
            attempt_number=2,
        )
        if not success:
            db_service.increment_failure_count(_task_id(report.issue))
        return new_report
    except Exception as e:
        return ImplementationReport(
            issue=report.issue, branch=report.branch,
            files_changed=[], summary="", success=False, error=str(e),
        )


def escalate_spec_gap(spec: TechnicalSpec, gap_description: str) -> AgentResult:
    """Signal back to Architect when spec has a gap or conflict. Never improvise."""
    return AgentResult(
        success=False,
        next_agent="architect",
        payload={"task_id": _task_id(spec.issue), "gap_description": gap_description, "from_agent": "developer"},
        error=gap_description,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_spec_prompt(spec: TechnicalSpec) -> str:
    files_to_create = "\n".join(
        f"Create {f['path']}:\n{f.get('description', '')}" for f in spec.files_to_create
    )
    files_to_modify = "\n".join(
        f"Modify {f['path']}:\n{f.get('change', '')}" for f in spec.files_to_modify
    )
    deps = "\n".join(
        f"npm install {d['name']}@{d.get('version', 'latest')}" for d in spec.dependencies
    )
    scope = "\n".join(f"- {r}" for r in spec.risks)

    return (
        "You are implementing a specific technical spec. Follow it exactly.\n"
        "Do not add features, refactor unrelated code, or deviate in any way.\n\n"
        f"## What to build\n{spec.summary}\n\n"
        f"## Files to create\n{files_to_create}\n\n"
        f"## Files to modify\n{files_to_modify}\n\n"
        f"## Dependencies to install\n{deps}\n\n"
        f"## Scope boundaries — do NOT do any of these\n{scope}\n\n"
        "## When you are done\n"
        "Run: npx tsc --noEmit\n"
        "Report any type errors — do not ignore them.\n"
        "Report the exact list of files you created or modified."
    )


def _run_with_retry(prompt: str, cwd: Path) -> tuple[str, bool]:
    """Run Claude Code, auto-retry once on failure. Returns (output, success)."""
    output = claude_code.execute(prompt, cwd)
    if _is_success(output):
        return output, True

    retry_prompt = (
        f"The previous implementation had these errors:\n{output}\n\n"
        "Fix only these errors. The original task and scope still apply.\n"
        "Run: npx tsc --noEmit after fixing. Report result."
    )
    output = claude_code.execute(retry_prompt, cwd)
    return output, _is_success(output)


def _is_success(output: str) -> bool:
    if not output.strip():
        return False  # empty output means something went wrong
    low = output.lower()
    # Explicit failure signals
    if re.search(r"found \d+ error", low):
        return False
    if any(kw in low for kw in ("error ts2", "error ts1", "cannot find module", "module not found")):
        return False
    # Explicit success signals
    if re.search(r"tsc.*0 errors?|0 errors?.*tsc", low):
        return True
    if "typescript check: passed" in low or "tsc passed" in low:
        return True
    # If Claude reports files modified/created with no error keywords, treat as success
    has_output = any(kw in low for kw in ("created:", "modified:", "changed:"))
    has_error = any(kw in low for kw in ("error:", "failed:", "exception:"))
    if has_output and not has_error:
        return True
    return False


def _extract_files(output: str) -> list[str]:
    files: list[str] = []
    for line in output.splitlines():
        for prefix in ("Created:", "Modified:", "created:", "modified:"):
            if line.strip().startswith(prefix):
                files.append(line.strip()[len(prefix):].strip())
    return files


def _extract_summary(output: str) -> str:
    meaningful = [l for l in output.splitlines() if l.strip() and not l.startswith(" ")]
    return " ".join(meaningful[:3])[:500]


def _branch_for_issue(issue: Issue) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", "", issue.title.lower()).split()][:3]
    return f"feature/issue-{issue.number}-{'-'.join(words)}"
