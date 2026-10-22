"""Table-driven tests for the scope verifier (plain code, no LLM)."""

import pytest

from review_map.models import (
    MODULE_UNIT,
    ChangedFile,
    ChangedUnit,
    ClaimedScope,
    ClaimSource,
    FileStatus,
    MatchLevel,
    UnitKind,
)
from review_map.verify.scope import (
    BROAD_CLAIM_FILES,
    FunctionClaim,
    function_matches,
    is_wildcard_only,
    normalize_path,
    parse_function_claim,
    path_level,
    verify_scope,
)

FILE, FOLDER, FUNCTION, NONE = (
    MatchLevel.FILE,
    MatchLevel.FOLDER,
    MatchLevel.FUNCTION,
    MatchLevel.NONE,
)


def unit(path: str, name: str, old_path: str | None = None) -> ChangedUnit:
    kind = UnitKind.MODULE if name == MODULE_UNIT else UnitKind.FUNCTION
    return ChangedUnit(
        path=path, old_path=old_path, name=name, kind=kind, start_line=1, end_line=2,
        added_lines=[1],
    )  # fmt: skip


def changed(path: str, **kwargs) -> ChangedFile:
    return ChangedFile(path=path, status=kwargs.pop("status", FileStatus.MODIFIED), **kwargs)


def claims(**kwargs) -> ClaimedScope:
    return ClaimedScope(source=ClaimSource.LLM, **kwargs)


def run(scope: ClaimedScope, units: list[ChangedUnit], files: list[ChangedFile] | None = None):
    files = (
        files if files is not None else [changed(p) for p in dict.fromkeys(u.path for u in units)]
    )
    return verify_scope(scope, units, files)


def levels(report) -> dict[str, MatchLevel]:
    return {c.unit_id: c.level for c in report.coverage}


# --- Path claims -------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "path", "expected"),
    [
        ("src/auth/session.py", "src/auth/session.py", FILE),  # exact
        ("auth/session.py", "src/auth/session.py", FILE),  # shortened path
        ("session.py", "src/auth/session.py", FILE),  # file name only
        ("ion.py", "src/auth/session.py", None),  # not a whole segment
        ("./src/auth/session.py", "src/auth/session.py", FILE),
        ("/src/auth/session.py", "src/auth/session.py", FILE),
        ("src\\auth\\session.py", "src/auth/session.py", FILE),  # Windows separators
        ("`src/auth/session.py`", "src/auth/session.py", FILE),
        ("src/auth/", "src/auth/session.py", FOLDER),  # folder claim
        ("src/auth/", "src/auth/deep/token.py", FOLDER),
        ("auth/", "src/auth/session.py", FOLDER),  # folder anywhere in the path
        ("src/auth", "src/auth/session.py", FOLDER),  # folder without trailing slash
        ("src/au/", "src/auth/session.py", None),
        ("src/auth/", "src/authz/x.py", None),
        ("src/auth/*.py", "src/auth/session.py", FOLDER),  # glob
        ("src/auth/*.py", "src/auth/deep/x.py", None),
        ("src/session.py", "src/auth/session.py", None),
        ("**", "anything.py", None),  # wildcard-only claims never match
        ("", "a.py", None),
    ],
)
def test_path_level(claim, path, expected):
    assert path_level(claim, path) == expected


@pytest.mark.parametrize(
    ("claim", "wild"), [("**", True), ("*", True), (".", True), ("/", True), ("./**/*", True),
                        ("src/", False), ("*.py", False)],
)  # fmt: skip
def test_is_wildcard_only(claim, wild):
    assert is_wildcard_only(claim) is wild


def test_normalize_path():
    assert normalize_path(" ././src\\a.py ") == "src/a.py"


