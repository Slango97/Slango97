from types import SimpleNamespace as NS

from symbol_resolver import rank_gold_symbols, score_gold_symbol


def info(name, base="", profit="", trade_mode=4, path="", description="", tick=True, volume=True):
    return NS(name=name, currency_base=base, currency_profit=profit, trade_mode=trade_mode,
              path=path, description=description, trade_tick_size=0.01 if tick else 0.0,
              trade_tick_value=0.1 if tick else 0.0, volume_min=0.01 if volume else 0.0,
              volume_step=0.01 if volume else 0.0)


def test_suffix_symbol_beats_unrelated_symbols():
    ranked = rank_gold_symbols([
        info("XAUUSDs", "XAU", "USD"),
        info("GOLDmicro", "XAU", "USD"),
        info("USOIL", "USD", "USD"),
    ])
    assert ranked[0].name == "XAUUSDs"
    assert any("preferred symbol with broker suffix/prefix" in r for r in ranked[0].reasons)


def test_exact_preferred_symbol_wins():
    exact = info("XAUUSD", "XAU", "USD")
    suffixed = info("XAUUSDs", "XAU", "USD")
    s1, _ = score_gold_symbol(exact)
    s2, _ = score_gold_symbol(suffixed)
    assert s1 > s2


def test_inconsistent_gold_name_is_penalized():
    bad = info("GOLDXYZ", "EUR", "GBP", path="Commodities\\Other")
    good = info("XAUUSDs", "XAU", "USD")
    ranked = rank_gold_symbols([bad, good])
    assert ranked[0].name == "XAUUSDs"
