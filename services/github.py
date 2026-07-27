import os
import requests
from github import Github, GithubException
from models.domain import Issue

_client: Github | None = None


def _get() -> Github:
    global _client
    if _client is None:
        _client = Github(os.environ["GITHUB_TOKEN"])
    return _client


def get_issue(repo: str, issue_number: int) -> Issue:
    gh_issue = _get().get_repo(repo).get_issue(issue_number)
    return Issue(
        id=gh_issue.id,
        number=gh_issue.number,
        title=gh_issue.title,
        body=gh_issue.body or "",
        repo=repo,
        labels=[label.name for label in gh_issue.labels],
        created_at=gh_issue.created_at,
    )


def list_open_issues(repo: str) -> list[Issue]:
    gh_repo = _get().get_repo(repo)
    return [
        Issue(
            id=i.id,
            number=i.number,
            title=i.title,
            body=i.body or "",
            repo=repo,
            labels=[label.name for label in i.labels],
            created_at=i.created_at,
        )
        for i in gh_repo.get_issues(state="open")
        if i.pull_request is None
    ]


def create_branch(repo: str, branch_name: str, base: str = "main") -> str:
    gh_repo = _get().get_repo(repo)
    base_sha = gh_repo.get_branch(base).commit.sha
    gh_repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
    return branch_name


def add_comment(repo: str, issue_number: int, body: str) -> None:
    _get().get_repo(repo).get_issue(issue_number).create_comment(body)


def close_issue(repo: str, issue_number: int, comment: str | None = None) -> None:
    issue = _get().get_repo(repo).get_issue(issue_number)
    if comment:
        issue.create_comment(comment)
    issue.edit(state="closed")


def create_pr(repo: str, title: str, body: str, branch: str, base: str = "main") -> dict:
    pr = _get().get_repo(repo).create_pull(title=title, body=body, head=branch, base=base)
    return {"number": pr.number, "url": pr.html_url, "title": pr.title}


def get_pr(repo: str, pr_number: int) -> dict:
    pr = _get().get_repo(repo).get_pull(pr_number)
    return {
        "number": pr.number,
        "url": pr.html_url,
        "title": pr.title,
        "state": pr.state,
        "mergeable": pr.mergeable,
        "branch": pr.head.ref,
    }


def merge_pr(repo: str, pr_number: int) -> None:
    _get().get_repo(repo).get_pull(pr_number).merge(merge_method="squash")


def get_branch_status(repo: str, branch_name: str) -> dict:
    try:
        branch = _get().get_repo(repo).get_branch(branch_name)
        return {"exists": True, "sha": branch.commit.sha}
    except GithubException:
        return {"exists": False, "sha": None}


def get_open_pr_for_branch(repo: str, branch: str) -> dict | None:
    """Return an open PR for the given head branch, or None if none exists."""
    owner = repo.split("/")[0]
    pulls = _get().get_repo(repo).get_pulls(state="open", head=f"{owner}:{branch}")
    for pr in pulls:
        return {"number": pr.number, "url": pr.html_url, "title": pr.title}
    return None


def get_pr_diff(repo: str, pr_number: int) -> str:
    """Return the unified diff of a PR as a string, using token auth for private repos."""
    pr = _get().get_repo(repo).get_pull(pr_number)
    resp = requests.get(
        pr.diff_url,
        headers={
            "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github.v3.diff",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def list_open_prs(repo: str) -> list[dict]:
    """Return all open PRs for a repo."""
    pulls = _get().get_repo(repo).get_pulls(state="open")
    return [{"number": pr.number, "title": pr.title, "url": pr.html_url, "branch": pr.head.ref} for pr in pulls]


def list_merged_prs(repo: str, limit: int = 20) -> list[dict]:
    """Return recently merged PRs."""
    pulls = _get().get_repo(repo).get_pulls(state="closed", sort="updated", direction="desc")
    result = []
    for pr in pulls:
        if pr.merged:
            result.append({"number": pr.number, "title": pr.title, "url": pr.html_url, "merged_at": str(pr.merged_at)})
        if len(result) >= limit:
            break
    return result


def post_review(repo: str, pr_number: int, body: str, approved: bool = True) -> None:
    event = "APPROVE" if approved else "REQUEST_CHANGES"
    _get().get_repo(repo).get_pull(pr_number).create_review(body=body, event=event)
