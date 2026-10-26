"""Tests for the GitHub Action: event parsing, the single-comment upsert, forks, safety.

A fake GitHub API (`FakeGitHub`, behind `httpx2.MockTransport`) keeps comments in
memory; the "checkout" is a temp git repo. No network, no real tokens.
"""

import io
import json
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml
from typer.testing import CliRunner

from review_map import cli
from review_map.cli import app
from review_map.errors import EXIT_GATE_FAILED, EXIT_OK, EXIT_USER_ERROR, ReviewMapError
from review_map.github import action
from review_map.github.action import (
    BOT_LOGIN,
    FORK_NOTE,
    ActionError,
    annotate,
    find_own_comment,
    fit,
    read_event,
    stop_commands,
)
from review_map.github.client import GitHubClient, GitHubError, GitHubPermissionError
from review_map.report.render_md import MARKER
from tests.conftest import FIXTURES, GitRepo
from tests.test_cli_verify import CHANGES_MD, _make_feature

ROOT = Path(__file__).parents[1]
EVENT_TEMPLATE = FIXTURES / "events" / "pull_request.json"
runner = CliRunner()


# --- Fake GitHub ----------------------------------------------------------------------


class FakeGitHub:
    """Just enough of the REST API for comments: list (paginated), create, update, /user."""

    def __init__(self, page_size: int = 100) -> None:
        self.comments: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.page_size = page_size
        self.login: str | None = None  # None: /user answers 403, like an Actions token
        self.write_status = 0  # e.g. 403 to refuse writes
        self.next_id = 100

    def add(self, body: str, login: str) -> dict:
        self.next_id += 1
        comment = {
            "id": self.next_id,
            "body": body,
            "user": {"login": login},
            "html_url": f"https://github.com/acme/shop/pull/7#issuecomment-{self.next_id}",
        }
        self.comments.append(comment)
        return comment

    def writes(self) -> list[str]:
        return [r.method for r in self.requests if r.method in ("POST", "PATCH")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/user":
            if self.login is None:
                return httpx.Response(
                    403, json={"message": "Resource not accessible by integration"}
                )
            return httpx.Response(200, json={"login": self.login})
        if method in ("POST", "PATCH") and self.write_status:
            return httpx.Response(
                self.write_status, json={"message": "Resource not accessible by integration"}
            )
        if path == "/repos/acme/shop/issues/7/comments" and method == "GET":
            page = int(request.url.params.get("page", "1"))
            start = (page - 1) * self.page_size
            chunk = self.comments[start : start + self.page_size]
            headers = {}
            if start + self.page_size < len(self.comments):
                nxt = request.url.copy_merge_params({"page": str(page + 1)})
                headers["link"] = f'<{nxt}>; rel="next"'
            return httpx.Response(200, json=chunk, headers=headers)
        if path == "/repos/acme/shop/issues/7/comments" and method == "POST":
            body = json.loads(request.content)["body"]
            return httpx.Response(201, json=self.add(body, self.login or BOT_LOGIN))
        if path.startswith("/repos/acme/shop/issues/comments/") and method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[1])
            comment = next(c for c in self.comments if c["id"] == comment_id)
            comment["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- Checkout + event ---------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path) -> GitRepo:
    """The workflow's checkout: main plus the PR branch, full history, at the PR head."""
    repo = GitRepo(tmp_path / "checkout")
    _make_feature(repo, with_changes_md=False)
    return repo


def write_event(checkout: GitRepo, tmp_path: Path, *, fork: bool = False, **pr) -> Path:
    event = json.loads(EVENT_TEMPLATE.read_text())
    data = event["pull_request"]
    data["body"] = CHANGES_MD
    data["base"]["sha"] = checkout.git("rev-parse", "main")
    data["head"]["sha"] = checkout.git("rev-parse", "feature")
    if fork:
        data["head"]["repo"] = {"full_name": "someone/shop", "fork": True}
        data["head"]["label"] = "someone:feature"
    data.update(pr)
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return path


def run_action(checkout, event, gh=None, *, env=None, **kwargs):
    out = io.StringIO()
    options = {"dry_run": False, "fail_on": None, "budget": None, "no_llm": True} | kwargs
    code = action.run(
        event=event,
        repo=checkout.path,
        out=out,
        env={"GITHUB_TOKEN": "run-token"} | (env or {}),
        transport=gh.transport if gh else None,
        **options,
    )
    return code, out.getvalue()


# --- Event parsing ----------------------------------------------------------------------


def test_read_event(checkout, tmp_path):
    pr = read_event(write_event(checkout, tmp_path))
    assert (pr.repo, pr.number, pr.base_ref) == ("acme/shop", 7, "main")
    assert pr.head_sha == checkout.git("rev-parse", "feature")
    assert not pr.from_fork
    assert pr.description.startswith("Fix pagination off-by-one")


def test_read_event_fork(checkout, tmp_path):
    assert read_event(write_event(checkout, tmp_path, fork=True)).from_fork


def test_read_event_errors(tmp_path):
    with pytest.raises(ActionError, match="GITHUB_EVENT_PATH is not set") as exc:
        read_event(None)
    assert "--dry-run" in exc.value.hint
    with pytest.raises(ActionError, match="not found"):
        read_event(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ActionError, match="Could not read"):
        read_event(bad)
    push = tmp_path / "push.json"
    push.write_text(json.dumps({"ref": "refs/heads/main", "commits": []}))
    with pytest.raises(ActionError, match="not for a pull request") as exc:
        read_event(push)
    assert "on: pull_request" in exc.value.hint
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"pull_request": {"number": 7}}))
    with pytest.raises(ActionError, match="missing expected fields"):
        read_event(broken)


