from __future__ import annotations

from datetime import date

import requests

from bi_kpi.extract import fx


def test_api_rates_are_cached_then_reused(settings, monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rates": {"2017-03-01": {"MXN": 6.3, "USD": 0.32}}}

    monkeypatch.setattr(fx.requests, "get", lambda *a, **k: Resp())
    live = fx.get_rates(settings.fx, date(2017, 3, 1), date(2017, 3, 2))
    assert set(live["fx_source"]) == {"api"}
    assert len(live) == 2

    def down(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(fx.requests, "get", down)
    cached = fx.get_rates(settings.fx, date(2017, 3, 1), date(2017, 3, 2))
    assert set(cached["fx_source"]) == {"cache"}
    assert cached.set_index("currency")["rate"].to_dict() == {"MXN": 6.3, "USD": 0.32}


def test_fallback_when_offline_without_cache(settings):
    rates = fx.get_rates(settings.fx, date(2017, 3, 1), date(2017, 3, 2), offline=True)
    assert set(rates["fx_source"]) == {"fallback"}
    assert rates.set_index("currency")["rate"].to_dict() == settings.fx.fallback_rates
