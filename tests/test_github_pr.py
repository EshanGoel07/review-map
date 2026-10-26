"""Tests for `review-map pr`: ref parsing, the GitHub client, the clone cache, the CLI.

GitHub's API is replaced by `httpx2.MockTransport`; the "GitHub remote" is a
local git repo with a `refs/pull/7/head` ref, so nothing touches the network.
"""

import base64
import json
import os
import time
from functools import partial
from pathlib import Path

import httpx2 as httpx
import pytest
from typer.testing import CliRunner

from review_map import cli
from review_map.cli import app
from review_map.diff import github_pr
from review_map.diff.github_pr import (
    PullRequest,
    clone_path,
    default_repo_cache_dir,
    ensure_pr_commits,
    fetch_pr,
    git_auth,
    load_pr_config,
    parse_pr_ref,
)
from review_map.errors import EXIT_GATE_FAILED, EXIT_USER_ERROR, GitError, ReviewMapError
from review_map.github.client import TOKEN_URL, GitHubClient, GitHubError, token_hint
from tests.conftest import GitRepo
from tests.test_cli_verify import CHANGES_MD, _make_feature

runner = CliRunner()
WIDE = {"COLUMNS": "250"}


# --- Parsing PR references --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "slug", "number"),
    [
        ("psf/requests#1234", "psf/requests", 1234),
        ("  psf/requests#1  ", "psf/requests", 1),
        ("https://github.com/psf/requests/pull/1234", "psf/requests", 1234),
        ("https://github.com/psf/requests/pull/1234/files", "psf/requests", 1234),
        ("http://www.github.com/pallets/flask/pull/5#issuecomment-1", "pallets/flask", 5),
        ("github.com/a-b/c.d_e/pull/9", "a-b/c.d_e", 9),
    ],
)
def test_parse_pr_ref(text, slug, number):
    ref = parse_pr_ref(text)
    assert (ref.slug, ref.number) == (slug, number)
    assert str(ref) == f"{slug}#{number}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "psf/requests",
        "psf/requests#",
        "psf/requests#abc",
        "psf/requests#0",
        "psf#12",
        "../etc#1",
        "psf/..#1",
        "psf/requests.git#1",
        "-x/y#1",
        "https://gitlab.com/psf/requests/pull/1",
        "https://github.com/psf/requests/issues/1",
    ],
)
def test_parse_pr_ref_rejects(text):
    with pytest.raises(ReviewMapError, match="pull request"):
        parse_pr_ref(text)


# --- Fake GitHub API -------------------------------------------------------------


def pr_json(**overrides) -> dict:
    data = {
        "number": 7,
        "title": "Fix pagination off-by-one",
        "body": CHANGES_MD,
        "state": "closed",
        "merged": True,
        "draft": False,
        "html_url": "https://github.com/acme/shop/pull/7",
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "acme/shop"}},
        "head": {
            "ref": "feature",
            "label": "someone:feature",
            "sha": "h" * 40,
            "repo": {"full_name": "someone/shop"},
        },
    }
    data.update(overrides)
    return data


def make_client(handler, token: str | None = None) -> GitHubClient:
    return GitHubClient(token, transport=httpx.MockTransport(handler))


def respond(status: int, body=None, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    return handler


REF = parse_pr_ref("acme/shop#7")


def test_fetch_pr_normal():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pr_json())

    pr = fetch_pr(make_client(handler), REF)
    assert seen[0].url.path == "/repos/acme/shop/pulls/7"
    assert seen[0].headers["accept"] == "application/vnd.github+json"
    assert "authorization" not in seen[0].headers
    assert seen[0].headers["user-agent"].startswith("review-map/")
    assert pr.repo == "acme/shop"
    assert (pr.base_ref, pr.base_sha, pr.head_sha) == ("main", "b" * 40, "h" * 40)
    assert pr.head_label == "someone:feature"
    assert pr.status == "merged"
    assert pr.from_fork
    assert pr.description.startswith("Fix pagination off-by-one\n\n# Fix pagination")