# --- Helpers ----------------------------------------------------------------------------


def test_fit_leaves_short_text_alone():
    assert fit("hello\n" + MARKER, 1000) == "hello\n" + MARKER


def test_fit_cuts_at_a_line_keeps_marker_and_closes_details():
    text = "### Plan\n<details><summary>Skim</summary>\n" + "- a line\n" * 5000 + "</details>\n"
    text += MARKER
    cut = fit(text, 2000)
    assert len(cut) <= 2000
    assert cut.rstrip().endswith(MARKER)
    assert cut.count("<details>") == cut.count("</details>")
    assert "cut to fit" in cut
    assert "- a line\n" in cut and "- a li\n" not in cut


def test_find_own_comment_needs_marker_and_author():
    comments = [
        {"id": 1, "body": f"copied {MARKER}", "user": {"login": "mallory"}},
        {"id": 2, "body": "no marker", "user": {"login": BOT_LOGIN}},
        {"id": 3, "body": f"plan\n{MARKER}", "user": {"login": BOT_LOGIN}},
        "junk",
    ]
    assert find_own_comment(comments, BOT_LOGIN)["id"] == 3
    assert find_own_comment(comments[:2], BOT_LOGIN) is None


def test_annotate_escapes_newlines():
    out = io.StringIO()
    annotate("error", "50% bad\nsecond line\r", out)
    assert out.getvalue() == "::error title=Review Map::50%25 bad%0Asecond line%0D\n"


def test_stop_commands_wraps_output():
    out = io.StringIO()
    with stop_commands(out, enabled=True):
        print("::add-mask::sneaky", file=out)
    lines = out.getvalue().splitlines()
    token = lines[0].removeprefix("::stop-commands::")
    assert len(token) == 32
    assert lines == [f"::stop-commands::{token}", "::add-mask::sneaky", f"::{token}::"]
    quiet = io.StringIO()
    with stop_commands(quiet, enabled=False):
        print("x", file=quiet)
    assert quiet.getvalue() == "x\n"


# --- Upsert: create, update, never duplicate --------------------------------------


def test_creates_the_comment(checkout, tmp_path):
    gh = FakeGitHub()
    summary = tmp_path / "summary.md"
    code, out = run_action(
        checkout, write_event(checkout, tmp_path), gh, env={"GITHUB_STEP_SUMMARY": str(summary)}
    )
    assert code == EXIT_OK, out
    assert gh.writes() == ["POST"]
    body = gh.comments[0]["body"]
    assert body.startswith("### Review Map: reading plan")
    assert MARKER in body
    assert "can\\_edit" in body or "can_edit" in body
    assert "Comment created: https://github.com/acme/shop/pull/7#issuecomment-" in out
    assert summary.read_text().startswith("### Review Map: reading plan")
    auth = [r.headers["authorization"] for r in gh.requests]
    assert set(auth) == {"Bearer run-token"}


