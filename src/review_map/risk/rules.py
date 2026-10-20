"""Rule-based risk scoring with human-readable reasons.

Each rule looks at a ChangedUnit (path, name, changed lines) and may add points
with a reason. A unit's score is the sum of its points, clamped to 0-100.
All points live in WEIGHTS so they can be tuned in one place.

Keyword rules match whole words after splitting identifiers (`isAdmin` ->
`is admin`, `reset_password` -> `reset password`), so `author` does not count as
`auth`. Pattern rules (eval, SQL, secrets...) look only at *added* lines: the
risk is in what a change introduces, and removing an `eval` makes code safer.
"""

import re
from dataclasses import dataclass

from review_map.config import Config
from review_map.globs import first_match
from review_map.models import (
    ChangedFile,
    ChangedUnit,
    RiskLevel,
    RiskReason,
    RiskScore,
    ScoredUnit,
)

WEIGHTS: dict[str, int] = {
    "sensitive_path": 30,  # matches `sensitive_paths` in .reviewmap.yml
    "auth": 25,
    "permissions": 25,
    "code_execution": 25,
    "secrets": 25,
    "security_check_changed": 20,  # bonus: auth/permissions unit AND a check changed
    "removed_check": 20,
    "crypto": 20,
    "payments": 20,
    "raw_sql": 20,
    "migration": 20,
    "ci_workflow": 20,
    "condition_loosened": 15,
    "file_deletion": 15,
    "condition_changed": 10,
    "network": 10,
    "config": 10,
    "size_medium": 5,  # >= 10 changed lines
    "size_large": 10,  # >= 30
    "size_huge": 15,  # >= 80
    "no_tests": 5,  # code changed but no test file changed in the whole diff
    "test_code": -20,  # the unit itself is test code: still listed, ranked lower
}

HIGH_THRESHOLD = 60
MEDIUM_THRESHOLD = 30
SIZE_TIERS = [(80, "size_huge"), (30, "size_large"), (10, "size_medium")]

# --------------------------------------------------------------------------- words

KEYWORDS: dict[str, tuple[str, frozenset[str]]] = {
    "auth": (
        "auth",
        frozenset(
            "auth authn authenticate authenticated authentication authenticator login logout "
            "logon signin signon sso saml oauth oidc jwt session token password passwd "
            "credential csrf xsrf cookie mfa otp totp bearer".split()
        ),
    ),
    "permissions": (
        "permissions",
        frozenset(
            "permission perm perms role admin superuser staff authorize authorized "
            "authorization authz acl rbac policy privilege grant sudo forbidden "
            "unauthorized".split()
        ),
    ),
    "crypto": (
        "crypto",
        frozenset(
            "crypto cryptography encrypt decrypt encryption decryption cipher hmac hashlib "
            "sha1 sha256 sha512 md5 bcrypt scrypt argon2 pbkdf2 aes rsa ecdsa nonce salt "
            "signature tls ssl certificate cert x509 jwk keypair".split()
        ),
    ),
    "payments": (
        "payments",
        frozenset(
            "payment pay billing invoice charge refund stripe paypal braintree checkout "
            "subscription price pricing currency wallet payout ledger tax discount "
            "coupon".split()
        ),
    ),
}

_IDENT = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+\d*(?=[A-Z][a-z])|[A-Z]?[a-z]+\d*|[A-Z]+\d*|\d+")


def words(text: str) -> list[str]:
    """Split text into lowercase words, also splitting camelCase and snake_case."""
    out = []
    for token in _IDENT.findall(text):
        out.extend(part.lower() for part in _CAMEL.findall(token))
    return out


def _vocab_hits(candidates: list[str], vocab: frozenset[str]) -> list[str]:
    hits: list[str] = []
    for w in candidates:
        # exact word, simple plural ("roles"), "-ies" plural ("policies"), version ("oauth2")
        forms = (
            w,
            w[:-1] if w.endswith("s") else "",
            w[:-3] + "y" if w.endswith("ies") else "",
            w.rstrip("0123456789"),
        )
        hit = next((f for f in forms if f in vocab), None)
        if hit and hit not in hits:
            hits.append(hit)
    return hits


# --------------------------------------------------------------------------- patterns


def _rx(*patterns: str, flags: int = 0) -> list[re.Pattern[str]]:
    return [re.compile(p, flags) for p in patterns]