def test_token_is_sent_when_set():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pr_json())

    fetch_pr(make_client(handler, token="secret-token"), REF)
    assert seen[0].headers["authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("body", ["", None, "   \n "])
def test_fetch_pr_empty_description(body):
    pr = fetch_pr(make_client(respond(200, pr_json(body=body))), REF)
    assert pr.body.strip() == ""
    assert pr.description is None  # title alone is not a scope


def test_fetch_pr_same_repo_and_deleted_fork():
    head_same = {"ref": "f", "label": "acme:f", "sha": "h" * 40, "repo": {"full_name": "acme/shop"}}
    assert not fetch_pr(make_client(respond(200, pr_json(head=head_same))), REF).from_fork
    head_gone = {"ref": "f", "label": "ghost:f", "sha": "h" * 40, "repo": None}
    assert fetch_pr(make_client(respond(200, pr_json(head=head_gone))), REF).from_fork


def test_fetch_pr_status():
    def status(**kw):
        return fetch_pr(make_client(respond(200, pr_json(**kw))), REF).status

    assert status(state="open", merged=False) == "open"
    assert status(state="open", merged=False, draft=True) == "draft"
    assert status(state="closed", merged=False) == "closed"


def test_fetch_pr_missing_fields():
    with pytest.raises(GitHubError, match="missing expected fields"):
        fetch_pr(make_client(respond(200, {"number": 7})), REF)


def test_not_found_without_token_mentions_private_repos():
    with pytest.raises(GitHubError, match="Not found on GitHub: pull request acme/shop#7") as exc:
        fetch_pr(make_client(respond(404, {"message": "Not Found"})), REF)
    assert "private" in exc.value.hint
    assert "GITHUB_TOKEN" in exc.value.hint


def test_not_found_with_token():
    with pytest.raises(GitHubError, match="Not found") as exc:
        fetch_pr(make_client(respond(404), token="t"), REF)
    assert "token can read" in exc.value.hint


def test_bad_token():
    with pytest.raises(GitHubError, match="401") as exc:
        fetch_pr(make_client(respond(401, {"message": "Bad credentials"}), token="t"), REF)
    assert "GITHUB_TOKEN" in exc.value.hint


def test_rate_limit_without_token():
    reset = int(time.time()) + 600
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    handler = respond(403, {"message": "API rate limit exceeded"}, headers)
    with pytest.raises(GitHubError, match="rate limit reached. It resets at") as exc:
        fetch_pr(make_client(handler), REF)
    assert "60 requests an hour" in exc.value.hint
    assert "5,000 requests an hour" in exc.value.hint
    assert TOKEN_URL in exc.value.hint


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (429, {}, "Too many requests"),
        (403, {"retry-after": "60"}, "You have exceeded a secondary rate limit"),
        (403, {}, "API rate limit exceeded for user"),
    ],
)
def test_rate_limit_variants(status, headers, message):
    with pytest.raises(GitHubError, match="rate limit reached") as exc:
        fetch_pr(make_client(respond(status, {"message": message}, headers), token="t"), REF)
    assert "Wait" in exc.value.hint


def test_forbidden_is_not_a_rate_limit():
    handler = respond(403, {"message": "Resource not accessible"}, {"x-ratelimit-remaining": "40"})
    with pytest.raises(GitHubError, match="refused access.*Resource not accessible"):
        fetch_pr(make_client(handler, token="t"), REF)


def test_server_error():
    with pytest.raises(GitHubError, match="server error \\(502\\)"):
        fetch_pr(make_client(respond(502)), REF)


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (httpx.ConnectError("nope"), "Could not reach GitHub"),
        (httpx.ReadTimeout("slow"), "did not answer in time"),
    ],
)
def test_network_errors(exc, message):
    def handler(request):
        raise exc

    with pytest.raises(GitHubError, match=message):
        fetch_pr(make_client(handler), REF)


def test_unreadable_json():
    def handler(request):
        return httpx.Response(200, content=b"<html>oops</html>")

    with pytest.raises(GitHubError, match="unreadable"):
        fetch_pr(make_client(handler), REF)


def test_renamed_repo_redirect_is_followed():
    def handler(request):
        if request.url.path == "/repos/acme/shop/pulls/7":
            return httpx.Response(301, headers={"location": "/repositories/42/pulls/7"})
        return httpx.Response(200, json=pr_json())

    assert fetch_pr(make_client(handler), REF).number == 7


# --- Clone cache --------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path) -> GitRepo:
    """A stand-in for GitHub: main + a feature branch published as refs/pull/7/head."""
    repo = GitRepo(tmp_path / "remote")
    _make_feature(repo, with_changes_md=False)
    repo.git("update-ref", "refs/pull/7/head", "feature")
    repo.git("checkout", "-q", "main")
    return repo


