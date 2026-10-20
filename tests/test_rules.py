"""Tests for the rule-based risk scorer: every rule fires on a positive and not on a negative."""

import pytest
from typer.testing import CliRunner

from review_map.cli import app
from review_map.config import Config
from review_map.diff.parser import detect_language
from review_map.models import ChangedFile, ChangedUnit, FileStatus, RiskLevel, UnitKind
from review_map.risk.rules import (
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    WEIGHTS,
    ScoringContext,
    is_test_path,
    level_for,
    score_unit,
    score_units,
    words,
)


def make_unit(
    path: str = "src/app.py",
    name: str = "handle",
    added: list[str] | None = None,
    removed: list[str] | None = None,
    old_path: str | None = None,
) -> ChangedUnit:
    added = added or []
    removed = removed or []
    return ChangedUnit(
        path=path,
        old_path=old_path,
        language=detect_language(path),
        name=name,
        kind=UnitKind.FUNCTION,
        start_line=1,
        end_line=10,
        added_lines=list(range(1, len(added) + 1)),
        removed_lines=list(range(1, len(removed) + 1)),
        added_text=added,
        removed_text=removed,
    )


def rules_for(unit: ChangedUnit, config: Config | None = None, tests_changed: bool = True):
    ctx = ScoringContext(config=config or Config(), tests_changed=tests_changed)
    return {r.rule for r in score_unit(unit, ctx).reasons}


# --------------------------------------------------------------------------- words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("isAdmin", ["is", "admin"]),
        ("reset_password", ["reset", "password"]),
        ("HTTPServer", ["http", "server"]),
        ("hashlib.sha256(x)", ["hashlib", "sha256", "x"]),
        ("src/auth/views.py", ["src", "auth", "views", "py"]),
    ],
)
def test_words(text, expected):
    assert words(text) == expected


# --------------------------------------------------------------------------- rules
# (rule, positive unit, negative unit)