PATTERNS: dict[str, tuple[str, list[re.Pattern[str]]]] = {
    "raw_sql": (
        "raw SQL",
        _rx(
            r"\bSELECT\b.+\bFROM\b",
            r"\bINSERT\s+INTO\b",
            r"\bUPDATE\s+\w+\s+SET\b",
            r"\bDELETE\s+FROM\b",
            r"\b(DROP|ALTER|TRUNCATE)\s+TABLE\b",
        )  # uppercase only: "select an option from the list" is English, not SQL
        + _rx(r"\.(execute|executemany|executescript|raw)\s*\("),
    ),
    "code_execution": (
        "shell/eval/unsafe load",
        _rx(
            r"(?<![\w.])(eval|exec)\s*\(",
            r"\bsubprocess\b",
            r"\bos\.(system|popen|exec\w*|spawn\w*)\s*\(",
            r"\bshell\s*=\s*True\b",
            r"\bchild_process\b",
            r"\b(execSync|spawnSync|execFile)\s*\(",
            r"\bnew\s+Function\s*\(",
            r"\b(pickle|marshal|dill)\.loads?\s*\(",
            r"\byaml\.load\s*\(",
            r"(?<![\w.])__import__\s*\(",
            r"\bdangerouslySetInnerHTML\b",
            r"\.innerHTML\s*=",
        ),
    ),
    "file_deletion": (
        "deletes files",
        _rx(
            r"\bos\.(remove|unlink|rmdir|removedirs)\s*\(",
            r"\bshutil\.rmtree\s*\(",
            r"\.(unlink|rmdir)\s*\(",
            r"\bfs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\(",
            r"\brimraf\b",
            r"\brm\s+-[a-zA-Z]*[rf]",
        ),
    ),
    "network": (
        "network call",
        _rx(
            r"\brequests\.(get|post|put|patch|delete|head|request|Session)\b",
            r"\bhttpx\.",
            r"\burllib\.request\b",
            r"\burlopen\s*\(",
            r"\baiohttp\b",
            r"\bhttp\.client\b",
            r"\bsocket\.socket\s*\(",
            r"(?<![\w.])fetch\s*\(",
            r"\baxios\b",
            r"\bXMLHttpRequest\b",
            r"\bverify\s*=\s*False\b",
            r"\brejectUnauthorized\s*:\s*false\b",
        ),
    ),
    "secrets": (
        "hardcoded secret?",
        _rx(
            r"(?i)\b\w*(secret|api_?key|access_?key|private_?key|password|passwd|token)\w*"
            r"""["']?\s*[:=]\s*["'][^"'\s]{8,}["']""",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
            r"\bsk-[A-Za-z0-9_-]{20,}\b",
        ),
    ),
}

CONFIG_LINE_PATTERNS = _rx(
    r"\bos\.environ\b",
    r"\bos\.getenv\s*\(",
    r"\bprocess\.env\b",
    r"\bDEBUG\s*=\s*True\b",
    r"\b(ALLOWED_HOSTS|CORS_\w+|SECRET_KEY)\b",
)

# --------------------------------------------------------------------------- paths

MIGRATION_PATHS = ["migrations/", "alembic/", "db/migrate/", "*.sql", "schema.prisma"]
CI_PATHS = [
    ".github/workflows/",
    ".github/actions/",
    ".gitlab-ci.yml",
    ".circleci/",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    "/action.yml",
    "/action.yaml",
]
CONFIG_PATHS = [
    "settings.py",
    "settings/",
    "config/",
    ".env",
    ".env.*",
    "*.ini",
    "*.cfg",
    "*.conf",
    "Dockerfile",
    "docker-compose*.yml",
    "*.tf",
    "pyproject.toml",
    "setup.py",
    "requirements*.txt",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
]
TEST_PATHS = [
    "tests/",
    "test/",
    "__tests__/",
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "*.test.*",
    "*.spec.*",
    "*_test.go",
]

# Languages whose changed *lines* are prose rather than code: only their path counts.
_PROSE = {"markdown", None}
# Source-code languages (for the "no tests changed" signal; config files don't need tests).
_CODE = {
    "python", "javascript", "typescript", "tsx", "go", "rust", "java", "ruby", "php", "c",
    "cpp", "csharp",
}  # fmt: skip

_CHECK = re.compile(
    r"^\s*(?:\}\s*)?(if|elif|else\s+if|unless|assert|raise|throw|guard|require|abort)\b"
)
_CONDITION = re.compile(r"^\s*(?:\}\s*)?(if|elif|else\s+if|while|return|assert)\b")
_LOOSEN = re.compile(r"\bor\b|\|\||\?\?")


def is_test_path(path: str) -> bool:
    return first_match(path, TEST_PATHS) is not None


def level_for(score: int) -> RiskLevel:
    if score >= HIGH_THRESHOLD:
        return RiskLevel.HIGH
    if score >= MEDIUM_THRESHOLD:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


@dataclass
class ScoringContext:
    config: Config
    tests_changed: bool  # does the diff touch any test file?


def _hit(rule: str, message: str) -> RiskReason:
    return RiskReason(rule=rule, points=WEIGHTS[rule], message=message)


