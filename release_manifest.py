"""Release integrity: SHA-256 of every shipped file.
    python release_manifest.py generate     # writes RELEASE_MANIFEST.sha256
    python release_manifest.py verify       # exit code 1 if any file was changed, added or removed
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "RELEASE_MANIFEST.sha256"
SKIP_DIRS = {"__pycache__", "data", ".git", ".venv", "results", ".pytest_cache"}
SKIP_FILES = {NAME, ".env", "requirements.lock"}


def files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and p.name not in SKIP_FILES:
            yield p


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


ALLOWED_DATA = {"data/.gitkeep"}


def stray_data():
    d = ROOT / "data"
    if not d.exists():
        return []
    return sorted(p.relative_to(ROOT).as_posix() for p in d.rglob("*") if p.is_file() and p.relative_to(ROOT).as_posix() not in ALLOWED_DATA)


def generate():
    stray = stray_data()
    if stray:
        raise SystemExit("refusing to build a release with runtime/test data inside data/: " + ", ".join(stray))
    lines = [f"{digest(p)}  {p.relative_to(ROOT).as_posix()}" for p in files()]
    (ROOT / NAME).write_text("\n".join(lines) + "\n")
    return len(lines)


def verify():
    want = {}
    for line in (ROOT / NAME).read_text().splitlines():
        h, name = line.split("  ", 1)
        want[name] = h
    have = {p.relative_to(ROOT).as_posix(): digest(p) for p in files()}
    changed = sorted(k for k in want if k in have and have[k] != want[k])
    missing = sorted(set(want) - set(have))
    added = sorted(set(have) - set(want)) + [f"(data) {x}" for x in stray_data()]
    return changed, missing, added


if __name__ == "__main__":
    if sys.argv[1:] == ["generate"]:
        print(f"{generate()} files hashed into {NAME}")
    else:
        c, m, a = verify()
        for label, xs in (("CHANGED", c), ("MISSING", m), ("ADDED", a)):
            for x in xs:
                print(label, x)
        print("OK: release matches its manifest" if not (c or m or a) else "FAILED: release does not match its manifest")
        sys.exit(1 if (c or m or a) else 0)