# --- Function claims -----------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "path", "name", "expected"),
    [
        ("login", "auth.py", "login", True),
        ("login", "auth.py", "UserService.login", True),  # method by short name
        ("login()", "auth.py", "login", True),
        ("UserService.login", "auth.py", "UserService.login", True),
        ("UserService.login", "auth.py", "Admin.login", False),
        ("UserService", "auth.py", "UserService.login", True),  # class covers its methods
        ("UserService", "auth.py", "UserServiceFactory", False),
        ("log", "auth.py", "login", False),  # no partial names
        ("auth.py:login", "src/auth.py", "login", True),  # file-qualified
        ("auth.py::login", "src/auth.py", "login", True),
        ("auth.py:login", "src/other.py", "login", False),
        ("UserService::login", "auth.py", "UserService.login", True),  # C++/Rust style
        ("auth.session.refresh", "src/auth/session.py", "refresh", True),  # dotted module path
        ("auth.session.refresh", "src/auth/tokens.py", "refresh", False),
        ("session.Store.get", "app/session.py", "Store.get", True),
        ("login", "auth.py", MODULE_UNIT, False),  # module code never matches a name
    ],
)
def test_function_matches(claim, path, name, expected):
    assert function_matches(parse_function_claim(claim), unit(path, name)) is expected


def test_function_claim_on_renamed_file_matches_old_path():
    u = unit("src/new.py", "login", old_path="src/old.py")
    assert function_matches(parse_function_claim("old.py:login"), u)


def test_parse_function_claim():
    assert parse_function_claim("`auth.py:login()`") == FunctionClaim(
        "`auth.py:login()`", "login", "auth.py"
    )
    assert parse_function_claim("A::b").name == "A.b"


# --- Coverage --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        # Named directly.
        (claims(functions=["paginate"]), {"paginate": FUNCTION, "can_edit": NONE}),
        # File claim, no names: covers every unit in the file.
        (claims(files=["app/views.py"]), {"paginate": FILE, "can_edit": FILE}),
        # Folder claim.
        (claims(files=["app/"]), {"paginate": FOLDER, "can_edit": FOLDER}),
        # Sibling rule: the file is mentioned but other functions in it are named.
        (
            claims(files=["app/views.py"], functions=["paginate"]),
            {"paginate": FUNCTION, "can_edit": NONE},
        ),
        # Same through a folder claim.
        (claims(files=["app/"], functions=["paginate"]), {"paginate": FUNCTION, "can_edit": NONE}),
        # Nothing claimed.
        (claims(), {"paginate": NONE, "can_edit": NONE}),
        # Claims about other files.
        (claims(files=["other.py"], functions=["other"]), {"paginate": NONE, "can_edit": NONE}),
    ],
    ids=["function", "file", "folder", "sibling-file", "sibling-folder", "empty", "elsewhere"],
)
def test_coverage(scope, expected):
    report = run(scope, [unit("app/views.py", "paginate"), unit("app/views.py", "can_edit")])
    assert {c.name: c.level for c in report.coverage} == expected
    assert {c.name for c in report.unclaimed_changes} == {
        n for n, lvl in expected.items() if lvl is NONE
    }


def test_sibling_rule_explains_itself():
    report = run(
        claims(files=["app/views.py"], functions=["paginate"]),
        [unit("app/views.py", "paginate"), unit("app/views.py", "can_edit")],
    )
    (hidden,) = report.unclaimed_changes
    assert hidden.name == "can_edit"
    assert "app/views.py is mentioned, but this function is not" in hidden.note


def test_sibling_rule_is_per_file():
    report = run(
        claims(files=["a.py", "b.py"], functions=["f"]),
        [unit("a.py", "f"), unit("a.py", "g"), unit("b.py", "h")],
    )
    assert levels(report) == {"a.py::f": FUNCTION, "a.py::g": NONE, "b.py::h": FILE}