def pr_for(remote: GitRepo, **overrides) -> PullRequest:
    values = {
        "ref": "acme/shop#7",
        "repo": "acme/shop",
        "number": 7,
        "title": "Fix pagination off-by-one",
        "body": CHANGES_MD,
        "state": "open",
        "merged": False,
        "draft": False,
        "html_url": "https://github.com/acme/shop/pull/7",
        "base_ref": "main",
        "base_sha": remote.git("rev-parse", "main"),
        "head_label": "someone:feature",
        "head_sha": remote.git("rev-parse", "refs/pull/7/head"),
        "from_fork": True,
    }
    return PullRequest(**(values | overrides))


def test_default_repo_cache_dir():
    assert default_repo_cache_dir({"REVIEW_MAP_REPO_CACHE_DIR": "/x/y"}) == Path("/x/y")
    assert default_repo_cache_dir({"XDG_CACHE_HOME": "/c"}) == Path("/c/review-map/repos")
    assert default_repo_cache_dir({}) == Path.home() / ".cache" / "review-map" / "repos"


def test_clone_path_is_one_folder_per_repo(tmp_path):
    assert clone_path(tmp_path, "Acme/Shop") == tmp_path / "acme__shop.git"


def test_ensure_pr_commits_fetches_then_uses_cache(remote, tmp_path):
    pr = pr_for(remote)
    cache = tmp_path / "cache"
    clone, fetched = ensure_pr_commits(pr, cache, remote_url=str(remote.path))
    assert fetched
    assert clone == cache / "acme__shop.git"
    assert github_pr._has_commit(clone, pr.base_sha)
    assert github_pr._has_commit(clone, pr.head_sha)

    _, fetched_again = ensure_pr_commits(pr, cache, remote_url="/nowhere")  # no fetch needed
    assert not fetched_again


def test_ensure_pr_commits_fetches_only_new_commits(remote, tmp_path):
    cache = tmp_path / "cache"
    ensure_pr_commits(pr_for(remote), cache, remote_url=str(remote.path))
    remote.git("checkout", "-q", "feature")
    remote.write("app/api/views.py", "def paginate(items, page):\n    return items\n")
    new_head = remote.commit("follow-up")
    remote.git("update-ref", "refs/pull/7/head", new_head)
    clone, fetched = ensure_pr_commits(
        pr_for(remote, head_sha=new_head), cache, remote_url=str(remote.path)
    )
    assert fetched
    assert github_pr._has_commit(clone, new_head)


def test_ensure_pr_commits_missing_commit(remote, tmp_path):
    pr = pr_for(remote, head_sha="0" * 40)
    with pytest.raises(GitError):
        ensure_pr_commits(pr, tmp_path / "cache", remote_url=str(remote.path))


def test_ensure_pr_commits_unreachable_remote(remote, tmp_path):
    with pytest.raises(GitError, match="git fetch failed|could not read"):
        ensure_pr_commits(pr_for(remote), tmp_path / "cache", remote_url=str(tmp_path / "gone"))


def test_ensure_pr_commits_broken_cache(remote, tmp_path):
    cache = tmp_path / "cache"
    (cache / "acme__shop.git").mkdir(parents=True)
    (cache / "acme__shop.git" / "junk").write_text("not a repo")
    with pytest.raises(GitError, match="broken") as exc:
        ensure_pr_commits(pr_for(remote), cache, remote_url=str(remote.path))
    assert "Delete that folder" in exc.value.hint


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("fatal: unable to access '...': Could not resolve host: github.com", "Could not reach"),
        ("remote: Repository not found.\nfatal: repository '...' not found", "could not read"),
        ("fatal: could not read Username for 'https://github.com'", "could not read"),
        ("error: Server does not allow request for unadvertised object; not our ref", "no longer"),
        ("fatal: something odd\n", "git fetch failed: fatal: something odd"),
    ],
)
def test_fetch_error_messages(remote, stderr, message):
    assert message in github_pr._fetch_error(stderr, pr_for(remote)).message


def test_private_repo_git_error_has_token_hint(remote):
    error = github_pr._fetch_error("remote: Repository not found.", pr_for(remote))
    assert error.hint == token_hint(private=True)
    assert TOKEN_URL in error.hint


def test_token_hints_are_one_line_and_point_to_env_example():
    for hint in (token_hint(), token_hint(private=True)):
        assert "\n" not in hint
        assert "GITHUB_TOKEN" in hint and ".env.example" in hint and TOKEN_URL in hint