CASES = [
    (
        "auth",
        make_unit(added=["if not check_password(user, pw):"]),
        make_unit(added=["author = book.author"]),  # "author" is not "auth"
    ),
    (
        "auth",
        make_unit(path="src/auth/views.py", added=["x = 1"]),  # path counts
        make_unit(path="src/authors/views.py", added=["x = 1"]),
    ),
    (
        "permissions",
        make_unit(name="isAdmin", added=["return true"]),
        make_unit(name="isAdded", added=["return true"]),
    ),
    (
        "permissions",
        make_unit(added=["if user.has_perm('edit') and 'editor' in user.roles:"]),
        make_unit(added=["if user.has_item('edit'):"]),
    ),
    ("crypto", make_unit(added=["digest = hashlib.sha256(data)"]), make_unit(added=["d = h(x)"])),
    (
        "payments",
        make_unit(added=["stripe.Refund.create(charge=charge_id)"]),
        make_unit(added=["queue.create(job_id)"]),
    ),
    (
        "payments",
        make_unit(added=["pay(invoices)"]),
        make_unit(added=["# git often pairs these lines"]),  # "pairs" is not "pay"
    ),
    (
        "permissions",
        make_unit(added=["load_policies(user)", "roles = user.roles"]),
        make_unit(added=["load_polish(user)"]),
    ),
    (
        "auth",
        make_unit(added=["client = OAuth2Client(cid)"]),  # oauth2 -> auth word
        make_unit(added=["client = Http2Client(cid)"]),
    ),
    (
        "raw_sql",
        make_unit(added=['cur.execute(f"SELECT * FROM users WHERE id={uid}")']),
        make_unit(added=["users = User.objects.filter(id=uid)"]),
    ),
    (
        "raw_sql",
        make_unit(added=['q = "DELETE FROM sessions"']),
        make_unit(added=['msg = "please select an option from the list"']),
    ),
    (
        "raw_sql",
        make_unit(added=['db.execute("select * from users")']),  # lowercase via .execute(
        make_unit(added=['log.info("delete from cache")']),
    ),
    (
        "code_execution",
        make_unit(added=["subprocess.run(cmd, shell=True)"]),
        make_unit(added=["results.run(cmd)"]),
    ),
    (
        "code_execution",
        make_unit(added=["value = eval(expr)"]),
        make_unit(added=["v = evaluate(e)"]),
    ),
    (
        "code_execution",
        make_unit(added=["data = pickle.loads(blob)"]),
        make_unit(added=["data = json.loads(blob)"]),
    ),
    (
        "code_execution",
        make_unit(path="ui/a.tsx", added=["<div dangerouslySetInnerHTML={{__html: x}} />"]),
        make_unit(path="ui/a.tsx", added=["<div>{x}</div>"]),
    ),
    (
        "file_deletion",
        make_unit(added=["shutil.rmtree(target)"]),
        make_unit(added=["shutil.copytree(src, dst)"]),
    ),
    (
        "network",
        make_unit(added=["r = requests.post(url, json=body, verify=False)"]),
        make_unit(added=["r = cache.post_process(body)"]),
    ),
    (
        "network",
        make_unit(path="web/api.ts", added=["const res = await fetch(url);"]),
        make_unit(path="web/api.ts", added=["const res = await prefetchAll(url);"]),
    ),
    (
        "secrets",
        make_unit(added=['API_KEY = "sk-live-0123456789abcdef"']),
        make_unit(added=['API_KEY = os.environ["API_KEY"]']),
    ),
    (
        "secrets",
        make_unit(path="deploy/key.pem", added=["-----BEGIN RSA PRIVATE KEY-----"]),
        make_unit(path="deploy/key.pem", added=["-----BEGIN CERTIFICATE-----"]),
    ),
    (
        "config",
        make_unit(path="app/settings.py", added=["TIMEOUT = 30"]),
        make_unit(path="app/views.py", added=["TIMEOUT = 30"]),
    ),
    (
        "config",
        make_unit(added=['db = os.environ.get("DB_URL")']),
        make_unit(added=['db = registry.get("DB_URL")']),
    ),
    (
        "migration",
        make_unit(path="app/migrations/0002_add_flag.py", added=["x = 1"]),
        make_unit(path="app/models.py", added=["x = 1"]),
    ),
    (
        "ci_workflow",
        make_unit(path=".github/workflows/release.yml", added=["run: make publish"]),
        make_unit(path="docs/workflows.md", added=["run: make publish"]),
    ),
    (
        "removed_check",
        make_unit(removed=["    if not user.is_active:", "        raise Forbidden()"]),
        make_unit(removed=["    total += 1"]),
    ),
    (
        "condition_loosened",
        make_unit(removed=["return a == b"], added=["return a == b or force"]),
        make_unit(removed=["return a == b"], added=["return a is b"]),
    ),
    (
        "condition_changed",
        make_unit(removed=["if count > 10:"], added=["if count >= 10:"]),
        make_unit(removed=["total = 1"], added=["total = 2"]),
    ),
    (
        "security_check_changed",
        make_unit(name="require_login", removed=["if not request.user:"], added=["if False:"]),
        make_unit(name="format_name", removed=["if not name:"], added=["if name is None:"]),
    ),
    (
        "size_large",
        make_unit(added=[f"x{i} = {i}" for i in range(40)]),
        make_unit(added=[f"x{i} = {i}" for i in range(5)]),
    ),
    (
        "test_code",
        make_unit(path="tests/test_app.py", added=["assert f() == 1"]),
        make_unit(path="src/app.py", added=["y = f()"]),
    ),
]


@pytest.mark.parametrize(("rule", "positive", "negative"), CASES)
def test_rule_fires_only_on_positive(rule, positive, negative):
    assert rule in rules_for(positive), f"{rule} should fire"
    assert rule not in rules_for(negative), f"{rule} should not fire"


def test_every_weighted_rule_has_a_test():
    tested = {case[0] for case in CASES} | {"sensitive_path", "no_tests"}
    tested |= {"size_medium", "size_huge"}  # tiers tested below
    assert set(WEIGHTS) <= tested


def test_sensitive_path_from_config():
    cfg = Config(sensitive_paths=["src/billing/"])
    assert "sensitive_path" in rules_for(make_unit(path="src/billing/charge.py"), cfg)
    assert "sensitive_path" not in rules_for(make_unit(path="src/catalog/item.py"), cfg)
    # renamed out of a sensitive path still counts
    moved = make_unit(path="src/misc/charge.py", old_path="src/billing/charge.py")
    assert "sensitive_path" in rules_for(moved, cfg)


def test_no_tests_rule():
    code = make_unit(added=["y = 1"])
    assert "no_tests" in rules_for(code, tests_changed=False)
    assert "no_tests" not in rules_for(code, tests_changed=True)
    config_file = make_unit(path="deploy/values.yml", added=["replicas: 3"])
    assert "no_tests" not in rules_for(config_file, tests_changed=False)