def test_file_claim_beats_folder_claim():
    report = run(claims(files=["src/", "src/a.py"]), [unit("src/a.py", "f")])
    assert report.coverage[0].level is FILE
    assert report.coverage[0].claim == "src/a.py"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (claims(files=["a.py"]), FILE),  # file claimed
        (claims(functions=["f"]), FILE),  # a sibling is claimed: imports support it
        (claims(files=["a.py"], functions=["f"]), FILE),  # sibling rule doesn't apply
        (claims(functions=["other"]), NONE),
        (claims(), NONE),
    ],
)
def test_module_code(scope, expected):
    report = run(scope, [unit("a.py", MODULE_UNIT), unit("a.py", "f")])
    assert levels(report)["a.py::<module>"] is expected


def test_non_code_file_is_covered_by_file_claim():
    report = run(claims(files=["config.yaml"]), [unit("config.yaml", MODULE_UNIT)])
    assert report.coverage[0].level is FILE


def test_renamed_file_matches_old_or_new_path():
    files = [changed("src/new.py", old_path="src/old.py", status=FileStatus.RENAMED)]
    units = [unit("src/new.py", "f", old_path="src/old.py")]
    for claim in ("src/old.py", "src/new.py"):
        report = run(claims(files=[claim]), units, files)
        assert report.coverage[0].level is FILE
        assert report.claimed_but_missing == []


def test_function_rename_with_only_new_name_flags_the_old_one():
    # Phase 2 reports a renamed function as old (removed) + new (added).
    old = unit("cli.py", "parse_args")
    new = unit("cli.py", "parse_cli_args")
    report = run(claims(functions=["parse_cli_args"]), [old, new])
    assert [c.name for c in report.unclaimed_changes] == ["parse_args"]
    report = run(claims(functions=["parse_args", "parse_cli_args"]), [old, new])
    assert report.unclaimed_changes == []


# --- Skipped files -----------------------------------------------------------


def test_generated_ignored_and_binary_files_are_skipped():
    files = [
        changed("app.py"),
        changed("poetry.lock", generated_reason="lockfile"),
        changed("docs/x.md", ignored_by="docs/"),
        changed("logo.png", is_binary=True),
    ]
    report = verify_scope(claims(functions=["f"]), [unit("app.py", "f")], files)
    assert report.skipped_files == ["poetry.lock", "docs/x.md", "logo.png"]
    assert report.unclaimed_changes == []


def test_claiming_a_skipped_file_is_not_missing():
    files = [changed("app.py"), changed("poetry.lock", generated_reason="lockfile")]
    report = verify_scope(claims(files=["poetry.lock", "app.py"]), [unit("app.py", "f")], files)
    assert report.claimed_but_missing == []


def test_description_file_is_not_a_change():
    units = [unit("changes.md", MODULE_UNIT), unit("app.py", "f")]
    files = [changed("changes.md"), changed("app.py")]
    report = verify_scope(claims(functions=["f"]), units, files, description_path="changes.md")
    assert [c.unit_id for c in report.coverage] == ["app.py::f"]
    assert report.unclaimed_changes == []


# --- Claimed but missing ---------------------------------------------------------


def test_claimed_but_missing():
    report = run(
        claims(
            files=["app.py", "tests/test_app.py", "src/gone/"],
            functions=["f", "never_changed", "a.py:f"],
        ),
        [unit("app.py", "f")],
    )
    assert [(m.kind, m.claim) for m in report.claimed_but_missing] == [
        ("file", "tests/test_app.py"),
        ("file", "src/gone/"),
        ("function", "never_changed"),
        ("function", "a.py:f"),  # f changed, but not in a.py
    ]


def test_deleted_file_claim_is_not_missing():
    files = [changed("old.py", status=FileStatus.DELETED)]
    report = verify_scope(claims(files=["old.py"]), [unit("old.py", "f")], files)
    assert report.claimed_but_missing == []
    assert report.coverage[0].level is FILE


