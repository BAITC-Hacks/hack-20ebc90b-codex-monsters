"""Export only committed runtime files into an empty deployment directory.

Example: python scripts/prepare_deploy.py --output /tmp/ekt-deploy --ref HEAD
The resulting directory can be uploaded by a hosting CLI without uploading the
repository's history, corporate archives, local state, or uncommitted changes.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = frozenset({
    "Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock",
    "apps/buyer_ui/app.py", "apps/buyer_ui/styles.css",
    "assets/demo/ui-fixtures/bundle.json", ".streamlit/config.toml",
    "scripts/run_app.py", "scripts/check_health.py", "scripts/smoke_api.py",
})


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(part in {"..", ".git"} for part in parts):
        return False
    return (
        path in FIXED_FILES
        or (len(parts) >= 2 and parts[0] == "src" and path.endswith(".py"))
        or (len(parts) == 3 and parts[:2] == ("apps", "buyer_ui") and path.endswith(".py"))
    )


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git {args[0]} failed")
    return result.stdout


def prepare_deploy(output: Path, ref: str = "HEAD", *, repo: Path = ROOT) -> tuple[str, int, int]:
    """Return commit SHA, committed-file count and byte count (excluding marker)."""
    output = output.absolute()
    if output.is_symlink():
        raise RuntimeError("Output must not be a symbolic link")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError("Output must be a new or empty directory; nothing was overwritten")

    commit = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    commit = commit.decode("ascii").strip()
    entries = git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")
    selected = []
    for entry in entries:
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        path = os.fsdecode(name)
        if not allowed(path):
            continue
        mode, kind, object_id = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"Allowed runtime path must be a regular file: {path}")
        selected.append((path, mode, object_id))

    missing = FIXED_FILES.difference(path for path, _, _ in selected)
    if missing:
        raise RuntimeError("Commit is missing runtime files: " + ", ".join(sorted(missing)))

    # Resolve every blob before writing, so Git errors cannot leave half an export.
    files = [(path, mode, git(repo, "cat-file", "blob", oid)) for path, mode, oid in selected]
    output.mkdir(parents=True, exist_ok=True)
    for path, mode, content in files:
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        destination.chmod(0o755 if mode == "100755" else 0o644)
    with (output / "DEPLOY_COMMIT").open("x", encoding="ascii") as stream:
        stream.write(commit + "\n")
    return commit, len(files), sum(len(content) for _, _, content in files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new or empty deploy directory")
    parser.add_argument("--ref", default="HEAD", help="committed Git ref (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        commit, count, size = prepare_deploy(args.output, args.ref)
    except (OSError, RuntimeError) as exc:
        print(f"Cannot prepare deployment: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared {count} committed files ({size} bytes) in {args.output.absolute()}")
    print(f"Commit: {commit} (recorded in DEPLOY_COMMIT)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
