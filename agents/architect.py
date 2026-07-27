"""
Architect — produces a complete technical spec so Developer has zero design decisions.
See docs/contracts/architect.md for the full contract.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from models.domain import Issue, Plan, RiskAssessment, TechnicalSpec
import services.claude_code as claude_code
import services.github as github_service
import services.db as db_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "architect.md").read_text(encoding="utf-8")


def _task_id(issue: Issue) -> str:
    slug = issue.repo.replace("/", "-").replace("_", "-")
    return f"task-{issue.number}-{slug}"


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in architect response: {text[:200]}")


def generate_spec(plan: Plan) -> TechnicalSpec:
    """Read the codebase and produce a full implementation blueprint."""
    issue = plan.issue

    past_knowledge = db_service.search_knowledge(query_tags=[], project=issue.repo)
    knowledge_context = [
        {"source": k.source_type, "ref": k.source_ref, "content": k.content[:400]}
        for k in past_knowledge[:6]
        if k.source_type != "spec"
    ]

    user_prompt = json.dumps({
        "type": "generate_spec",
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "repo": issue.repo,
        },
        "plan_summary": plan.summary,
        "plan_context": plan.context,
        "past_decisions": knowledge_context,
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    deps: list[dict] = []
    for d in data.get("dependencies", []):
        if isinstance(d, dict):
            deps.append({"name": d.get("package", d.get("name", "")), "version": d.get("version", "latest")})
        elif isinstance(d, str):
            deps.append({"name": d, "version": "latest"})

    def _as_list(v: object) -> list:
        return v if isinstance(v, list) else []

    spec = TechnicalSpec(
        issue=issue,
        summary=data.get("summary", plan.summary),
        files_to_create=_as_list(data.get("files_to_create")),
        files_to_modify=_as_list(data.get("files_to_modify")),
        api_changes=_as_list(data.get("api_changes")),
        database_changes=_as_list(data.get("database_changes")),
        env_vars=_as_list(data.get("environment_variables")),
        dependencies=deps,
        test_requirements=_as_list(data.get("test_requirements")),
        risks=_as_list(data.get("risks")) + _as_list(data.get("scope_boundaries")),
        requires_approval=bool(data.get("requires_approval", False)),
    )

    db_service.save_spec(task_id=_task_id(issue), spec_json=spec.model_dump(mode="json"))

    try:
        github_service.add_comment(
            issue.repo,
            issue.number,
            f"**APEX Architect**: Spec generated\n\n{spec.summary}\n\n"
            f"Files to create: {len(spec.files_to_create)} · "
            f"Files to modify: {len(spec.files_to_modify)} · "
            f"Requires approval: {spec.requires_approval}",
        )
    except Exception:
        pass

    if data.get("adr_required"):
        try:
            draft_adr(
                decision=data.get("adr_description") or issue.title,
                context=spec.summary,
                rationale=data.get("summary", ""),
            )
        except Exception:
            pass

    return spec


def analyze_impact(plan: Plan) -> RiskAssessment:
    """Identify breaking changes, security concerns, and cost implications."""
    issue = plan.issue

    user_prompt = json.dumps({
        "type": "analyze_impact",
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "repo": issue.repo,
        },
        "plan_summary": plan.summary,
        "urgency": plan.urgency,
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    data = _extract_json(raw)

    return RiskAssessment(
        issue=issue,
        breaking_changes=data.get("breaking_changes", []),
        security_concerns=data.get("security_concerns", []),
        cost_implications=data.get("cost_implications", []),
        requires_user_approval=bool(data.get("requires_approval", False)),
        approval_reason=data.get("approval_reason"),
    )


def requires_approval(spec: TechnicalSpec, risk: RiskAssessment) -> bool:
    """Return True if any part of this spec needs user sign-off before proceeding."""
    return spec.requires_approval or risk.requires_user_approval


def draft_adr(decision: str, context: str, rationale: str) -> str:
    """Write an ADR to docs/adr/ and index it in knowledge."""
    adr_dir = Path(__file__).parent.parent / "docs" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(adr_dir.glob("ADR-*.md"))
    num = len(existing) + 1
    slug = re.sub(r"[^a-z0-9]+", "-", decision.lower())[:40].strip("-")
    filename = f"ADR-{num:03d}-{slug}.md"

    content = (
        f"# ADR-{num:03d}: {decision}\n\n"
        f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d')}\n"
        f"**Status:** Proposed\n\n"
        f"## Context\n\n{context}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Rationale\n\n{rationale}\n\n"
        f"## Consequences\n\n_To be determined after implementation._\n"
    )

    adr_path = adr_dir / filename
    adr_path.write_text(content, encoding="utf-8")

    db_service.save_knowledge(
        source_type="adr",
        content=content,
        source_ref=str(adr_path),
        tags=["adr", "architecture"],
    )

    return str(adr_path)