# --- Out of scope ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "violations"),
    [
        ("app/payments/", [("app/payments/refund.py", "refund")]),  # LLM style: bare path
        ("Payments (`app/payments/`) are not touched.", [("app/payments/refund.py", "refund")]),
        ("payments/refund.py", [("app/payments/refund.py", "refund")]),
        ("refund", [("app/payments/refund.py", "refund")]),  # bare identifier
        ("We did not touch `refund()`", [("app/payments/refund.py", "refund")]),
        ("app/search/", []),  # checkable, but nothing there changed
        ("LoginView", []),
    ],
)
def test_out_of_scope_violations(item, violations):
    report = run(
        claims(functions=["paginate"], out_of_scope=[item]),
        [unit("app/api/views.py", "paginate"), unit("app/payments/refund.py", "refund")],
    )
    assert [(v.path, v.name) for v in report.out_of_scope_violations] == violations
    assert all(v.item == item for v in report.out_of_scope_violations)
    assert report.not_checkable == []


@pytest.mark.parametrize("item", ["the login form", "database layer", "Search page."])
def test_prose_out_of_scope_is_not_checkable(item):
    report = run(claims(out_of_scope=[item]), [unit("app/login.py", "login_form")])
    assert report.out_of_scope_violations == []
    assert report.not_checkable == [item]


def test_out_of_scope_file_without_units_is_a_file_violation():
    files = [changed("pay/new.py", old_path="pay/old.py", status=FileStatus.RENAMED)]
    report = verify_scope(claims(out_of_scope=["pay/"]), [], files)  # pure rename: no units
    assert [(v.path, v.name) for v in report.out_of_scope_violations] == [("pay/new.py", None)]


def test_out_of_scope_ignores_skipped_files():
    files = [changed("vendor/lib.js", generated_reason="vendored")]
    report = verify_scope(claims(out_of_scope=["vendor/"]), [], files)
    assert report.out_of_scope_violations == []


def test_violations_are_not_duplicated():
    report = run(
        claims(out_of_scope=["Neither `pay.py` nor `refund()` changed, see pay.py"]),
        [unit("pay.py", "refund")],
    )
    assert len(report.out_of_scope_violations) == 1


def test_claimed_and_out_of_scope_both_reported():
    # A contradictory description: the verifier reports both sides.
    report = run(claims(functions=["f"], out_of_scope=["a.py"]), [unit("a.py", "f")])
    assert report.coverage[0].level is FUNCTION
    assert len(report.out_of_scope_violations) == 1


# --- Notes and broad claims ----------------------------------------------------------


def test_wildcard_claim_is_ignored_with_a_note():
    # An injected "claim everything" can't hide changes.
    report = run(claims(files=["**", "."]), [unit("a.py", "f")])
    assert [c.level for c in report.coverage] == [NONE]
    assert any("match everything: **, ." in n for n in report.notes)
    assert report.claimed_but_missing == []


def test_broad_folder_claim_gets_a_note():
    units = [unit(f"src/m{i}.py", "f") for i in range(BROAD_CLAIM_FILES)]
    report = run(claims(files=["src/"]), units)
    assert any("Broad claim src/ covers 10 files" in n for n in report.notes)
    report = run(claims(files=["src/"]), units[:-1])
    assert not any("Broad claim" in n for n in report.notes)


def test_no_claims_note():
    report = run(claims(behaviors=["made it faster"]), [unit("a.py", "f")])
    assert any("names no files or functions" in n for n in report.notes)


def test_heuristic_claims_are_marked_low_confidence():
    scope = ClaimedScope(files=["a.py"], low_confidence=True, source=ClaimSource.HEURISTIC)
    report = run(scope, [unit("a.py", "f")])
    assert report.low_confidence is True
    assert any("heuristic parser" in n for n in report.notes)


def test_report_json_includes_unclaimed_changes():
    report = run(claims(), [unit("a.py", "f")])
    data = report.model_dump(mode="json")
    assert data["unclaimed_changes"][0]["unit_id"] == "a.py::f"
    assert data["coverage"][0]["level"] == "none"
