"""
Knowledge Curator — answers queries from agents and grows APEX's institutional memory.
See docs/contracts/knowledge_curator.md for the full contract.
"""
import json
import re
from pathlib import Path

from models.domain import KnowledgeAnswer, KnowledgeQuery
import services.claude_code as claude_code
import services.github as github_service
import services.db as db_service
import services.slack as slack_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "knowledge_curator.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def query(question: str, project_id: str | None = None) -> KnowledgeAnswer:
    """Answer a knowledge query from any agent using stored decisions and patterns."""
    stored = db_service.search_knowledge(query_tags=[], project=project_id)
    stored_context = [
        {"source": k.source_type, "ref": k.source_ref or "", "content": k.content[:500]}
        for k in stored[:10]
    ]

    user_prompt = json.dumps({
        "mode": "query",
        "question": question,
        "project": project_id,
        "stored_knowledge": stored_context,
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    status = data.get("status", "not_found")
    answer_data = data.get("answer") or {}

    if isinstance(answer_data, dict):
        answer_text = answer_data.get("direct_answer", data.get("gap_description", "No relevant knowledge found."))
        sources = [
            d.get("adr_or_pr", "") for d in answer_data.get("decisions", []) if isinstance(d, dict)
        ] + [
            p.get("source", "") for p in answer_data.get("patterns", []) if isinstance(p, dict)
        ]
    else:
        answer_text = str(answer_data) if answer_data else "No relevant knowledge found."
        sources = []

    has_gaps = status in ("not_found", "partial")

    return KnowledgeAnswer(
        question=question,
        answer=answer_text,
        sources=[s for s in sources if s],
        has_gaps=has_gaps,
    )


def store_from_pr(pr_number: int, repo: str) -> None:
    """Index a merged PR and extract knowledge to store."""
    try:
        pr = github_service.get_pr(repo, pr_number)
        diff = github_service.get_pr_diff(repo, pr_number)
    except Exception as e:
        slack_service.send_text(f"⚠️ Knowledge Curator: could not read PR #{pr_number} in {repo}: {e}")
        return

    user_prompt = json.dumps({
        "mode": "index",
        "event_type": "pr_merged",
        "source": {
            "pr_number": pr_number,
            "repo": repo,
            "title": pr.get("title", ""),
            "diff_excerpt": diff[:3000],
        },
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        content = json.dumps(item)
        db_service.save_knowledge(
            source_type=item.get("type", "pattern"),
            content=content,
            source_ref=f"PR #{pr_number}",
            project=repo,
            tags=[item.get("topic", ""), item.get("type", "")],
        )


def store_adr(adr_path: str) -> None:
    """Index a new or updated Architecture Decision Record."""
    path = Path(adr_path)
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")

    user_prompt = json.dumps({
        "mode": "index",
        "event_type": "adr_created",
        "source": {
            "path": adr_path,
            "content": content[:3000],
        },
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        db_service.save_knowledge(
            source_type="adr",
            content=json.dumps(item),
            source_ref=adr_path,
            tags=["adr", item.get("topic", "")],
        )


def flag_gap(kq: KnowledgeQuery) -> None:
    """Signal to Engineering Manager that no knowledge exists for this query."""
    slack_service.send_text(
        f"⚠️ *Knowledge gap detected*\n"
        f"Question: {kq.question}\n"
        f"Project: {kq.project_id or 'all'}\n"
        f"Consider creating an ADR to document this pattern."
    )
