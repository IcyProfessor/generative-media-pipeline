"""Small public-tree hygiene check; not a vulnerability or full secret audit.

Scans distributable source/docs/config/tests for credential-shaped strings and
forbidden files. Never prints a matched secret. Run on the staged archive tree
as well as the development checkout. This does not inspect past git commits.
"""
from pathlib import Path
import ast
import argparse
import json
import re
import sys

IGNORED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "build", "dist", "output", "htmlcov"}
TEXT_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".cmd", ".in", ".example"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "telegram_token": re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "google_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b"),
}
FORBIDDEN_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2", ".pyc", ".pyo", ".pem"}


def check(root: Path, *, strict=False) -> dict:
    findings, scanned = [], 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            findings.append({"file": str(relative), "rule": "symlink"})
            continue
        ignored = any(part in IGNORED or part.endswith(".egg-info") for part in relative.parts)
        local = (relative.parts[:2] == ("assets", "local") or
                 relative.parts[0] == "configs" and path.name.startswith("local"))
        if ignored or local:
            if strict and path.is_file():
                findings.append({"file": str(relative), "rule": "private_or_generated_bundle_file"})
            continue
        if not path.is_file():
            continue
        scanned += 1
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append({"file": str(relative), "rule": "forbidden_distribution_file"})
        if path.name.startswith(".env") and path.name != ".env.example":
            findings.append({"file": str(relative), "rule": "private_env_file"})
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            findings.append({"file": str(relative), "rule": "unreadable_text"})
            continue
        for rule, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append({"file": str(relative), "rule": rule})
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(relative), feature_version=(3, 12))
            except SyntaxError:
                findings.append({"file": str(relative), "rule": "python_312_syntax"})
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.endswith(("API_KEY", "TOKEN", "SECRET")):
                        literal = value.value.strip()
                        if len(literal) >= 24 and not any(x in literal.lower() for x in ("example", "placeholder", "test", "fake")):
                            findings.append({"file": str(relative), "line": node.lineno, "rule": "literal_credential"})
    return {"files_scanned": scanned, "findings": findings,
            "scope": "current public tree only; no git history, dependency advisories or binary content audit"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true", help="Also reject ignored/local files inside a staged bundle")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("Root must be an existing directory")
    result = check(root, strict=args.strict)
    print(json.dumps(result, indent=2))
    return bool(result["findings"])


if __name__ == "__main__":
    raise SystemExit(main())
