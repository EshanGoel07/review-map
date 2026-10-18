"""Tests for gitignore-style glob matching."""

import pytest

from review_map.globs import first_match, match_path


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # no slash: matches the name at any depth
        ("*.py", "a.py", True),
        ("*.py", "src/deep/a.py", True),
        ("*.py", "a.pyc", False),
        ("yarn.lock", "web/yarn.lock", True),
        ("migrations", "app/migrations/0001.py", True),
        # trailing slash: everything under that directory, at any depth
        ("vendor/", "vendor/a.js", True),
        ("vendor/", "lib/vendor/a.js", True),
        ("vendor/", "vendors/a.js", False),
        # slash inside: anchored at the root
        ("src/auth/*.py", "src/auth/login.py", True),
        ("src/auth/*.py", "src/auth/sub/login.py", False),
        ("src/auth/*.py", "other/src/auth/login.py", False),
        ("/dist/", "dist/x.js", True),
        ("/dist/", "pkg/dist/x.js", False),
        # double star
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/y/a.py", True),
        ("**/secrets/**", "config/secrets/key.txt", True),
        # ? and literal dots
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("a.b", "aXb", False),
    ],
)
def test_match_path(pattern, path, expected):
    assert match_path(path, pattern) is expected


def test_first_match_returns_pattern():
    assert first_match("src/a.py", ["*.js", "*.py", "src/"]) == "*.py"
    assert first_match("README.md", ["*.js"]) is None
