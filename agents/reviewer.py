"""
Reviewer — checks PR quality so user is never asked to approve something broken.
See docs/contracts/reviewer.md for the full contract.
"""
import json
import re
from pathlib import Path

from models.domain import ReviewResult, TestResult
import services.claude_code as claude_code
import services.github as github_service
import services.db as db_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "reviewer.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in reviewer response: {text[:200]}")


def review_pr(test_result: TestResult, repo: str, pr_number: int, task_id: str) -> ReviewResult:
    """Read the PR diff against the spec and check quality standards."""
    pr = github_service.get_pr(repo, pr_number)

    try:
        diff = github_service.get_pr_diff(repo, pr_number)
        diff_excerpt = diff[:4000]
    except Exception:
        diff_excerpt = "(diff unavailable)"

    spec = db_service.get_spec(task_id)

    user_prompt = json.dumps({
        "type": "review_pr",
        "pr_number": pr_number,
        "pr_url": pr["url"],
        "repo": repo,
        "diff": diff_excerpt,
        "spec": spec,
        "test_result": {
            "tests_run": test_result.tests_run,
            "tests_passed": test_result.tests_passed,
            "tests_failed": test_result.tests_failed,
        },
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    status = data.get("status", "approved")
    approved = status == "approved"

    issues_found: list[str] = []
    for issue in data.get("blocking_issues", []):
        desc = issue.get("issue") if isinstance(issue, dict) else str(issue)
        if desc:
            issues_found.append(desc)
    for issue in data.get("non_blocking_issues", []):
        desc = issue.get("issue") if isinstance(issue, dict) else str(issue)
        if desc:
            issues_found.append(f"[minor] {desc}")

    result = ReviewResult(
        pr_number=pr_number,
        pr_url=pr["url"],
        approved=approved,
        issues_found=issues_found,
        summary=data.get("summary", f"PR #{pr_number} reviewed — {'approved' if approved else 'changes requested'}."),
    )

    try:
        review_body = result.summary
        if issues_found:
            review_body += "\n\n**Issues:**\n" + "\n".join(f"- {i}" for i in issues_found)
        github_service.post_review(repo, pr_number, review_body, approved=approved)
    except Exception:
        pass

    return result


def approve(review_result: ReviewResult) -> ReviewResult:
    """Mark PR as approved and ready for user merge confirmation."""
    return ReviewResult(
        pr_number=review_result.pr_number,
        pr_url=review_result.pr_url,
        approved=True,
        issues_found=review_result.issues_found,
        summary=review_result.summary,
    )


def request_changes(review_result: ReviewResult, issues: list[str]) -> ReviewResult:
    """Send specific issues back to Developer before user sees the PR."""
    return ReviewResult(
        pr_number=review_result.pr_number,
        pr_url=review_result.pr_url,
        approved=False,
        issues_found=issues,
        summary=review_result.summary,
    )