def test_updates_its_own_comment_in_place(checkout, tmp_path):
    gh = FakeGitHub()
    old = gh.add(f"old plan\n{MARKER}", BOT_LOGIN)
    code, out = run_action(checkout, write_event(checkout, tmp_path), gh)
    assert code == EXIT_OK, out
    assert gh.writes() == ["PATCH"]
    assert len(gh.comments) == 1
    assert gh.comments[0]["id"] == old["id"]
    assert gh.comments[0]["body"].startswith("### Review Map")
    assert "Comment updated" in out


def test_second_run_with_same_plan_changes_nothing(checkout, tmp_path):
    gh = FakeGitHub()
    event = write_event(checkout, tmp_path)
    run_action(checkout, event, gh)
    code, out = run_action(checkout, event, gh)
    assert code == EXIT_OK
    assert gh.writes() == ["POST"]  # no PATCH the second time: no notification noise
    assert "Comment unchanged" in out


def test_ignores_a_marker_pasted_by_someone_else(checkout, tmp_path):
    gh = FakeGitHub()
    theirs = gh.add(f"I copied it {MARKER}", "mallory")
    run_action(checkout, write_event(checkout, tmp_path), gh)
    assert gh.writes() == ["POST"]
    assert theirs["body"] == f"I copied it {MARKER}"  # untouched
    assert len(gh.comments) == 2


def test_finds_its_comment_on_a_later_page(checkout, tmp_path):
    gh = FakeGitHub(page_size=2)
    for i in range(5):
        gh.add(f"chatter {i}", "someone")
    ours = gh.add(f"old\n{MARKER}", BOT_LOGIN)
    run_action(checkout, write_event(checkout, tmp_path), gh)
    assert gh.writes() == ["PATCH"]
    assert ours["body"].startswith("### Review Map")
    pages = [
        r.url.params.get("page")
        for r in gh.requests
        if r.method == "GET" and "comments" in r.url.path
    ]
    assert pages == [None, "2", "3"]


def test_personal_token_matches_by_its_own_login(checkout, tmp_path):
    gh = FakeGitHub()
    gh.login = "eshan"
    bot_comment = gh.add(f"from the bot\n{MARKER}", BOT_LOGIN)
    mine = gh.add(f"from me\n{MARKER}", "eshan")
    run_action(checkout, write_event(checkout, tmp_path), gh)
    assert gh.writes() == ["PATCH"]
    assert mine["body"].startswith("### Review Map")
    assert bot_comment["body"].startswith("from the bot")


# --- Permissions and forks ----------------------------------------------------------


def test_missing_write_permission_is_a_clear_error(checkout, tmp_path):
    gh = FakeGitHub()
    gh.write_status = 403
    with pytest.raises(GitHubPermissionError) as exc:
        run_action(checkout, write_event(checkout, tmp_path), gh)
    assert "pull-requests: write" in exc.value.hint


def test_fork_pr_without_write_access_warns_and_uses_the_summary(checkout, tmp_path):
    gh = FakeGitHub()
    gh.write_status = 403
    summary = tmp_path / "summary.md"
    code, out = run_action(
        checkout,
        write_event(checkout, tmp_path, fork=True),
        gh,
        env={"GITHUB_STEP_SUMMARY": str(summary)},
        no_llm=False,
    )
    assert code == EXIT_OK
    assert "::warning title=Review Map::Can't comment on a PR from a fork" in out
    assert "It is in the job summary." in out
    text = " ".join(summary.read_text().split())
    assert "This PR comes from a fork" in text


def test_fork_note_only_when_the_llm_was_missing(checkout, tmp_path):
    gh = FakeGitHub()
    run_action(checkout, write_event(checkout, tmp_path, fork=True), gh, no_llm=False)
    assert " ".join(FORK_NOTE.split()[:5]) in " ".join(gh.comments[0]["body"].split())
    gh2 = FakeGitHub()
    run_action(checkout, write_event(checkout, tmp_path), gh2, no_llm=False)  # not a fork
    assert "comes from a fork" not in gh2.comments[0]["body"]


