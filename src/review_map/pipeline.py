"""The analysis pipeline shared by `analyze`, `pr` and the GitHub Action.

diff → units → risk scores (rules + model) → claims → scope check → reading plan.
Each step is its own module; this file only wires them together.
"""

from dataclasses import dataclass
from pathlib import Path

from review_map.claims.parser import parse_claims
from review_map.code.functions import collect_units
from review_map.config import LoadedConfig
from review_map.diff import git_source
from review_map.diff.github_pr import PullRequest
from review_map.diff.parser import parse_diff
from review_map.models import ChangedFile, ChangedUnit, ClaimedScope, ReadingPlan
from review_map.report.plan import NO_DESCRIPTION_NOTE, build_plan
from review_map.risk.rules import score_units
from review_map.risk.scorer import (
    REPO_MODEL_DIR,
    change_context,
    choose_model,
    commit_reader,
    folder_reader,
    model_scores,
)
from review_map.verify.scope import verify_scope

PR_DESCRIPTION = "PR description"
PR_NO_DESCRIPTION_NOTE = (
    "This PR has no description, so the scope check was skipped. "
    "Only the risk score ranks the changes."
)


@dataclass
class LocalChange:
    root: Path
    base: str
    head: str
    merge_base: str
    head_sha: str
    loaded: LoadedConfig
    files: list[ChangedFile]
    # False for PRs: their working tree is untrusted, so a repo model is read from the
    # base commit (like the config), never from the PR's own files.
    trusted_tree: bool = True


def pr_change(root: Path, pr: PullRequest, loaded: LoadedConfig) -> LocalChange:
    """The change a PR proposes, from a repo that has both of its commits."""
    files = parse_diff(git_source.get_diff(root, pr.base_sha, pr.head_sha), loaded.config)
    return LocalChange(
        root=root,
        base=pr.base_ref,
        head=pr.head_label,
        merge_base=git_source.merge_base(root, pr.base_sha, pr.head_sha),
        head_sha=pr.head_sha,
        loaded=loaded,
        files=files,
        trusted_tree=False,
    )


def collect(change: LocalChange) -> list[ChangedUnit]:
    return collect_units(
        change.files,
        read_old=lambda p: git_source.read_file(change.root, change.merge_base, p),
        read_new=lambda p: git_source.read_file(change.root, change.head_sha, p),
    )


@dataclass
class PlanRun:
    plan: ReadingPlan
    claims: ClaimedScope | None
    cached: bool


def plan_for(
    change: LocalChange,
    description: str | None,
    label: str | None,
    rel: str | None,
    *,
    no_llm: bool,
    no_cache: bool,
    missing_note: str = NO_DESCRIPTION_NOTE,
    use_model: bool = True,
) -> PlanRun:
    """Score, check the description (if any) against the diff, and build the plan.

    `label` names where the description came from; `rel` is its path in the repo,
    if it is a file there, so the verifier doesn't count it as an unclaimed change.
    """
    changed = collect(change)
    scored = score_units(changed, change.files, change.loaded.config)
    if change.trusted_tree:
        folder = change.root / REPO_MODEL_DIR
        choice = choose_model(folder_reader(folder), str(folder), use_model)
    else:
        reader = commit_reader(change.root, change.merge_base)
        choice = choose_model(reader, f"{REPO_MODEL_DIR} (base commit)", use_model)
    scores = None
    if choice.model is not None:
        ctx = change_context(change.root, change.merge_base, change.head_sha, change.files)
        scores = model_scores(choice.model, scored, ctx)
    claims = scope = None
    cached = False
    if description is not None:
        claims, cached = parse_claims(
            description, change.loaded.config.llm, use_llm=not no_llm, use_cache=not no_cache
        )
        scope = verify_scope(claims, changed, change.files, rel)
    plan = build_plan(
        scored,
        change.files,
        change.loaded.config.reading_budget,
        base=change.base,
        head=change.head,
        base_sha=change.merge_base,
        head_sha=change.head_sha,
        claims=claims,
        scope=scope,
        description=label,
        missing_description_note=missing_note,
        model_scores=scores,
        scorer=choice.model.label if choice.model else "rules",
    )
    plan.notes += choice.notes
    plan.scorer_calibrated = bool(choice.model and choice.model.calibrated)
    return PlanRun(plan, claims, cached)