def _fmt(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (", ..." if len(items) > limit else "")


def _keyword_reasons(unit: ChangedUnit) -> list[RiskReason]:
    context = words(unit.path) + words(unit.old_path or "") + words(unit.name)
    lines = [] if unit.language in _PROSE else words(" ".join(unit.added_text + unit.removed_text))
    reasons = []
    for rule, (label, vocab) in KEYWORDS.items():
        hits = _vocab_hits(context + lines, vocab)
        if hits:
            reasons.append(_hit(rule, f"{label}: {_fmt(hits)}"))
    return reasons


def _pattern_reasons(unit: ChangedUnit) -> list[RiskReason]:
    reasons = []
    for rule, (label, patterns) in PATTERNS.items():
        for text in unit.added_text:
            match = next((m for p in patterns if (m := p.search(text))), None)
            if match:
                snippet = match.group(0).strip()
                snippet = snippet if len(snippet) <= 40 else snippet[:37] + "..."
                reasons.append(_hit(rule, f"{label} `{snippet}`"))
                break
    return reasons


def _check_reasons(unit: ChangedUnit) -> list[RiskReason]:
    """Removed checks and changed conditions; lines that only moved are ignored."""
    added = {t.strip() for t in unit.added_text}
    removed = {t.strip() for t in unit.removed_text}
    gone = [t for t in unit.removed_text if t.strip() not in added]
    new = [t for t in unit.added_text if t.strip() not in removed]

    removed_checks = [t for t in gone if _CHECK.match(t)]
    added_checks = [t for t in new if _CHECK.match(t)]
    if removed_checks and not added_checks:
        n = len(removed_checks)
        what = "check" if n == 1 else f"{n} checks"
        return [_hit("removed_check", f"removed {what} without replacement")]

    old_conds = [t for t in gone if _CONDITION.match(t)]
    new_conds = [t for t in new if _CONDITION.match(t)]
    if old_conds and new_conds:
        before = sum(len(_LOOSEN.findall(t)) for t in old_conds)
        after = sum(len(_LOOSEN.findall(t)) for t in new_conds)
        if after > before:
            return [_hit("condition_loosened", "loosened condition (`or`/`||`)")]
        return [_hit("condition_changed", "changed condition/return")]
    return []


def _path_reasons(unit: ChangedUnit, ctx: ScoringContext) -> list[RiskReason]:
    paths = [unit.path] + ([unit.old_path] if unit.old_path else [])
    reasons = []

    def match(patterns: list[str]) -> str | None:
        return next((m for p in paths if (m := first_match(p, patterns))), None)

    if pattern := match(ctx.config.sensitive_paths):
        reasons.append(_hit("sensitive_path", f"sensitive path `{pattern}`"))
    if match(MIGRATION_PATHS):
        reasons.append(_hit("migration", "DB migration/schema"))
    if match(CI_PATHS):
        reasons.append(_hit("ci_workflow", "CI workflow (has secrets)"))
    if match(CONFIG_PATHS):
        reasons.append(_hit("config", "config/dependency file"))
    elif any(p.search(t) for p in CONFIG_LINE_PATTERNS for t in unit.added_text):
        reasons.append(_hit("config", "reads config/env"))
    return reasons


def score_unit(unit: ChangedUnit, ctx: ScoringContext) -> RiskScore:
    reasons = _path_reasons(unit, ctx)
    keyword = _keyword_reasons(unit)
    checks = _check_reasons(unit)
    reasons += keyword + _pattern_reasons(unit) + checks

    if checks and any(r.rule in ("auth", "permissions") for r in keyword):
        reasons.append(_hit("security_check_changed", "security check changed"))

    for minimum, rule in SIZE_TIERS:
        if unit.lines_changed >= minimum:
            reasons.append(_hit(rule, f"large change ({unit.lines_changed} lines)"))
            break

    if is_test_path(unit.path):
        reasons.append(_hit("test_code", "test code"))
    elif not ctx.tests_changed and unit.language in _CODE:
        reasons.append(_hit("no_tests", "no tests changed"))

    reasons.sort(key=lambda r: -r.points)
    score = max(0, min(100, sum(r.points for r in reasons)))
    return RiskScore(score=score, level=level_for(score), reasons=reasons)


def score_units(
    units: list[ChangedUnit], files: list[ChangedFile], config: Config
) -> list[ScoredUnit]:
    """Score every unit; highest risk first (ties keep file order)."""
    ctx = ScoringContext(config=config, tests_changed=any(is_test_path(f.path) for f in files))
    scored = [ScoredUnit(unit=u, risk=score_unit(u, ctx)) for u in units]
    scored.sort(key=lambda s: -s.risk.score)
    return scored
