"""Build the Review Map demo repository.

    python examples/demo_repo/build.py [TARGET]      (default: <temp dir>/review-map-demo)

Creates a git repo with two commits: `main` holds `base/`, and the branch
`feature/paginate-notes` holds `head/` plus `changes.md`. The description claims a
pagination change; the branch also quietly lets anyone delete shared notes.

A real git repo can't be committed inside this one, so the demo is stored as plain
files and built on demand. Uses only the standard library and `git`.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TARGET = Path(tempfile.gettempdir()) / "review-map-demo"
BRANCH = "feature/paginate-notes"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def build(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    git(target, "init", "-q", "-b", "main")
    # Repo-local identity, so building works without a global git config.
    git(target, "config", "user.name", "Review Map Demo")
    git(target, "config", "user.email", "demo@example.com")
    git(target, "config", "commit.gpgsign", "false")

    copy_tree(HERE / "base", target)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "Notes service")

    git(target, "checkout", "-q", "-b", BRANCH)
    git(target, "rm", "-rq", ".")
    copy_tree(HERE / "head", target)
    shutil.copyfile(HERE / "changes.md", target / "changes.md")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "Paginate the notes list")


def main(argv: list[str]) -> int:
    target = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_TARGET
    if target.exists() and any(target.iterdir()):
        print(f"error: {target} already exists and is not empty; pick another folder.")
        return 2
    try:
        build(target)
    except FileNotFoundError:
        print("error: git is not installed or not on PATH.")
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: git {' '.join(exc.cmd[3:])} failed:\n{exc.stderr}")
        return 2
    print(f"Demo repo ready at {target} (branch {BRANCH}). Try:")
    print(f"  review-map analyze --repo {target} --base main")
    print(f"  review-map analyze --repo {target} --base main --format html --out report.html")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