def test_git_auth_sends_token_via_env_only(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")
    with git_auth("tok"):
        assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
        assert os.environ["GIT_CONFIG_COUNT"] == "2"
        assert os.environ["GIT_CONFIG_KEY_0"] == "user.name"  # existing entries untouched
        assert os.environ["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
        header = os.environ["GIT_CONFIG_VALUE_1"]
        assert base64.b64decode(header.split()[-1]).decode() == "x-access-token:tok"
    assert os.environ["GIT_CONFIG_COUNT"] == "1"
    assert "GIT_CONFIG_KEY_1" not in os.environ
    assert "GIT_TERMINAL_PROMPT" not in os.environ


def test_git_auth_without_token_only_disables_prompts():
    with git_auth(None):
        assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_KEY_0" not in os.environ


# --- Config comes from the base commit, never the head ---------------------------


def test_pr_config_is_read_from_base(remote, tmp_path):
    remote.write(".reviewmap.yml", "ignore:\n  - 'docs/**'\n")
    base = remote.commit("config")
    remote.git("checkout", "-q", "feature")
    remote.git("merge", "-q", "main", "-m", "merge main")
    remote.write(".reviewmap.yml", "ignore:\n  - '**'\n")  # the PR tries to hide everything
    head = remote.commit("sneaky config")
    remote.git("update-ref", "refs/pull/7/head", head)
    pr = pr_for(remote, base_sha=base, head_sha=head)
    clone, _ = ensure_pr_commits(pr, tmp_path / "cache", remote_url=str(remote.path))

    loaded = load_pr_config(clone, pr.base_sha, None)
    assert loaded.config.ignore == ["docs/**"]
    assert loaded.source == Path(".reviewmap.yml")


def test_pr_config_defaults_and_explicit(remote, tmp_path):
    pr = pr_for(remote)
    clone, _ = ensure_pr_commits(pr, tmp_path / "cache", remote_url=str(remote.path))
    assert load_pr_config(clone, pr.base_sha, None).source is None
    explicit = tmp_path / "mine.yml"
    explicit.write_text("ignore:\n  - 'x/**'\n")
    assert load_pr_config(clone, pr.base_sha, explicit).config.ignore == ["x/**"]


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def fake_github(remote, monkeypatch):
    """Point `review-map pr` at the fake API and the local remote. Returns a setter."""
    state = {"handler": None}

    def set_pr(**overrides):
        body = pr_json(**overrides)
        body["base"]["sha"] = remote.git("rev-parse", "main")
        body["head"]["sha"] = remote.git("rev-parse", "refs/pull/7/head")
        state["handler"] = respond(200, body)

    def client(token=None):
        return make_client(lambda request: state["handler"](request), token)

    monkeypatch.setattr(cli, "GitHubClient", client)
    monkeypatch.setattr(
        cli, "ensure_pr_commits", partial(ensure_pr_commits, remote_url=str(remote.path))
    )
    set_pr()
    return state, set_pr


def run_pr(*args: str):
    return runner.invoke(app, ["pr", "acme/shop#7", "--no-llm", *args], env=WIDE)


def test_pr_text(fake_github):
    result = run_pr()
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Review Map acme/shop#7 (merged · from a fork): Fix pagination off-by-one" in out
    assert "main...someone:feature" in out
    assert "commits fetched" in out
    assert "config: defaults" in out
    assert "Claims from PR description (heuristic parser)" in out
    assert "Read carefully" in out
    assert "Unmentioned changes" in out
    assert out.index("can_edit") < out.index("Unmentioned changes")


def test_pr_second_run_uses_cached_commits(fake_github):
    assert run_pr().exit_code == 0
    result = run_pr()
    assert result.exit_code == 0, result.output
    assert "commits cached" in result.output


def test_pr_json_matches_the_local_analysis(fake_github, remote):
    data = json.loads(run_pr("--format", "json").stdout)
    ranked = [i["name"] for i in data["items"] if i["rank"]]
    assert ranked[:2] == ["refund", "can_edit"]  # out of scope first (D26)
    assert data["risk_scorer"]["name"] == "shipped default model"
    assert data["change"]["base"] == "main"
    assert data["change"]["head"] == "someone:feature"
    assert data["change"]["head_sha"] == remote.git("rev-parse", "refs/pull/7/head")
    assert data["change"]["base_sha"] == remote.git("merge-base", "main", "feature")
    assert data["claims"]["description"] == "PR description"


def test_pr_markdown_and_html(fake_github, tmp_path):
    md = run_pr("--format", "md")
    assert md.exit_code == 0, md.output
    assert md.stdout.startswith("### Review Map: reading plan")
    out = tmp_path / "pr.html"
    html = run_pr("--format", "html", "--out", str(out))
    assert html.exit_code == 0, html.output
    assert "can_edit" in out.read_text()


def test_pr_empty_description(fake_github):
    _, set_pr = fake_github
    set_pr(body="")
    result = run_pr()
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "This PR has no description, so the scope check was skipped" in out
    assert "Claims from" not in out
    assert "Unmentioned changes" not in out


def test_pr_not_found(fake_github):
    state, _ = fake_github
    state["handler"] = respond(404, {"message": "Not Found"})
    result = run_pr()
    assert result.exit_code == EXIT_USER_ERROR
    assert "Not found on GitHub: pull request acme/shop#7" in result.output
    assert "Traceback" not in result.output


def test_pr_private_repo_without_token(fake_github):
    # GitHub answers 404, not 403, for private repos you can't see.
    state, _ = fake_github
    state["handler"] = respond(404, {"message": "Not Found"})
    out = " ".join(run_pr().output.split())
    assert "If the repo is private, set GITHUB_TOKEN in .env" in out
    assert TOKEN_URL in out


def test_pr_rate_limited(fake_github):
    state, _ = fake_github
    state["handler"] = respond(
        403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"}
    )
    result = run_pr()
    assert result.exit_code == EXIT_USER_ERROR
    assert "GitHub rate limit reached" in result.output


def test_pr_uses_token_from_env(fake_github, monkeypatch):
    seen = []
    state, _ = fake_github
    inner = state["handler"]
    state["handler"] = lambda request: seen.append(request) or inner(request)
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    assert run_pr().exit_code == 0
    assert seen[0].headers["authorization"] == "Bearer abc"


SECRET = "ghp_SuperSecretTokenValue123"


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (200, None, {}),
        (401, {"message": "Bad credentials"}, {}),
        (404, {"message": "Not Found"}, {}),
        (403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"}),
        (403, {"message": "Resource not accessible"}, {}),
    ],
)
def test_pr_never_echoes_the_token(fake_github, monkeypatch, status, body, headers):
    state, set_pr = fake_github
    if status != 200:
        state["handler"] = respond(status, body, headers)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    for fmt in ("text", "json"):
        result = run_pr("--format", fmt)
        assert SECRET not in result.output
        assert SECRET not in (result.stderr or "")


def test_pr_never_echoes_the_token_on_git_errors(fake_github, monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "ensure_pr_commits", partial(ensure_pr_commits, remote_url=str(tmp_path / "gone"))
    )
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    result = run_pr()
    assert result.exit_code == EXIT_USER_ERROR
    assert SECRET not in result.output


def test_pr_bad_ref():
    result = runner.invoke(app, ["pr", "not-a-pr", "--no-llm"], env=WIDE)
    assert result.exit_code == EXIT_USER_ERROR
    assert "Not a pull request reference" in result.output


def test_pr_gate(fake_github):
    result = run_pr("--fail-on", "high")
    assert result.exit_code == EXIT_GATE_FAILED, result.output
    assert "Gate failed" in result.output


def test_pr_cache_dir_option(fake_github, tmp_path):
    cache = tmp_path / "my-cache"
    assert run_pr("--cache-dir", str(cache)).exit_code == 0
    assert (cache / "acme__shop.git").is_dir()


def test_pr_help():
    result = runner.invoke(app, ["pr", "--help"], env=WIDE)
    assert result.exit_code == 0
    assert "owner/repo#123" in result.output
    assert "--cache-dir" in result.output


# --- Skim list labels ------------------------------------------------------------------


def test_skim_list_names_the_file_for_module_level_code(git_repo):
    _make_feature(git_repo)
    result = runner.invoke(
        app,
        ["analyze", "--repo", str(git_repo.path), "--base", "main", "--no-llm", "--rules-only"],
        env=WIDE,
    )
    assert result.exit_code == 0, result.output
    skim_line = next(line for line in result.output.splitlines() if "Safe to skim" in line)
    assert "changes.md" in skim_line
    assert "<module>" not in skim_line
