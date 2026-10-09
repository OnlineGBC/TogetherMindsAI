"""
Raw SQL in the app must work on Postgres, not just SQLite.

The tests run on SQLite, which accepts things Postgres rejects. Startup ALTERs are
wrapped in try/except, so a rejected one fails silently and the column never
appears — that is how `promo_codes.alerted_nearly` went missing in production and
turned /accessadmin into a 500. This scans every string literal in the app for the
known SQLite-only habits. It is a list of known-bad patterns, not a proof.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {"tests", "venv", ".venv", "node_modules", "migrations", "scripts", "build"}

# Only strings that look like SQL are checked, so prose and comments never trip it.
_LOOKS_LIKE_SQL = re.compile(r"^\s*(ALTER|CREATE|INSERT|UPDATE|DELETE|SELECT|DROP)\b", re.I)

_BAD = [
    (re.compile(r"\bDATETIME\b", re.I), "DATETIME is not a Postgres type; use TIMESTAMP"),
    (re.compile(r"\bBOOLEAN\b[^,)]*\bDEFAULT\s+[01]\b", re.I),
     "integer default on a BOOLEAN; use DEFAULT FALSE/TRUE"),
    (re.compile(r"\bdatetime\s*\(\s*'now'", re.I), "SQLite datetime('now'); use CURRENT_TIMESTAMP"),
    (re.compile(r"\bstrftime\s*\(", re.I), "SQLite strftime(); use to_char/extract"),
    (re.compile(r"\bifnull\s*\(", re.I), "SQLite ifnull(); use COALESCE"),
    (re.compile(r"\bgroup_concat\s*\(", re.I), "SQLite group_concat(); use string_agg"),
    (re.compile(r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\b", re.I),
     "SQLite INSERT OR ...; use ON CONFLICT"),
    (re.compile(r"\bPRAGMA\b", re.I), "SQLite PRAGMA has no Postgres equivalent"),
    (re.compile(r"\bAUTOINCREMENT\b", re.I), "SQLite AUTOINCREMENT; use SERIAL/IDENTITY"),
]


def _app_python_files():
    """Our own .py files. Walks the tree itself so it never descends into a
    virtualenv (this repo's is named TogetherMindsAI.venv) or other vendored code."""
    stack = [ROOT]
    while stack:
        folder = stack.pop()
        for entry in folder.iterdir():
            if entry.is_dir():
                if (entry.name in _SKIP_DIRS or entry.name.endswith(".venv")
                        or entry.name.startswith(".") or entry.name == "__pycache__"
                        or (entry / "pyvenv.cfg").exists()):
                    continue
                stack.append(entry)
            elif entry.suffix == ".py":
                yield entry


def _sql_strings():
    for path in _app_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and _LOOKS_LIKE_SQL.match(node.value)):
                yield path.relative_to(ROOT), node.lineno, node.value


def find_problems(sql_strings):
    return [(str(p), line, why, text.strip()[:80])
            for p, line, text in sql_strings
            for pattern, why in _BAD if pattern.search(text)]


def test_app_sql_is_postgres_safe():
    problems = find_problems(_sql_strings())
    assert not problems, "SQLite-only SQL found:\n" + "\n".join(
        f"  {p}:{n}: {why}  [{snippet}]" for p, n, why, snippet in problems)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE promo_codes ADD COLUMN alerted_nearly BOOLEAN DEFAULT 0",
    "ALTER TABLE x ADD COLUMN t DATETIME",
    "SELECT ifnull(a, 0) FROM t",
    "INSERT OR REPLACE INTO t VALUES (1)",
])
def test_scanner_catches_known_bad_sql(sql):
    assert find_problems([(Path("x.py"), 1, sql)])


def test_scanner_accepts_postgres_safe_sql():
    assert not find_problems([(Path("x.py"), 1,
        "ALTER TABLE promo_codes ADD COLUMN alerted_nearly BOOLEAN DEFAULT FALSE")])
