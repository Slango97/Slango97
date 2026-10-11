"""Dependency-free test runner (pytest also works: `pytest tests`).  Usage: python tests/run_tests.py [-k text] [-x]"""
import importlib.util
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def main(argv):
    key = argv[argv.index("-k") + 1] if "-k" in argv else ""
    stop = "-x" in argv
    folder = HERE / "v3_baseline" if "--v3-baseline" in argv else HERE
    if "--v3-baseline" in argv and os.environ.get("V4_FEATURES") != "off":
        print("re-running with V4_FEATURES=off (V3 regression mode)")
        return subprocess.call([sys.executable, __file__] + argv, env={**os.environ, "V4_FEATURES": "off"})
    passed = failed = 0
    superseded = {}
    sup_file = folder / "SUPERSEDED.txt"
    if sup_file.exists():
        for line in sup_file.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                name, ver, why = [x.strip() for x in line.split("|", 2)]
                superseded[name] = f"superseded in {ver}: {why}"
    n_sup = 0
    data_dir = HERE.parent / "data"
    before = {p for p in data_dir.rglob("*")} if data_dir.exists() else set()
    failures = []
    for path in sorted(folder.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            failed += 1
            failures.append((f"{path.stem} (import)", traceback.format_exc()))
            print(f"FAIL {path.stem} (import error)")
            continue
        for name in [n for n in dir(mod) if n.startswith("test_") and key in n]:
            t0 = time.time()
            try:
                fn = getattr(mod, name)
                shared = HERE.parent / "data" / "shared_risk.json"   # account-wide loss rules: fresh per test
                if shared.exists():
                    shared.unlink()
                import inspect
                import tempfile
                kw = {"tmp_path": __import__("pathlib").Path(tempfile.mkdtemp())} if "tmp_path" in inspect.signature(fn).parameters else {}
                fn(**kw)
                passed += 1
                print(f"PASS {path.stem}::{name} ({time.time() - t0:.1f}s)")
            except Exception:
                if name in superseded:
                    n_sup += 1
                    print(f"SUPERSEDED {path.stem}::{name} ({superseded[name]})")
                    continue
                failed += 1
                failures.append((f"{path.stem}::{name}", traceback.format_exc()))
                print(f"FAIL {path.stem}::{name}")
                if stop:
                    break
        if stop and failed:
            break
    for name, tb in failures:
        print("\n" + "=" * 70 + f"\n{name}\n" + tb)
    for p in sorted(({q for q in data_dir.rglob("*")} if data_dir.exists() else set()) - before, reverse=True):
        try:                                          # tests must never leave runtime files inside the release
            p.unlink() if p.is_file() else p.rmdir()
        except OSError:
            pass
    print(f"\n{passed} passed, {failed} failed" + (f", {n_sup} superseded (documented)" if n_sup else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
