"""Daily stock price checks via Yahoo Finance chart API (free, no API key)."""
from __future__ import annotations

from dataclasses import dataclass

import requests

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass
class Quote:
    ticker: str
    date: str
    price: float
    change_pct: float


def get_quote(ticker: str) -> Quote | None:
    url = YAHOO_URL.format(symbol=ticker.upper())
    resp = requests.get(
        url,
        params={"range": "5d", "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        return None
    meta = results[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    pct = meta.get("regularMarketChangePercent")
    if pct is None:
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        pct = ((price - prev) / prev * 100.0) if prev else 0.0
    ts = meta.get("regularMarketTime")
    date = ""
    if ts:
        import datetime as _dt

        date = _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    return Quote(ticker=ticker.upper(), date=date, price=float(price), change_pct=float(pct))
