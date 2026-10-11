import sys
from pathlib import Path

TESTS_DIR = str(Path(__file__).resolve().parent)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)



import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fresh_shared_risk_state():
    """The two-loss lock / cooldown store is account-wide by design; every test starts without one."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "shared_risk.json"
    if p.exists():
        p.unlink()
    yield
