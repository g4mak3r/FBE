from __future__ import annotations

import argparse
import re
import sys
import json
import zipfile
import subprocess
from pathlib import Path

ALLOWED_ENV = {".env.example"}
FORBIDDEN_EXACT = {
    ".env",
    "config.json",
    "data/fbe.db",
    "data/suz_local_settings.json",
    "data/economy_settings.json",
    "data/local/connections.json",
}
FORBIDDEN_PARTS = {
    ".venv", "venv", "__pycache__", ".pytest_cache",
    "runtime", "diagnostics", "backups", "logs", "caches", "cache",
    "data/workspaces", "data/unconnected", "data/print", "data/raw_suz",
    "data/diagnostics", "data/economy/raw", "data/logs", "data/local", "data/demo",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pfx", ".p12", ".pem", ".key",
    ".crt", ".cer", ".log", ".jsonl", ".pdf", ".btw",
    ".db-wal", ".db-shm", ".db-journal", ".sqlite-wal", ".sqlite-shm",
    ".sqlite3-wal", ".sqlite3-shm", ".sqlite-journal", ".sqlite3-journal",
    ".p7b", ".p7c", ".der", ".jks", ".keystore", ".csr",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bak", ".dump",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".bat", ".ps1",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".example",
}
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


SAFE_DATA_FILES = {
    "data/KEEP_EXISTING_DATA.txt", "data/products.example.xlsx",
    "data/inventory.example.xlsx", "data/FBE_catalog_template_0.81.1.xlsx",
}
SECRET_KEYS = {
    "wb_token", "token", "password", "unified_token", "statistics_token",
    "finance_token", "promotion_token", "suz_oms_id", "suz_connection_id",
    "suz_cert_thumbprint", "suz_auth_inn", "suz_true_participant_inn",
    "suz_true_producer_inn", "suz_true_owner_inn", "oms_id", "connection_id",
    "cert_thumbprint", "participant_inn", "tin",
}


def safe_example(value) -> bool:
    if value is None or value is False or value == "":
        return True
    raw = str(value).upper()
    return raw.startswith(("DEMO", "TEST", "SYNTHETIC", "PLACEHOLDER", "PASTE_", "YOUR_")) or set(raw) <= {"0", "-"}


def check_env(text: str) -> bool:
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if ("TOKEN" in key.upper() or key.upper().startswith("CZ_")) and "URL" not in key.upper():
            if not safe_example(value.strip("\"'")):
                return True
    return False


def check_json(value) -> bool:
    if isinstance(value, dict):
        return any((str(k).lower() in SECRET_KEYS and not safe_example(v)) or check_json(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(check_json(v) for v in value)
    return False


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def scan(root: Path) -> list[str]:
    problems: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = _rel(path, root)
        rel_parts = set(Path(rel).parts)
        if path.name == ".gitkeep":
            continue
        if path.is_symlink():
            problems.append(f"symlink requires explicit review: {rel}")
            continue
        if rel.startswith("data/") and rel not in SAFE_DATA_FILES:
            problems.append(f"unapproved data artifact: {rel}")
            continue
        if path.name in {"connections.json", "credentials.json", "config.json"}:
            problems.append(f"local configuration file: {rel}")
            continue
        if path.suffix.lower() == ".xlsx":
            try:
                with zipfile.ZipFile(path) as book:
                    xml = "\n".join(book.read(n).decode("utf-8", errors="ignore")
                                    for n in book.namelist() if n.endswith(".xml"))
                if JWT_RE.search(xml) or PRIVATE_KEY_RE.search(xml):
                    problems.append(f"credential in spreadsheet: {rel}")
                # Shipped examples must be small. A renamed operational workbook
                # with thousands of rows must never pass as an example again.
                if len(re.findall(r"<row\b", xml)) > 100:
                    problems.append(f"non-minimal spreadsheet requires review: {rel}")
                if "WB-GI-" in xml and "WB-GI-DEMO" not in xml:
                    problems.append(f"supply identifiers in spreadsheet: {rel}")
            except (OSError, zipfile.BadZipFile):
                problems.append(f"unreadable spreadsheet: {rel}")
            continue
        if rel in ALLOWED_ENV:
            pass
        elif rel in FORBIDDEN_EXACT:
            problems.append(f"forbidden runtime file: {rel}")
            continue
        if any(part in rel_parts or rel.startswith(part + "/") for part in FORBIDDEN_PARTS):
            problems.append(f"forbidden runtime path: {rel}")
            continue
        low = path.name.lower()
        if low.startswith(".env") and rel not in ALLOWED_ENV:
            problems.append(f"environment file: {rel}")
            continue
        if any(low.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            problems.append(f"sensitive/runtime file type: {rel}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.suffix.lower() == ".json":
            try:
                if check_json(json.loads(text)):
                    problems.append(f"non-placeholder credential field: {rel}")
            except ValueError:
                problems.append(f"invalid JSON requires review: {rel}")
        if path.name.startswith(".env") and check_env(text):
            problems.append(f"non-placeholder environment credential: {rel}")
        if JWT_RE.search(text):
            problems.append(f"JWT-like token detected: {rel}")
        if PRIVATE_KEY_RE.search(text):
            problems.append(f"private key material detected: {rel}")
    # An ignored file can still be staged/tracked. Inspect index blob content too,
    # so a removed-from-worktree secret awaiting commit is not missed.
    if (root / ".git").exists():
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
        if result.returncode:
            problems.append("Git index could not be inspected")
        else:
            for raw in result.stdout.split(b"\0"):
                if not raw:
                    continue
                rel = raw.decode("utf-8", errors="replace")
                entry = Path(rel)
                if (rel not in ALLOWED_ENV and (entry.name.startswith(".env") or
                    entry.name in {"config.json", "connections.json", "credentials.json"})) or any(
                    entry.name.lower().endswith(suffix) for suffix in FORBIDDEN_SUFFIXES
                ) or (rel.startswith("data/") and rel not in SAFE_DATA_FILES and entry.name != ".gitkeep"):
                    problems.append(f"runtime/sensitive file tracked in Git: {rel}")
                blob = subprocess.run(["git", "-C", str(root), "show", ":" + rel], capture_output=True)
                text = blob.stdout.decode("utf-8", errors="ignore")
                if blob.returncode:
                    problems.append(f"Git index blob unavailable: {rel}")
                elif JWT_RE.search(text) or PRIVATE_KEY_RE.search(text):
                    problems.append(f"secret material in Git index: {rel}")
                elif entry.name.startswith(".env") and check_env(text):
                    problems.append(f"environment credential in Git index: {rel}")
                elif entry.suffix == ".json":
                    try:
                        if check_json(json.loads(text)):
                            problems.append(f"credential field in Git index: {rel}")
                    except ValueError:
                        problems.append(f"invalid JSON in Git index: {rel}")
    return sorted(set(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if a Git candidate contains FBE runtime secrets/data.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    problems = scan(root)
    if problems:
        print(f"[FBE] Git safety check FAILED: {len(problems)} problem(s).", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        print("[FBE] Secret values are intentionally not printed.", file=sys.stderr)
        return 2
    print("[FBE] Git safety check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
