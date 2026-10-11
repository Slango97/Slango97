from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
from helpers import Config
from news import NewsEngine
from intel import make_chain
CFG = Config()

def test_fresh_calendar_sets_timestamp_and_is_eligible():
    cfg = replace(CFG, features=replace(CFG.features, news_redundancy=True))
    ev = {"title":"CPI m/m","country":"USD","impact":"High","date":datetime.now(timezone.utc).isoformat(),"forecast":"0.2%","previous":"0.1%"}
    n = NewsEngine(cfg, http_get=lambda url: json.dumps([ev]).encode(), llm=None)
    n._load_calendar(); c=n.get()
    assert n.calendar_state == 'FRESH' and n.calendar_cached_at > 0 and c.calendar_ok

def test_unavailable_calendar_fails_closed():
    cfg = replace(CFG, features=replace(CFG.features, news_redundancy=True))
    n = NewsEngine(cfg, http_get=lambda url: (_ for _ in ()).throw(RuntimeError('down')), llm=None)
    # Isolate from any developer-machine cache: this test must exercise true unavailability.
    n.nc = replace(n.nc, calendar_cache_file=str(Path('data') / 'definitely_missing_v44_cache.json'))
    try: n._load_calendar()
    except RuntimeError: pass
    c=n.get(); assert c.calendar_state in ('UNAVAILABLE','STALE') and not c.calendar_ok

def test_llm_is_opt_in():
    ic = replace(CFG.intel, provider='openai')
    assert make_chain(ic) is None