def test_no_llm_key_says_heuristic_and_points_to_secrets(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gh = FakeGitHub()
    run_action(checkout, write_event(checkout, tmp_path), gh, no_llm=False)
    body = " ".join(gh.comments[0]["body"].split())
    assert "heuristic parser" in body
    assert "repository secret" in body
    assert ".env" not in body


def test_missing_token(checkout, tmp_path):
    with pytest.raises(ActionError, match="GITHUB_TOKEN is not set") as exc:
        run_action(checkout, write_event(checkout, tmp_path), env={"GITHUB_TOKEN": ""})
    assert "--dry-run" in exc.value.hint


def test_shallow_checkout_is_explained(checkout, tmp_path):
    event = write_event(checkout, tmp_path, base={"ref": "main", "sha": "1" * 40,
                                                   "repo": {"full_name": "acme/shop"}})  # fmt: skip
    with pytest.raises(ActionError, match="missing this PR's commits") as exc:
        run_action(checkout, event, FakeGitHub())
    assert "fetch-depth: 0" in exc.value.hint


# --- Gate, budget, dry run ----------------------------------------------------------


def test_gate_fails_after_commenting(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gh = FakeGitHub()
    code, out = run_action(
        checkout, write_event(checkout, tmp_path), gh, env={"GITHUB_ACTIONS": "true"},
        fail_on="medium",
    )  # fmt: skip
    assert code == EXIT_GATE_FAILED
    assert gh.writes() == ["POST"]  # the comment explains why the check is red
    assert "::error title=Review Map::Gate failed" in out


def test_budget_limits_sections(checkout, tmp_path):
    gh = FakeGitHub()
    _, out = run_action(checkout, write_event(checkout, tmp_path), gh, budget=1)
    assert "Review Map: 1 of" in out


def test_dry_run_prints_and_never_calls_github(checkout, tmp_path):
    def no_api(request):
        raise AssertionError("dry run must not call GitHub")

    out = io.StringIO()
    code = action.run(
        event=write_event(checkout, tmp_path),
        repo=checkout.path,
        dry_run=True,
        fail_on=None,
        budget=None,
        no_llm=True,
        out=out,
        env={},  # no token needed
        transport=httpx.MockTransport(no_api),
    )
    assert code == EXIT_OK
    assert "### Review Map: reading plan" in out.getvalue()
    assert MARKER in out.getvalue()


def test_dry_run_in_actions_blocks_workflow_commands_from_the_pr(checkout, tmp_path):
    hostile = "Fix it\n\n::add-mask::x\n::set-env name=PATH::/evil\n::error::fake"
    event = write_event(checkout, tmp_path, title="::warning::title", body=hostile)
    out = io.StringIO()
    action.run(
        event=event, repo=checkout.path, dry_run=True, fail_on=None, budget=None,
        no_llm=True, out=out, env={"GITHUB_ACTIONS": "true"},
    )  # fmt: skip
    lines = out.getvalue().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[start].removeprefix("::stop-commands::")
    end = lines.index(f"::{token}::")
    # Every line that could be read as a command sits inside the stop-commands block.
    for i, line in enumerate(lines):
        if line.lstrip().startswith("::") and not (start <= i <= end):
            pytest.fail(f"unprotected workflow command: {line!r}")


# --- Client additions --------------------------------------------------------------


def test_pagination_refuses_links_to_other_hosts():
    def handler(request):
        return httpx.Response(
            200, json=[], headers={"link": '<https://evil.example/x>; rel="next"'}
        )

    client = GitHubClient("t", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError, match="another host"):
        client.get_all("/repos/a/b/issues/1/comments", what="comments")


def test_read_403_is_not_a_permission_error():
    client = GitHubClient(
        "t", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "no"}))
    )
    with pytest.raises(GitHubError) as exc:
        client.get_json("/x", what="x")
    assert not isinstance(exc.value, GitHubPermissionError)
    assert isinstance(exc.value, ReviewMapError)


def test_login_without_token_is_none():
    assert GitHubClient(None).login() is None


# --- CLI --------------------------------------------------------------------------------