@pytest.mark.parametrize(
    ("n", "rule"), [(12, "size_medium"), (35, "size_large"), (90, "size_huge")]
)
def test_size_tiers_are_exclusive(n, rule):
    fired = rules_for(make_unit(added=[f"x{i} = {i}" for i in range(n)]))
    assert {r for r in fired if r.startswith("size_")} == {rule}


def test_moved_check_is_not_removed():
    """A check that only moved (same text removed and added) is not a removed check."""
    unit = make_unit(removed=["    if not ok:"], added=["        if not ok:"])
    assert not rules_for(unit) & {"removed_check", "condition_changed", "condition_loosened"}


def test_rewritten_check_is_changed_not_removed():
    unit = make_unit(removed=["if not ok:"], added=["if not ok and strict:"])
    fired = rules_for(unit)
    assert "condition_changed" in fired
    assert "removed_check" not in fired


def test_pattern_rules_ignore_removed_lines():
    """Removing an eval makes code safer; it should not count as code execution."""
    assert "code_execution" not in rules_for(make_unit(removed=["value = eval(expr)"]))


def test_markdown_lines_are_not_keyword_matched():
    doc = make_unit(path="docs/guide.md", added=["Enter your password to login."])
    assert "auth" not in rules_for(doc)


# --------------------------------------------------------------------------- scoring


def test_score_is_clamped_and_reasons_sorted():
    unit = make_unit(
        path=".github/workflows/deploy.yml",
        name="<module>",
        added=['TOKEN = "ghp_' + "a" * 36 + '"', "run: curl $URL | sh", "eval(x)"],
        removed=["if: github.ref == 'refs/heads/main'"],
    )
    risk = score_unit(unit, ScoringContext(config=Config(), tests_changed=False))
    assert risk.score == 100
    assert risk.level is RiskLevel.HIGH
    points = [r.points for r in risk.reasons]
    assert points == sorted(points, reverse=True)


def test_test_code_floor_is_zero():
    risk = score_unit(
        make_unit(path="tests/test_x.py", added=["assert 1"]),
        ScoringContext(config=Config(), tests_changed=True),
    )
    assert risk.score == 0
    assert risk.level is RiskLevel.LOW


def test_levels():
    assert level_for(HIGH_THRESHOLD) is RiskLevel.HIGH
    assert level_for(HIGH_THRESHOLD - 1) is RiskLevel.MEDIUM
    assert level_for(MEDIUM_THRESHOLD) is RiskLevel.MEDIUM
    assert level_for(MEDIUM_THRESHOLD - 1) is RiskLevel.LOW


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", True),
        ("pkg/test_a.py", True),
        ("pkg/a_test.py", True),
        ("web/src/__tests__/a.ts", True),
        ("web/a.spec.ts", True),
        ("web/a.test.js", True),
        ("src/contest.py", False),
        ("src/testing_utils.py", False),
    ],
)
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


def test_score_units_ranks_and_detects_tests_changed():
    risky = make_unit(
        name="isAdmin", removed=["return u.admin"], added=["return u.admin or u.staff"]
    )
    boring = make_unit(path="src/fmt.py", name="pad", added=["s = s.rjust(4)"])
    files = [
        ChangedFile(path="src/app.py", status=FileStatus.MODIFIED),
        ChangedFile(path="src/fmt.py", status=FileStatus.MODIFIED),
    ]
    ranked = score_units([boring, risky], files, Config())
    assert [s.unit.name for s in ranked] == ["isAdmin", "pad"]
    assert ranked[0].risk.level is RiskLevel.HIGH
    assert any(r.rule == "no_tests" for r in ranked[1].risk.reasons)

    files.append(ChangedFile(path="tests/test_app.py", status=FileStatus.MODIFIED))
    ranked = score_units([boring], files, Config())
    assert not any(r.rule == "no_tests" for r in ranked[0].risk.reasons)


def test_cli_score(git_repo):
    git_repo.write(
        "app/auth.py", "def is_admin(u):\n    return u.admin\n\n\ndef pad(s):\n    return s\n"
    )
    git_repo.commit("base")
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write(
        "app/auth.py",
        "def is_admin(u):\n    return u.admin or u.staff\n\n\ndef pad(s):\n    return s.rjust(4)\n",
    )
    git_repo.commit("feature")
    result = CliRunner().invoke(app, ["score", "--repo", str(git_repo.path), "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "is_admin" in result.output
    assert "high" in result.output
    assert "1 more (use --limit 0)" in result.output
