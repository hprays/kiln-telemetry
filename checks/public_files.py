"""Inspect the Git index and reachable commit objects without printing secret values."""
import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private-key": rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----",
    "github-token": rb"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})",
    "cloud-key-id": rb"(?:AKIA|ASIA)[A-Z0-9]{16}",
    "bearer-value": rb"(?i)Bearer\s+[a-z0-9_./+=-]{24,}",
    "embedded-credential": rb"https?://[^\s/@:]+:[^\s/@]+@",
    "hardcoded-secret": rb"(?im)^\s*(?:api_key|password|token|secret)\s*[:=]\s*[\"'][a-z0-9_./+=-]{20,}[\"']",
    "host-home-path": rb"(?i)[A-Z]:[\\/]Users[\\/][A-Za-z0-9_.-]+[\\/]",
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def forbidden(name):
    path = PurePosixPath(name.lower())
    tail = path.name
    return (any(part in {".terraform", ".kiln-local", ".runs", "__pycache__", "node_modules"}
                for part in path.parts)
            or tail.startswith((".env", "kubeconfig")) or ".tfstate" in tail
            or tail.endswith((".tfvars", ".tfvars.json", ".tfplan", ".plan", ".db",
                              ".sqlite", ".sqlite3", ".pem", ".key", ".token", ".pfx", ".exe"))
            or tail == "plan.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--local-token", type=Path, help="Also check for the exact local producer value")
    args = parser.parse_args()
    exact = args.local_token.read_bytes().strip() if args.local_token else b""
    paths = {}
    for record in git("ls-files", "-s", "-z").split(b"\0"):
        if record:
            meta, name = record.split(b"\t", 1)
            paths.setdefault(meta.split()[1].decode(), set()).add(name.decode())
    if args.history:
        commits = git("rev-list", "--all").decode().splitlines()
        for commit in commits:
            for row in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                if row:
                    meta, name = row.split(b"\t", 1)
                    kind, oid = meta.split()[1:]
                    if kind == b"blob":
                        paths.setdefault(oid.decode(), set()).add(name.decode())
    violations = []
    for oid, names in paths.items():
        payload = git("cat-file", "blob", oid)
        for name in names:
            if forbidden(name):
                violations.append((name, "excluded-file"))
            if exact and exact in payload:
                violations.append((name, "local-producer-value"))
            if b"\0" not in payload:
                for label, pattern in RULES.items():
                    if re.search(pattern, payload):
                        violations.append((name, label))
    for name, label in violations:
        print(name, label)
    print(f"blobs={len(paths)} findings={len(violations)} history={args.history}")
    return bool(violations)


if __name__ == "__main__":
    raise SystemExit(main())