@pytest.fixture
def cli_github(monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(
        action, "GitHubClient", lambda token, **kw: GitHubClient(token, transport=gh.transport)
    )
    return gh


def invoke(*args, env=None):
    return runner.invoke(app, ["action", "--no-llm", *args], env={"COLUMNS": "200"} | (env or {}))


def test_cli_action_posts(checkout, tmp_path, cli_github):
    event = write_event(checkout, tmp_path)
    result = invoke(
        "--repo", str(checkout.path), env={"GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "t"}
    )
    assert result.exit_code == 0, result.output
    assert cli_github.writes() == ["POST"]


def test_cli_action_reads_inputs_from_env(checkout, tmp_path, cli_github):
    event = write_event(checkout, tmp_path)
    env = {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_TOKEN": "t",
        "REVIEW_MAP_FAIL_ON": "medium",
        "REVIEW_MAP_BUDGET": "1",
    }
    result = invoke("--repo", str(checkout.path), env=env)
    assert result.exit_code == EXIT_GATE_FAILED, result.output
    assert "Review Map: 1 of" in result.output


def test_cli_action_empty_inputs_mean_defaults(checkout, tmp_path, cli_github):
    # action.yml passes unset inputs as empty strings.
    env = {
        "GITHUB_EVENT_PATH": str(write_event(checkout, tmp_path)),
        "GITHUB_TOKEN": "t",
        "REVIEW_MAP_FAIL_ON": "",
        "REVIEW_MAP_BUDGET": "",
    }
    result = invoke("--repo", str(checkout.path), env=env)
    assert result.exit_code == 0, result.output


def test_cli_action_permission_error_exit_code_and_annotation(checkout, tmp_path, cli_github):
    cli_github.write_status = 403
    env = {
        "GITHUB_EVENT_PATH": str(write_event(checkout, tmp_path)),
        "GITHUB_TOKEN": "t",
        "GITHUB_ACTIONS": "true",
    }
    result = invoke("--repo", str(checkout.path), env=env)
    assert result.exit_code == EXIT_USER_ERROR
    out = " ".join(result.output.split())
    assert "pull-requests: write" in out
    assert "::error title=Review Map::GitHub refused access" in result.output
    assert "Traceback" not in result.output


def test_cli_action_dry_run_with_event_flag(checkout, tmp_path):
    result = invoke("--repo", str(checkout.path), "--event", str(write_event(checkout, tmp_path)),
                    "--dry-run")  # fmt: skip
    assert result.exit_code == 0, result.output
    assert MARKER in result.output


def test_cli_action_outside_actions():
    result = invoke("--dry-run")
    assert result.exit_code == EXIT_USER_ERROR
    assert "GITHUB_EVENT_PATH is not set" in result.output


def test_cli_action_help():
    result = runner.invoke(app, ["action", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for flag in ("--event", "--dry-run", "--fail-on", "--budget"):
        assert flag in result.output


def test_env_file_is_not_loaded_inside_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_env_file", lambda: calls.append(1))
    runner.invoke(app, ["version"], env={"GITHUB_ACTIONS": "true"})
    assert calls == []
    runner.invoke(app, ["version"], env={"GITHUB_ACTIONS": ""})
    assert calls == [1]


# --- action.yml and the example workflow ----------------------------------------


def test_action_yml_is_safe_and_complete():
    spec = yaml.safe_load((ROOT / "action.yml").read_text())
    assert spec["runs"]["using"] == "composite"
    assert {"fail-on", "budget", "provider", "model", "github-token"} <= set(spec["inputs"])
    for step in spec["runs"]["steps"]:
        # Inputs must reach scripts through env vars, never `${{ }}` inside `run:`.
        assert "${{" not in step.get("run", ""), step
    env = spec["runs"]["steps"][-1]["env"]
    assert env["REVIEW_MAP_FAIL_ON"] == "${{ inputs.fail-on }}"
    assert env["GITHUB_TOKEN"] == "${{ inputs.github-token }}"


def test_action_yml_meets_marketplace_rules():
    spec = yaml.safe_load((ROOT / "action.yml").read_text())
    assert len(spec["description"]) < 125
    assert spec["name"].lower() != "review map"  # taken by the "review-map" organization
    assert spec["branding"] == {"icon": "map", "color": "blue"}


def test_example_workflow():
    flow = yaml.safe_load((ROOT / "examples" / "workflow.yml").read_text())
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    assert "pull_request" in triggers and "pull_request_target" not in triggers
    assert set(triggers["pull_request"]["types"]) >= {"opened", "synchronize", "edited"}
    assert flow["permissions"] == {"contents": "read", "pull-requests": "write"}
    steps = flow["jobs"]["reading-plan"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert "secrets.GROQ_API_KEY" in steps[1]["env"]["GROQ_API_KEY"]
    assert "concurrency" in flow
