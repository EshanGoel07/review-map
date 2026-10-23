"""GitHub Action entry point: analyze the PR that triggered the run and upsert one comment.

Everything about the PR comes from the event file GitHub writes for the run
(`GITHUB_EVENT_PATH`), and the code from the workflow's own checkout, so the
only API calls are for the comment itself.

Untrusted text: PR titles, descriptions, paths and names can contain `::command::`
lines, which the Actions runner would execute if they reached the log. Anything
derived from the PR is printed between `::stop-commands::` markers.
"""

import json
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from review_map.diff import git_source
from review_map.diff.github_pr import PRRef, PullRequest, load_pr_config, pr_from_api
from review_map.errors import EXIT_GATE_FAILED, EXIT_OK, ReviewMapError
from review_map.github.client import API_URL, GitHubClient, GitHubPermissionError
from review_map.pipeline import PR_DESCRIPTION, PR_NO_DESCRIPTION_NOTE, plan_for, pr_change
from review_map.report.plan import gate_failed
from review_map.report.render_md import MARKER, render_markdown

BOT_LOGIN = "github-actions[bot]"  # who comments when the token is the run's GITHUB_TOKEN
MAX_COMMENT_CHARS = 65_000  # GitHub's limit is 65,536
MAX_SUMMARY_CHARS = 1_000_000  # job summaries are capped at 1 MiB per step

FORK_NOTE = (
    "This PR comes from a fork. GitHub gives fork PRs no secrets, so the LLM parser "
    "was not available and the claims are approximate."
)
TRUNCATED_NOTE = (
    "> The plan was cut to fit GitHub's comment size limit. "
    "The full plan is in the workflow run's job summary."
)


class ActionError(ReviewMapError):
    """The Action can't run: no event, not a PR, missing commits or token."""


def annotate(level: str, message: str, out: TextIO) -> None:
    """A workflow annotation (`notice`, `warning`, `error`) shown on the run's page."""
    text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title=Review Map::{text}", file=out, flush=True)


@contextmanager
def stop_commands(out: TextIO, enabled: bool) -> Iterator[None]:
    """Make the runner ignore `::command::` lines printed inside this block."""
    if not enabled:
        yield
        return
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", file=out, flush=True)
    try:
        yield
    finally:
        print(f"::{token}::", file=out, flush=True)


