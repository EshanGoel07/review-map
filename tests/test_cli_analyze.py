"""End-to-end tests for `review-map analyze` and the demo repo (no LLM, no network)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from review_map.claims import parser
from review_map.cli import app
from review_map.errors import EXIT_GATE_FAILED, EXIT_USER_ERROR
from tests.test_claims import fake_client
from tests.test_cli_verify import _make_feature

runner = CliRunner()
DEMO_BUILD = Path(__file__).parents[1] / "examples" / "demo_repo" / "build.py"
WIDE = {"COLUMNS": "250"}  # so rich doesn't wrap table cells


def analyze(repo: Path, *args: str, **kwargs):
    return runner.invoke(
        app,
        ["analyze", "--repo", str(repo), "--base", "main", "--no-llm", *args],
        env=WIDE,
        **kwargs,
    )


def test_analyze_text(git_repo):
    _make_feature(git_repo)
    result = analyze(git_repo.path, "--rules-only")  # the Phase 6 layout and counts
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Read carefully (2 of 5 sections" in out
    assert out.index("can_edit") < out.index("Unmentioned changes")
    assert "Unmentioned changes (2)" in out
    assert "Claimed but not changed (1)" in out
    assert "Safe to skim: 3 sections" in out  # incl. changes.md itself
    assert "1 file skipped" in out
    assert "Claims from changes.md (heuristic parser)" in out
    assert "halved" in out  # heuristic claims


def test_analyze_ranks_hidden_changes_first(git_repo):
    _make_feature(git_repo)
    data = json.loads(analyze(git_repo.path, "--format", "json").stdout)
    ranked = [(i["name"], i["rank"]) for i in data["items"] if i["rank"]]
    # With the model, out-of-scope violations are must-read and come first, and the
    # rest of the budget is filled by risk per line (D26).
    assert ranked[:2] == [("refund", 1), ("can_edit", 2)]
    names = [i["path"] for i in data["items"] if i["rank"]]
    assert "changes.md" not in names  # not model-scored: keeps the rules gate
    assert data["risk_scorer"] == {"name": "shipped default model", "calibrated": False}
    assert data["claims"]["description"] == "changes.md"
    assert data["change"]["base"] == "main"
    assert len(data["change"]["head_sha"]) == 40


def test_analyze_rules_only_keeps_the_priority_order(git_repo):
    _make_feature(git_repo)
    data = json.loads(analyze(git_repo.path, "--format", "json", "--rules-only").stdout)
    ranked = [(i["name"], i["rank"]) for i in data["items"] if i["rank"]]
    assert ranked == [("can_edit", 1), ("refund", 2)]
    assert all(i["model_level"] is None for i in data["items"])
    assert data["risk_scorer"]["name"] == "rules"


def test_analyze_markdown(git_repo):
    _make_feature(git_repo)
    result = analyze(git_repo.path, "--format", "md")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("### Review Map: reading plan")
    assert result.stdout.rstrip().endswith("<!-- review-map -->")


def test_analyze_html_defaults_to_report_html(git_repo, tmp_path, monkeypatch):
    _make_feature(git_repo)
    monkeypatch.chdir(tmp_path)
    result = analyze(git_repo.path, "--format", "html")
    assert result.exit_code == 0, result.output
    page = (tmp_path / "report.html").read_text()
    assert "can_edit" in page and "flag-high" in page
    assert "Wrote report.html" in result.output


@pytest.mark.parametrize("fmt", ["md", "json", "html", "text"])
def test_analyze_out_file(git_repo, tmp_path, fmt):
    _make_feature(git_repo)
    target = tmp_path / f"plan.{fmt}"
    result = analyze(git_repo.path, "--format", fmt, "--out", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text()
    assert "can_edit" in content
    assert "\x1b[" not in content  # no terminal colour codes in files


def test_analyze_out_to_missing_folder_is_a_clear_error(git_repo, tmp_path):
    _make_feature(git_repo)
    result = analyze(git_repo.path, "--format", "md", "--out", str(tmp_path / "no" / "x.md"))
    assert result.exit_code == EXIT_USER_ERROR
    assert "Could not write" in result.output
    assert "Traceback" not in result.output


def test_analyze_without_description(git_repo):
    _make_feature(git_repo, with_changes_md=False)
    result = analyze(git_repo.path, "--format", "json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["claims"] is None
    assert any("scope check was skipped" in n for n in data["notes"])


def test_analyze_with_llm_claims(git_repo, monkeypatch):
    _make_feature(git_repo)
    reply = json.dumps(
        {
            "files": ["app/api/views.py"],
            "functions": ["paginate"],
            "out_of_scope": ["app/payments/"],
        }
    )
    client, _ = fake_client(reply)
    monkeypatch.setattr(parser, "build_client", lambda *a, **k: client)
    result = runner.invoke(
        app, ["analyze", "--repo", str(git_repo.path), "--base", "main", "--format", "json"]
    )
    data = json.loads(result.stdout)
    assert data["claims"]["model"] == "groq/openai/gpt-oss-120b"
    refund = next(i for i in data["items"] if i["name"] == "refund")
    assert refund["scope"]["out_of_scope"] == ["app/payments/"]
    assert refund["boost"] == 30  # full boost: LLM claims aren't low confidence


@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        ((), 0),
        (("--fail-on", "high"), EXIT_GATE_FAILED),
        (("--fail-on", "medium"), EXIT_GATE_FAILED),
    ],
)
def test_analyze_gate(git_repo, args, exit_code):
    _make_feature(git_repo)
    result = analyze(git_repo.path, *args)
    assert result.exit_code == exit_code, result.output
    if exit_code:
        assert "Gate failed" in result.output


def test_analyze_gate_passes_for_harmless_change(git_repo):
    git_repo.write("app.py", "def greet():\n    return 'hi'\n")
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("app.py", "def greet():\n    return 'hello'\n")
    git_repo.write("tests/test_app.py", "def test_greet():\n    pass\n")
    git_repo.commit("feature")
    result = analyze(git_repo.path, "--fail-on", "medium")
    assert result.exit_code == 0, result.output
    rules = analyze(git_repo.path, "--fail-on", "medium", "--rules-only")
    assert rules.exit_code == 0, rules.output
    assert "Nothing stands out" in rules.output


def test_analyze_gate_still_applies_with_file_output(git_repo, tmp_path):
    _make_feature(git_repo)
    result = analyze(git_repo.path, "--format", "json", "--out", str(tmp_path / "p.json"),
                     "--fail-on", "high")  # fmt: skip
    assert result.exit_code == EXIT_GATE_FAILED
    assert (tmp_path / "p.json").exists()


def test_analyze_bad_format():
    result = runner.invoke(app, ["analyze", "--format", "pdf"])
    assert result.exit_code == 2
    assert "pdf" in result.output


def test_analyze_help():
    result = runner.invoke(app, ["analyze", "--help"], env=WIDE)
    assert result.exit_code == 0
    for option in ("--format", "--out", "--fail-on", "--desc", "--no-llm"):
        assert option in result.output


# --- Demo repo ------------------------------------------------------------------


def build_demo(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEMO_BUILD), str(target)], capture_output=True, text=True
    )


def test_demo_repo_catches_the_hidden_change(tmp_path):
    demo = tmp_path / "demo"
    built = build_demo(demo)
    assert built.returncode == 0, built.stdout + built.stderr
    assert "Demo repo ready" in built.stdout

    data = json.loads(analyze(demo, "--format", "json").stdout)
    top = data["items"][0]
    assert (top["name"], top["rank"]) == ("can_delete", 1)
    assert top["unmentioned"] is True
    assert top["level"] == "high"
    # The claimed pagination work is not flagged as unmentioned.
    claimed = {i["name"]: i["unmentioned"] for i in data["items"]}
    assert claimed["NoteStore.list_notes"] is False and claimed["list_notes"] is False
    assert analyze(demo, "--fail-on", "high").exit_code == EXIT_GATE_FAILED


def test_demo_build_refuses_a_non_empty_folder(tmp_path):
    (tmp_path / "keep.txt").write_text("mine")
    result = build_demo(tmp_path)
    assert result.returncode == 2
    assert "not empty" in result.stdout
    assert (tmp_path / "keep.txt").read_text() == "mine"
