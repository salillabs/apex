"""
QA Agent — runs tests, interprets results, writes bug reports.
See docs/contracts/qa.md for the full contract.
"""
import json
import re
from pathlib import Path

from models.domain import BugReport, ImplementationReport, Issue, TestResult
import services.claude_code as claude_code
import services.db as db_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "qa.md").read_text(encoding="utf-8")

_TRANSIENT_KEYWORDS = {"network", "timeout", "connection refused", "econnrefused", "etimedout", "port"}


def run_tests(report: ImplementationReport) -> TestResult:
    """Trigger test suite in the managed project directory and collect results."""
    project = db_service.get_project(report.issue.repo)
    if not project or not project.local_path:
        raise ValueError(f"No local_path configured for '{report.issue.repo}' in projects.yaml")
    cwd = Path(project.local_path)

    prompt = (
        "Run the full test suite.\n"
        "Command: npm test (or whatever the test script is in package.json)\n"
        "Do not modify any code.\n"
        "Capture complete output including: test names, pass/fail status, error messages, stack traces.\n\n"
        "After running, output a JSON block with this exact structure:\n"
        "{\n"
        '  "tests_run": <number>,\n'
        '  "tests_passed": <number>,\n'
        '  "tests_failed": <number>,\n'
        '  "failures": [\n'
        "    {\n"
        '      "test_name": "...",\n'
        '      "file": "...",\n'
        '      "error": "exact error message",\n'
        '      "stack_excerpt": "first relevant stack frame",\n'
        '      "plain_english": "one sentence explanation for a non-technical reader"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    raw_output = claude_code.execute(prompt, cwd)
    data = _parse_test_output(raw_output)

    result = TestResult(
        branch=report.branch,
        tests_run=data.get("tests_run", 0),
        tests_passed=data.get("tests_passed", 0),
        tests_failed=data.get("tests_failed", 0),
        failures=data.get("failures", []),
        success=data.get("tests_failed", 1) == 0,
    )

    db_service.save_test_result(
        task_id=_task_id(report.issue),
        branch=report.branch,
        tests_run=result.tests_run,
        tests_passed=result.tests_passed,
        tests_failed=result.tests_failed,
        failures=result.failures,
        success=result.success,
        raw_output=raw_output[:5000],
    )
    return result


def evaluate(result: TestResult) -> bool:
    """Return True if all tests pass and implementation can proceed to Reviewer."""
    return result.success and result.tests_failed == 0


def write_bug_report(result: TestResult) -> BugReport:
    """Produce a structured bug report with failure details and user options."""
    user_prompt = json.dumps({
        "type": "write_bug_report",
        "test_result": {
            "branch": result.branch,
            "tests_run": result.tests_run,
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "failures": result.failures,
        },
    })

    # Ask Claude to diagnose failures — extract any additional plain-English causes it surfaces
    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        import re as _re
        extra = _re.findall(r"(?:likely cause|root cause|reason)[:\s]+([^\n]+)", raw, _re.IGNORECASE)
    except Exception:
        extra = []

    likely_causes = [
        f.get("plain_english", f.get("error", ""))
        for f in result.failures
        if f.get("plain_english") or f.get("error")
    ]
    for e in extra:
        if e.strip() and e.strip() not in likely_causes:
            likely_causes.append(e.strip())
    likely_causes = likely_causes or ["See failure details above"]

    summary = (
        f"Branch: {result.branch} | "
        f"Tests: {result.tests_run} run, {result.tests_passed} passed, {result.tests_failed} failed"
    )

    return BugReport(
        test_result=result,
        summary=summary,
        likely_causes=likely_causes,
        options=["retry_fix", "abandon", "override"],
    )


def retry_transient(result: TestResult, repo: str) -> TestResult:
    """Rerun tests once if all failures look transient (network, timeout, race condition)."""
    if not result.failures:
        return result

    all_transient = all(
        any(kw in f.get("error", "").lower() for kw in _TRANSIENT_KEYWORDS)
        for f in result.failures
    )
    if not all_transient:
        return result

    from datetime import datetime
    dummy_issue = Issue(id=0, number=0, title="retry", body="", repo=repo, labels=[], created_at=datetime.utcnow())
    dummy_report = ImplementationReport(issue=dummy_issue, branch=result.branch, files_changed=[], summary="transient retry", success=False)
    return run_tests(dummy_report)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _task_id(issue: Issue) -> str:
    slug = issue.repo.replace("/", "-").replace("_", "-")
    return f"task-{issue.number}-{slug}"


def _parse_test_output(raw: str) -> dict:
    """Extract the JSON block from Claude Code's test output."""
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Fallback: scrape numbers from text
    run_m = re.search(r"(\d+)\s+tests?\s+ran", raw, re.IGNORECASE)
    pass_m = re.search(r"(\d+)\s+passed", raw, re.IGNORECASE)
    fail_m = re.search(r"(\d+)\s+failed", raw, re.IGNORECASE)

    tests_run = int(run_m.group(1)) if run_m else 0
    tests_passed = int(pass_m.group(1)) if pass_m else 0
    tests_failed = int(fail_m.group(1)) if fail_m else 0

    return {
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "failures": [],
    }