def read_event(path: Path | None) -> PullRequest:
    if path is None:
        raise ActionError(
            "No GitHub event: GITHUB_EVENT_PATH is not set.",
            hint="This command runs inside GitHub Actions. To try it locally, pass "
            "--event FILE --dry-run.",
        )
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ActionError(f"Event file not found: {path}") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ActionError(f"Could not read the event file {path}: {exc}") from None

    data = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        raise ActionError(
            "This workflow run is not for a pull request.",
            hint="Trigger the workflow with `on: pull_request` (see examples/workflow.yml).",
        )
    try:
        owner, repo = data["base"]["repo"]["full_name"].split("/", 1)
        ref = PRRef(owner, repo, int(data["number"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ActionError("The event's pull request is missing expected fields.") from None
    return pr_from_api(ref, data)


def _ensure_commits(root: Path, pr: PullRequest) -> None:
    try:
        git_source.resolve_ref(root, pr.base_sha)
        git_source.resolve_ref(root, pr.head_sha)
        git_source.merge_base(root, pr.base_sha, pr.head_sha)
    except ReviewMapError:
        raise ActionError(
            "The checkout is missing this PR's commits.",
            hint="Use actions/checkout with `fetch-depth: 0` (see examples/workflow.yml).",
        ) from None


def fit(markdown: str, limit: int, note: str = TRUNCATED_NOTE) -> str:
    """Cut `markdown` at a line break to fit `limit`, keeping it well-formed and marked."""
    if len(markdown) <= limit:
        return markdown
    tail = f"\n\n{note}\n\n{MARKER}\n"
    cut = markdown.rfind("\n", 0, limit - len(tail) - len("\n</details>"))
    body = markdown[: max(cut, 0)]
    if body.count("<details>") > body.count("</details>"):
        body += "\n</details>"
    return body + tail


def find_own_comment(comments: list[Any], login: str) -> dict | None:
    """Our earlier comment: it has the marker *and* we wrote it.

    The marker alone isn't enough: anyone can paste it into their own comment.
    """
    for c in comments:
        if not isinstance(c, dict):
            continue
        author = (c.get("user") or {}).get("login")
        if MARKER in (c.get("body") or "") and author == login:
            return c
    return None


@dataclass
class Upsert:
    outcome: str  # "created", "updated" or "unchanged"
    url: str


def upsert_comment(client: GitHubClient, pr: PullRequest, body: str) -> Upsert:
    """Create the reading-plan comment, or update ours in place. Never a second one."""
    login = client.login() or BOT_LOGIN
    comments = client.get_all(
        f"/repos/{pr.repo}/issues/{pr.number}/comments", what=f"comments on {pr.ref}"
    )
    own = find_own_comment(comments, login)
    if own is None:
        made = client.post_json(
            f"/repos/{pr.repo}/issues/{pr.number}/comments",
            {"body": body},
            what=f"a comment on {pr.ref}",
        )
        return Upsert("created", str(made.get("html_url", "")))
    if own.get("body") == body:
        return Upsert("unchanged", str(own.get("html_url", "")))
    made = client.patch_json(
        f"/repos/{pr.repo}/issues/comments/{own['id']}",
        {"body": body},
        what=f"the Review Map comment on {pr.ref}",
    )
    return Upsert("updated", str(made.get("html_url", "")))


def write_summary(markdown: str, env: Mapping[str, str]) -> bool:
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    text = fit(markdown, MAX_SUMMARY_CHARS, "> The plan was cut to fit the summary.")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        return False
    return True


def run(
    *,
    event: Path | None,
    repo: Path,
    dry_run: bool,
    fail_on: str | None,
    budget: int | None,
    no_llm: bool,
    out: TextIO,
    env: Mapping[str, str] | None = None,
    transport: Any = None,
) -> int:
    """Run the Action. Returns the exit code: 0 ok, 1 gate failed."""
    env = os.environ if env is None else env
    in_actions = env.get("GITHUB_ACTIONS") == "true"
    pr = read_event(event)
    root = git_source.find_repo_root(repo)
    _ensure_commits(root, pr)

    loaded = load_pr_config(root, pr.base_sha, None)  # base, never the PR's own config
    if budget is not None:
        loaded.config.reading_budget.max_units = budget
    change = pr_change(root, pr, loaded)
    result = plan_for(
        change,
        pr.description,
        PR_DESCRIPTION,
        None,
        no_llm=no_llm,
        no_cache=False,
        missing_note=PR_NO_DESCRIPTION_NOTE,
    )
    plan = result.plan
    if pr.from_fork and result.claims is not None and not result.claims.model and not no_llm:
        plan.notes.append(FORK_NOTE)

    markdown = render_markdown(plan)
    comment = fit(markdown, MAX_COMMENT_CHARS)
    summary = write_summary(markdown, env)
    s = plan.summary
    print(
        f"Review Map: {s.read_units} of {s.units_changed} changed sections to read carefully, "
        f"{len(plan.unmentioned) + len(plan.file_violations)} unmentioned.",
        file=out,
    )

    if dry_run:
        with stop_commands(out, in_actions):
            print(comment, file=out)
    else:
        token = env.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise ActionError(
                "GITHUB_TOKEN is not set, so the comment can't be posted.",
                hint="The action passes the run's token by default; check the `github-token` "
                "input, or use --dry-run to print the comment instead.",
            )
        api_url = env.get("GITHUB_API_URL") or API_URL  # GitHub Enterprise sets its own
        try:
            with GitHubClient(token, base_url=api_url, transport=transport) as client:
                done = upsert_comment(client, pr, comment)
            print(f"Comment {done.outcome}: {done.url}", file=out)
        except GitHubPermissionError as exc:
            if not pr.from_fork:
                raise
            where = " It is in the job summary." if summary else ""
            annotate(
                "warning",
                "Can't comment on a PR from a fork: GitHub gives fork PRs a read-only "
                f"token.{where} {exc.message}",
                out,
            )

    failing = gate_failed(plan, fail_on)
    if failing:
        message = f"{len(failing)} section(s) at {fail_on} priority or above (--fail-on {fail_on})."
        if in_actions:
            annotate("error", f"Gate failed: {message}", out)
        else:
            print(f"Gate failed: {message}", file=out)
        return EXIT_GATE_FAILED
    return EXIT_OK
