"""Build a local website from the tracked funds' 13F data.

Produces two datasets:
  * Latest actions  -- each fund's buys/sells vs. its previous 13F filing.
  * Common holdings -- stocks held by two or more of the tracked funds.

Current holdings are read from the state saved by `hedge_monitor holdings`.
Each fund's previous 13F filing is fetched from SEC EDGAR to compute the diff.
Output: site/data.json and site/index.html (open with `--serve` to view).

Usage (from the repo root):
    python -m hedge_monitor holdings          # refresh current holdings first
    python -m tools.build_site --serve        # build site and serve locally
"""
from __future__ import annotations

import argparse
import http.server
import json
import re
import socketserver
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import requests

from hedge_monitor.config import load_config
from hedge_monitor.edgar import EdgarClient
from hedge_monitor import news as news_mod
from hedge_monitor import prices as prices_mod
from hedge_monitor.storage import Storage
from tools.countries import (
    COUNTRIES, COUNTRY_ORDER, international_managers, country_stocks, country_pundits,
)

SITE_DIR = Path("site")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Tokens dropped when normalizing company names for name->ticker matching.
_NAME_NOISE = {
    "INC", "CORP", "CORPORATION", "CO", "COM", "COMPANY", "LTD", "LIMITED", "PLC",
    "LLC", "LP", "THE", "HLDGS", "HOLDINGS", "HOLDING", "GROUP", "GRP", "NEW",
    "SA", "NV", "AG", "TRUST", "FUND", "ADR", "CLASS", "CL", "SP", "SPON",
}


def _norm_name(name: str) -> str:
    tokens = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split()
    kept = [t for t in tokens if t not in _NAME_NOISE and len(t) > 1]
    return " ".join(kept)


def _to_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def compute_actions(current: list[dict], prev: list[dict]) -> list[dict]:
    """Diff two holdings snapshots into buy/sell actions keyed by CUSIP."""
    cur_by = {h.get("cusip"): h for h in current if h.get("cusip")}
    prev_by = {h.get("cusip"): h for h in prev if h.get("cusip")}
    actions: list[dict] = []

    for cusip, h in cur_by.items():
        shares = _to_int(h.get("shares"))
        value = _to_int(h.get("value"))
        name = h.get("name", "?")
        if cusip not in prev_by:
            actions.append(
                {"action": "BUY", "name": name, "cusip": cusip,
                 "shares_delta": shares, "value": value}
            )
        else:
            prev_sh = _to_int(prev_by[cusip].get("shares"))
            if shares > prev_sh:
                actions.append(
                    {"action": "ADD", "name": name, "cusip": cusip,
                     "shares_delta": shares - prev_sh, "value": value}
                )
            elif shares < prev_sh:
                actions.append(
                    {"action": "TRIM", "name": name, "cusip": cusip,
                     "shares_delta": shares - prev_sh, "value": value}
                )

    for cusip, h in prev_by.items():
        if cusip not in cur_by:
            actions.append(
                {"action": "SELL", "name": h.get("name", "?"), "cusip": cusip,
                 "shares_delta": -_to_int(h.get("shares")), "value": 0}
            )
    return actions


def top_actions(actions: list[dict], per_side: int) -> list[dict]:
    """Keep the largest buys and largest sells to keep the table readable."""
    buys = [a for a in actions if a["action"] in ("BUY", "ADD")]
    sells = [a for a in actions if a["action"] in ("SELL", "TRIM")]
    buys.sort(key=lambda a: (a["value"], abs(a["shares_delta"])), reverse=True)
    sells.sort(key=lambda a: abs(a["shares_delta"]), reverse=True)
    return buys[:per_side] + sells[:per_side]


def _news_query(fund_name: str) -> str:
    base = fund_name.split("(")[0].strip()
    person = ""
    if "(" in fund_name and ")" in fund_name:
        person = fund_name[fund_name.find("(") + 1 : fund_name.find(")")].strip()
    return f"{base} {person}".strip()


def _google_news(query: str, days: int, limit: int, site: str | None = None) -> list[dict]:
    """Fetch parsed, date-stamped news items from Google News RSS (public)."""
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime

    qy = f"{query} site:{site}" if site else query
    q = quote(f"{qy} when:{days}d")
    try:
        items = news_mod.fetch_feed(GOOGLE_NEWS_RSS.format(q=q))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for it in items[:limit]:
        title, source = it.title, it.source
        if " - " in title:  # Google News encodes publisher as "Headline - Publisher"
            title, source = title.rsplit(" - ", 1)
        ts, published = 0.0, it.published
        try:
            dt = parsedate_to_datetime(it.published)
            if dt is not None:
                ts = dt.timestamp()
                published = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
        out.append({"title": title, "link": it.link, "source": source,
                    "published": published, "ts": ts})
    return out


def fetch_fund_news(fund_name: str, days: int, limit: int, site: str | None = None) -> list[dict]:
    """Latest news about a fund from Google News RSS (public, no API key).

    Pass site='bloomberg.com' to restrict results to a single publisher.
    """
    return _google_news(_news_query(fund_name), days, limit, site)


_BUY_WORDS = (
    "upgrade", "upgraded", "buy rating", "raises price target", "raised price target",
    "hikes price target", "outperform", "overweight", "bullish", "initiates buy",
    "initiated buy", "top pick", "strong buy", "reiterates buy", "raised to buy",
    "upgrades to buy",
)
_SELL_WORDS = (
    "downgrade", "downgraded", "sell rating", "cuts price target", "cut price target",
    "lowers price target", "lowered price target", "underperform", "underweight",
    "bearish", "initiates sell", "reiterates sell", "strong sell", "cut to sell",
    "downgrades to sell", "slashes price target",
)


def classify_signal(title: str) -> str:
    t = title.lower()
    buy = any(w in t for w in _BUY_WORDS)
    sell = any(w in t for w in _SELL_WORDS)
    if buy and not sell:
        return "BUY"
    if sell and not buy:
        return "SELL"
    return ""


# Well-known, heavily-traded stocks to scan for analyst buy/sell signals.
FAMOUS_STOCKS = [
    ("NVDA", "Nvidia"), ("TSLA", "Tesla"), ("AAPL", "Apple"), ("AMZN", "Amazon"),
    ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"), ("META", "Meta Platforms"),
    ("AMD", "AMD"), ("NFLX", "Netflix"), ("PLTR", "Palantir"), ("COIN", "Coinbase"),
    ("MSTR", "MicroStrategy"), ("AVGO", "Broadcom"), ("INTC", "Intel"),
    ("MU", "Micron"), ("SMCI", "Super Micro Computer"), ("BABA", "Alibaba"),
    ("TSM", "Taiwan Semiconductor"), ("UBER", "Uber"), ("DIS", "Disney"),
]

# Famous market analysts / pundits whose latest commentary to surface.
ANALYSTS = [
    "Jim Cramer", "Dan Ives", "Tom Lee", "Cathie Wood", "Gene Munster", "Mad Money",
]


INDICES = [
    ("S&P 500", "^GSPC"), ("Dow Jones", "^DJI"), ("Nasdaq", "^IXIC"),
    ("Russell 2000", "^RUT"), ("VIX", "^VIX"), ("FTSE 100", "^FTSE"),
    ("DAX", "^GDAXI"), ("CAC 40", "^FCHI"), ("Euro Stoxx 50", "^STOXX50E"),
    ("Nikkei 225", "^N225"), ("Hang Seng", "^HSI"), ("Shanghai", "000001.SS"),
    ("Sensex", "^BSESN"), ("Nifty 50", "^NSEI"), ("OMX Stockholm 30", "^OMX"),
    ("Oslo OBX", "OBX.OL"),
]
CURRENCIES = [
    ("EUR/USD", "EURUSD=X"), ("GBP/USD", "GBPUSD=X"), ("USD/JPY", "USDJPY=X"),
    ("USD/INR", "USDINR=X"), ("USD/CNY", "USDCNY=X"), ("USD/SEK", "USDSEK=X"),
    ("USD/NOK", "USDNOK=X"), ("USD/DKK", "USDDKK=X"), ("AUD/USD", "AUDUSD=X"),
    ("USD/CAD", "USDCAD=X"), ("USD/CHF", "USDCHF=X"), ("Dollar Index", "DX-Y.NYB"),
]
CRYPTO = [
    ("Bitcoin", "BTC-USD"), ("Ethereum", "ETH-USD"), ("BNB", "BNB-USD"),
    ("Solana", "SOL-USD"), ("XRP", "XRP-USD"), ("Cardano", "ADA-USD"),
    ("Dogecoin", "DOGE-USD"), ("Tron", "TRX-USD"), ("Avalanche", "AVAX-USD"),
    ("Chainlink", "LINK-USD"),
]


def fetch_markets(max_active: int = 25) -> dict:
    """Live indices, currencies, crypto (Yahoo chart) + most-active stocks (screener)."""
    def rows(pairs: list[tuple[str, str]]) -> list[dict]:
        out: list[dict] = []
        for name, sym in pairs:
            try:
                q = prices_mod.get_quote(sym)
            except Exception:  # noqa: BLE001
                q = None
            if q:
                out.append({"name": name, "symbol": sym,
                            "price": round(q.price, 4), "change_pct": round(q.change_pct, 2)})
            time.sleep(0.05)
        return out

    markets = {"indices": rows(INDICES), "currencies": rows(CURRENCIES),
               "crypto": rows(CRYPTO), "most_active": []}
    try:
        url = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/"
               f"saved?count={max_active}&scrIds=most_actives")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200:
            for x in r.json()["finance"]["result"][0]["quotes"]:
                markets["most_active"].append({
                    "symbol": x.get("symbol"),
                    "name": x.get("shortName") or x.get("longName") or x.get("symbol"),
                    "price": x.get("regularMarketPrice"),
                    "change_pct": round(x.get("regularMarketChangePercent", 0) or 0, 2),
                    "volume": x.get("regularMarketVolume"),
                })
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] most-active screener: {exc}")
    return markets


def load_ticker_map(client: EdgarClient) -> dict[str, str]:
    """Map normalized company name -> ticker using SEC's official ticker list."""
    try:
        data = client._get(SEC_TICKERS_URL).json()
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] could not load SEC ticker map: {exc}")
        return {}
    out: dict[str, str] = {}
    for row in data.values():
        key = _norm_name(row.get("title", ""))
        ticker = str(row.get("ticker", "")).strip().upper()
        if key and ticker and key not in out:
            out[key] = ticker
    return out


def enrich_with_prices(data: dict, ticker_map: dict[str, str], max_quotes: int) -> None:
    """Attach ticker + live Yahoo price and today's % move to holdings/actions."""
    def ticker_for(name: str) -> str:
        return ticker_map.get(_norm_name(name), "")

    for row in data["common"]:
        row["ticker"] = ticker_for(row["name"])
    for a in data["actions"]:
        a["ticker"] = ticker_for(a["name"])

    wanted: list[str] = []
    seen: set[str] = set()
    for row in data["common"] + data["actions"]:
        t = row.get("ticker")
        if t and t not in seen:
            seen.add(t)
            wanted.append(t)
        if len(wanted) >= max_quotes:
            break

    quotes: dict[str, dict] = {}
    print(f"Fetching live Yahoo quotes for {len(wanted)} tickers...")
    for t in wanted:
        try:
            q = prices_mod.get_quote(t)
        except Exception:  # noqa: BLE001
            q = None
        if q:
            quotes[t] = {"price": round(q.price, 2), "day_pct": round(q.change_pct, 2)}
        time.sleep(0.05)

    for row in data["common"] + data["actions"]:
        q = quotes.get(row.get("ticker", ""))
        row["price"] = q["price"] if q else None
        row["day_pct"] = q["day_pct"] if q else None

    data["priced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data["quotes_count"] = len(quotes)


def build(config_path: str, data_dir: str, per_side: int, max_quotes: int = 300,
          news_days: int = 30, news_per_fund: int = 8, with_news: bool = True) -> dict:
    cfg = load_config(config_path)
    store = Storage(Path(data_dir))
    client = EdgarClient(cfg.sec_user_agent)

    fund_summaries: list[dict] = []
    all_actions: list[dict] = []
    aggregate: dict[str, dict] = {}

    for fund in cfg.funds:
        name = fund.get("name", fund.get("cik", "?"))
        cik = str(fund.get("cik", "")).strip()
        if not cik:
            continue

        state = store.load(f"holdings_{cik}", default={}) or {}
        current = state.get("holdings", []) or []
        filing_date = state.get("filing_date", "")

        prev: list[dict] = []
        try:
            filings = client.recent_13f(cik, limit=2)
            if not current and filings:
                current = [
                    {"name": h.name, "cusip": h.cusip, "shares": h.shares, "value": h.value}
                    for h in client.holdings(filings[0])
                ]
                filing_date = filings[0].filing_date
            if len(filings) >= 2:
                prev = [
                    {"name": h.name, "cusip": h.cusip, "shares": h.shares, "value": h.value}
                    for h in client.holdings(filings[1])
                ]
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] {name}: {exc}")

        actions = compute_actions(current, prev)
        for a in actions:
            a["fund"] = name
            a["date"] = filing_date
            a["country"] = "US"
        n_buys = sum(1 for a in actions if a["action"] in ("BUY", "ADD"))
        n_sells = sum(1 for a in actions if a["action"] in ("SELL", "TRIM"))
        all_actions.extend(top_actions(actions, per_side))

        total_value = sum(_to_int(h.get("value")) for h in current)
        fund_summaries.append(
            {
                "name": name,
                "cik": cik,
                "country": "US",
                "filing_date": filing_date,
                "positions": len(current),
                "buys": n_buys,
                "sells": n_sells,
                "total_value": total_value,
            }
        )
        print(f"  {name}: {len(current)} positions, {n_buys} buys, {n_sells} sells")

        for h in current:
            cusip = h.get("cusip")
            if not cusip:
                continue
            entry = aggregate.setdefault(
                cusip,
                {"name": h.get("name", "?"), "cusip": cusip, "funds": [],
                 "total_value": 0, "total_shares": 0},
            )
            entry["funds"].append(name)
            entry["total_value"] += _to_int(h.get("value"))
            entry["total_shares"] += _to_int(h.get("shares"))

    common = [
        {
            "name": e["name"],
            "cusip": e["cusip"],
            "num_funds": len(e["funds"]),
            "funds": sorted(set(e["funds"])),
            "total_value": e["total_value"],
            "total_shares": e["total_shares"],
        }
        for e in aggregate.values()
        if len(set(e["funds"])) >= 2
    ]
    common.sort(key=lambda e: (e["num_funds"], e["total_value"]), reverse=True)

    all_actions.sort(key=lambda a: (a.get("date", ""), a.get("value", 0)), reverse=True)

    funds_sorted = sorted(fund_summaries, key=lambda f: f["total_value"], reverse=True)

    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "funds": funds_sorted,
        "actions": all_actions,
        "common": common,
    }
    ensure_international_and_countries(data)

    ticker_map = load_ticker_map(client)
    enrich_with_prices(data, ticker_map, max_quotes)
    print("Fetching markets (indices, FX, crypto, most active)...")
    data["markets"] = fetch_markets()
    collect_news(data, news_days, news_per_fund, with_news)
    return data


def collect_news(data: dict, news_days: int, news_per_fund: int, with_news: bool = True) -> None:
    """Populate the fast-moving news datasets on an existing data dict."""
    if not with_news:
        for f in data["funds"]:
            f["news"] = []
        data["news_feed"] = []
        data["news_count"] = 0
        data["news_feed_count"] = 0
        data["stock_signals"] = []
        data["analysts"] = []
        return

    print(f"Fetching latest news (incl. Bloomberg) for {len(data['funds'])} funds...")
    feed: dict[str, dict] = {}
    for f in data["funds"]:
        country = f.get("country", "US")
        general = fetch_fund_news(f["name"], news_days, news_per_fund)
        time.sleep(0.2)
        if f.get("cik"):  # Bloomberg pass only for US 13F filers
            bloom = fetch_fund_news(f["name"], news_days, news_per_fund, site="bloomberg.com")
            time.sleep(0.2)
        else:
            bloom = []
        links = {g["link"] for g in general}
        f["news"] = general + [b for b in bloom if b["link"] not in links]
        f["news"].sort(key=lambda x: x.get("ts", 0), reverse=True)
        for item in f["news"]:
            key = item["link"]
            if key in feed:
                if f["name"] not in feed[key]["funds"]:
                    feed[key]["funds"].append(f["name"])
            else:
                feed[key] = {**item, "funds": [f["name"]], "country": country}
    news_feed = sorted(feed.values(), key=lambda x: x.get("ts", 0), reverse=True)
    data["news_feed"] = news_feed
    data["news_count"] = sum(len(f.get("news", [])) for f in data["funds"])
    data["news_feed_count"] = len(news_feed)
    print(f"  collected {data['news_count']} items ({len(news_feed)} unique articles).")

    print(f"Scanning news buy/sell signals across {len(COUNTRY_ORDER)} countries...")
    signals: list[dict] = []
    for code in COUNTRY_ORDER:
        for tk, company in country_stocks(code):
            q = f'{company} stock (upgrade OR downgrade OR "price target" OR "buy rating" OR "sell rating")'
            for it in _google_news(q, news_days, 10):
                sig = classify_signal(it["title"])
                if sig:
                    signals.append({**it, "ticker": tk, "company": company,
                                    "signal": sig, "country": code})
            time.sleep(0.2)
    signals.sort(key=lambda x: x.get("ts", 0), reverse=True)
    data["stock_signals"] = signals

    print("Fetching analyst commentary across countries...")
    analysts: list[dict] = []
    for code in COUNTRY_ORDER:
        for person in country_pundits(code):
            for it in _google_news(f"{person} stocks", news_days, 6):
                analysts.append({**it, "analyst": person, "country": code})
            time.sleep(0.2)
    analysts.sort(key=lambda x: x.get("ts", 0), reverse=True)
    data["analysts"] = analysts
    print(f"  {len(signals)} signals, {len(analysts)} analyst items.")


def ensure_international_and_countries(data: dict) -> None:
    """Append news-only managers from non-US countries and set country metadata.

    Idempotent: skips managers already present. US funds keep country='US'.
    """
    have = {f["name"] for f in data["funds"]}
    for mgr, code in international_managers():
        if mgr not in have:
            data["funds"].append({
                "name": mgr, "cik": "", "country": code, "filing_date": "",
                "positions": 0, "buys": 0, "sells": 0, "total_value": 0,
            })
    for f in data["funds"]:
        f.setdefault("country", "US")
    data["funds"].sort(key=lambda f: f.get("total_value", 0), reverse=True)
    for i, f in enumerate(data["funds"], 1):
        f["rank"] = i
    data["countries"] = [
        {"code": c, "name": COUNTRIES[c]["name"], "flag": COUNTRIES[c]["flag"]}
        for c in COUNTRY_ORDER
    ]


def refresh_dynamic(config_path: str, max_quotes: int, news_days: int,
                    news_per_fund: int, with_news: bool = True) -> dict:
    """Update only fast-moving data (prices + news/signals) on existing site data."""
    cfg = load_config(config_path)
    client = EdgarClient(cfg.sec_user_agent)
    path = SITE_DIR / "data.json"
    if not path.exists():
        raise FileNotFoundError("No site/data.json yet; run a full build first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ensure_international_and_countries(data)
    ticker_map = load_ticker_map(client)
    enrich_with_prices(data, ticker_map, max_quotes)
    print("Fetching markets (indices, FX, crypto, most active)...")
    data["markets"] = fetch_markets()
    collect_news(data, news_days, news_per_fund, with_news)
    return data


def write_site(data: dict) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "data.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )
    (SITE_DIR / "index.html").write_text(INDEX_HTML, encoding="utf-8")


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def serve(port: int, host: str = "127.0.0.1") -> None:
    handler = http.server.SimpleHTTPRequestHandler

    class Handler(handler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(SITE_DIR), **k)

        def log_message(self, *a):  # quiet
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), Handler) as httpd:
        local = f"http://127.0.0.1:{port}/"
        print(f"\nServing site at {local}  (Ctrl+C to stop)")
        if host not in ("127.0.0.1", "localhost"):
            print(f"On your phone (same Wi-Fi): http://{_lan_ip()}:{port}/")
        try:
            webbrowser.open(local)
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.yml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--per-side", type=int, default=15,
                        help="Max buys and max sells kept per fund.")
    parser.add_argument("--max-quotes", type=int, default=300,
                        help="Max unique tickers to price live from Yahoo.")
    parser.add_argument("--news-days", type=int, default=30,
                        help="Look back window (days) for fund news.")
    parser.add_argument("--news-per-fund", type=int, default=8,
                        help="Max news items kept per fund.")
    parser.add_argument("--no-news", action="store_true", help="Skip fetching fund news.")
    parser.add_argument("--serve", action="store_true", help="Serve the site after building.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address. Use 0.0.0.0 to allow phones on your Wi-Fi.")
    parser.add_argument("--lan", action="store_true",
                        help="Shortcut for --serve --host 0.0.0.0 (mobile access on same Wi-Fi).")
    parser.add_argument("--refresh-every", type=int, default=0, metavar="MIN",
                        help="Serve and auto-refresh prices+news every MIN minutes.")
    parser.add_argument("--refresh-only", action="store_true",
                        help="Only refresh prices+news on existing site/data.json (fast; no 13F).")
    args = parser.parse_args(argv)

    host = "0.0.0.0" if args.lan else args.host
    serve_it = args.serve or args.lan

    if args.refresh_only:
        print("Refreshing prices + news on existing data...")
        data = refresh_dynamic(args.config, args.max_quotes, args.news_days,
                               args.news_per_fund, not args.no_news)
        write_site(data)
        print(f"Updated {SITE_DIR/'data.json'} ({data['generated']}).")
        if serve_it:
            serve(args.port, host)
        return 0

    print("Building site data...")
    start = time.monotonic()
    data = build(args.config, args.data_dir, args.per_side, args.max_quotes,
                 args.news_days, args.news_per_fund, not args.no_news)
    write_site(data)
    print(
        f"\nWrote {SITE_DIR/'index.html'} and {SITE_DIR/'data.json'} "
        f"({len(data['actions'])} actions, {len(data['common'])} common holdings) "
        f"in {time.monotonic()-start:.0f}s."
    )

    if args.refresh_every and args.refresh_every > 0:
        import threading
        threading.Thread(target=serve, args=(args.port, host), daemon=True).start()
        print(f"Auto-refreshing prices + news every {args.refresh_every} min "
              f"(13F actions are quarterly and left as-is). Ctrl+C to stop.")
        try:
            while True:
                time.sleep(args.refresh_every * 60)
                print("Refreshing prices + news...")
                try:
                    data = refresh_dynamic(args.config, args.max_quotes, args.news_days,
                                           args.news_per_fund, not args.no_news)
                    write_site(data)
                    print(f"  updated {data['generated']}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  refresh failed: {exc}")
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    if serve_it:
        serve(args.port, host)
    return 0


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Monitor — Fund Activity</title>
<style>
  :root { --bg:#0f1420; --card:#171e2e; --line:#26304a; --fg:#e6ebf5; --mut:#93a0bd;
          --buy:#2ec26b; --sell:#ef5b6b; --accent:#5b9dff; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:20px 24px; border-bottom:1px solid var(--line); }
  h1 { margin:0; font-size:20px; }
  .sub { color:var(--mut); font-size:13px; margin-top:4px; }
  .wrap { padding:20px 24px; }
  .cards { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:12px 16px; min-width:130px; }
  .card .n { font-size:22px; font-weight:700; }
  .card .l { color:var(--mut); font-size:12px; }
  .tabs { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
  .tab { background:var(--card); border:1px solid var(--line); color:var(--fg);
         padding:8px 14px; border-radius:8px; cursor:pointer; }
  .tab.active { background:var(--accent); border-color:var(--accent); color:#04122c; font-weight:600; }
  .btn { background:var(--card); border:1px solid var(--line); color:var(--fg);
         padding:8px 14px; border-radius:8px; cursor:pointer; }
  .btn.on { background:var(--accent); border-color:var(--accent); color:#04122c; font-weight:600; }
  .controls { display:flex; gap:10px; margin-bottom:10px; flex-wrap:wrap; align-items:center; }
  input[type=search], select { background:var(--card); border:1px solid var(--line);
         color:var(--fg); padding:8px 10px; border-radius:8px; min-width:220px; }
  table { width:100%; border-collapse:collapse; background:var(--card);
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { position:sticky; top:0; background:#1b2438; cursor:pointer; user-select:none; font-size:12px;
       color:var(--mut); text-transform:uppercase; letter-spacing:.03em; }
  th:hover { color:var(--fg); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:hover td { background:rgba(91,157,255,.06); }
  .pill { padding:2px 8px; border-radius:20px; font-size:12px; font-weight:600; }
  .BUY,.ADD { color:var(--buy); background:rgba(46,194,107,.12); }
  .SELL,.TRIM { color:var(--sell); background:rgba(239,91,107,.12); }
  .pos { color:var(--buy); } .neg { color:var(--sell); }
  .muted { color:var(--mut); }
  .section { display:none; } .section.active { display:block; }
  .count { color:var(--mut); margin-left:auto; font-size:13px; }
  .fundlink { color:var(--accent); cursor:pointer; text-decoration:none; }
  .fundlink:hover { text-decoration:underline; }
  .chip { display:inline-block; background:var(--card); border:1px solid var(--line);
          border-radius:20px; padding:4px 12px; margin:0 6px 6px 0; }
  .banner-wrap { display:flex; align-items:center; gap:10px; background:#10192b;
                 border-bottom:1px solid var(--line); padding:0 14px; }
  .banner-label { flex:0 0 auto; font-size:12px; font-weight:700; color:var(--accent);
                  text-transform:uppercase; letter-spacing:.04em; }
  .banner-view { flex:1 1 auto; overflow:hidden; white-space:nowrap; }
  .banner-track { display:inline-block; padding:9px 0; animation:bannerscroll 220s linear infinite; }
  .banner-view:hover .banner-track { animation-play-state:paused; }
  @keyframes bannerscroll { from{transform:translateX(0)} to{transform:translateX(-50%)} }
  .banner-item { color:var(--fg); text-decoration:none; margin-right:38px; font-size:13px; }
  .banner-item:hover { color:var(--accent); }
  .banner-item .b-src { color:var(--mut); }
  .backbtn { background:var(--card); border:1px solid var(--line); color:var(--fg);
             padding:7px 12px; border-radius:8px; cursor:pointer; margin-bottom:12px; }
  .kpis { display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 16px; }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 14px; }
  .kpi .n { font-size:18px; font-weight:700; } .kpi .l { color:var(--mut); font-size:12px; }
  h2 { margin:6px 0; } h3 { margin:18px 0 8px; font-size:15px; }
  .news { display:flex; flex-direction:column; gap:8px; }
  .newsitem { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
  .newsitem a { color:var(--fg); text-decoration:none; font-weight:600; }
  .newsitem a:hover { color:var(--accent); }
  .newsmeta { color:var(--mut); font-size:12px; margin-top:3px; }
  footer { padding:16px 24px; color:var(--mut); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>Hedge Monitor — Fund Activity</h1>
  <div class="sub">
    <span id="meta">Loading…</span>
    <label style="margin-left:10px;">Country
      <select id="f-country"></select>
    </label>
    <button class="btn" id="refresh-btn" style="margin-left:10px;padding:4px 10px;">↻ Refresh</button>
    <a class="btn" id="rebuild-link" target="_blank" rel="noopener"
       href="https://github.com/NikhilCG/hedge-monitor/actions/workflows/deploy.yml"
       style="margin-left:6px;padding:4px 10px;text-decoration:none;">Rebuild data ↗</a>
  </div>
</header>
<div class="banner-wrap">
  <span class="banner-label" id="banner-label">Latest</span>
  <div class="banner-view"><div class="banner-track" id="banner-track"></div></div>
</div>
<div class="wrap">
  <div class="tabs">
    <button class="tab active" data-t="markets">Markets</button>
    <button class="tab" data-t="actions">Latest Actions (Buy / Sell)</button>
    <button class="tab" data-t="signals">Ratings &amp; Analysts</button>
    <button class="tab" data-t="common">Common Holdings</button>
    <button class="tab" data-t="funds">Funds</button>
  </div>

  <div class="section active" id="s-markets">
    <h3>Indices</h3>
    <div style="overflow:auto; max-height:26vh;"><table id="t-indices"></table></div>
    <h3>Currencies (FX)</h3>
    <div style="overflow:auto; max-height:26vh;"><table id="t-fx"></table></div>
    <h3>Cryptocurrency</h3>
    <div style="overflow:auto; max-height:26vh;"><table id="t-crypto"></table></div>
    <h3>Most Active Stocks (US, by volume)</h3>
    <div style="overflow:auto; max-height:34vh;"><table id="t-active"></table></div>
  </div>

  <div class="section" id="s-actions">
    <div class="controls">
      <input type="search" id="q-actions" placeholder="Search fund or stock…">
      <select id="f-side">
        <option value="">All actions</option>
        <option value="buy">Buys only</option>
        <option value="sell">Sells only</option>
      </select>
      <span class="count" id="c-actions"></span>
    </div>
    <div style="overflow:auto; max-height:70vh;"><table id="t-actions"></table></div>
  </div>

  <div class="section" id="s-common">
    <div class="controls">
      <input type="search" id="q-common" placeholder="Search stock or fund…">
      <span class="muted">Click a stock to see who holds it &amp; its actions by date</span>
      <span class="count" id="c-common"></span>
    </div>
    <div style="overflow:auto; max-height:70vh;"><table id="t-common"></table></div>
  </div>

  <div class="section" id="s-stock">
    <button class="backbtn" id="back-stock">← Common holdings</button>
    <h2 id="stock-name"></h2>
    <div class="kpis" id="stock-kpis"></div>
    <h3>Actions on this stock (by date)</h3>
    <div style="overflow:auto; max-height:38vh;"><table id="t-stock-actions"></table></div>
    <h3>Held by <span class="muted" id="held-sub"></span></h3>
    <div id="held-funds"></div>
  </div>

  <div class="section" id="s-signals">
    <h3>Buy / Sell Signals — Famous Stocks (from news)</h3>
    <div class="controls">
      <input type="search" id="q-sig" placeholder="Search stock or headline…">
      <select id="f-sig">
        <option value="">All signals</option>
        <option value="BUY">Buy</option>
        <option value="SELL">Sell</option>
      </select>
      <span class="count" id="c-sig"></span>
    </div>
    <div style="overflow:auto; max-height:38vh;"><table id="t-sig"></table></div>
    <h3>Analyst Commentary (Jim Cramer &amp; others)</h3>
    <div class="controls">
      <input type="search" id="q-an" placeholder="Search analyst or headline…">
      <select id="f-analyst"><option value="">All analysts</option></select>
      <span class="count" id="c-an"></span>
    </div>
    <div style="overflow:auto; max-height:38vh;"><table id="t-an"></table></div>
  </div>

  <div class="section" id="s-funds">
    <div class="controls">
      <input type="search" id="q-funds" placeholder="Search fund…">
      <label class="muted">Sort by
        <select id="f-fundsort">
          <option value="value">Portfolio value (assets)</option>
          <option value="date">Latest filing date</option>
          <option value="action">Latest action date</option>
          <option value="positions">Positions</option>
          <option value="buys">Buys</option>
          <option value="sells">Sells</option>
        </select>
      </label>
      <span class="muted">Click a fund to open its page</span>
      <span class="count" id="c-funds"></span>
    </div>
    <div style="overflow:auto; max-height:70vh;"><table id="t-funds"></table></div>
  </div>

  <div class="section" id="s-fund">
    <button class="backbtn" id="backbtn">← All funds</button>
    <h2 id="fund-name"></h2>
    <div class="kpis" id="fund-kpis"></div>
    <h3>Latest Actions (Buy / Sell)</h3>
    <div style="overflow:auto; max-height:40vh;"><table id="t-fund-actions"></table></div>
    <h3>Latest News <span class="muted" id="news-sub"></span></h3>
    <div class="news" id="fund-news"></div>
  </div>
</div>
<footer>Holdings &amp; buy/sell: SEC EDGAR 13F filings (public, quarterly). Live prices &amp; day change: Yahoo Finance public quote API.
Fund list from hedgefollow.com featured managers. Bloomberg is paywalled and not scraped.</footer>

<script>
const fmtUsd = v => {
  v = Number(v)||0; const a = Math.abs(v);
  if (a >= 1e9) return '$'+(v/1e9).toFixed(2)+'B';
  if (a >= 1e6) return '$'+(v/1e6).toFixed(2)+'M';
  if (a >= 1e3) return '$'+(v/1e3).toFixed(1)+'K';
  return '$'+v;
};
const fmtNum = v => (Number(v)||0).toLocaleString('en-US');
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let DATA = {funds:[], actions:[], common:[], news_feed:[], stock_signals:[], analysts:[], countries:[], markets:{}};
let FLAGS = {};
const countryFlag = c => FLAGS[c] ? FLAGS[c] + ' ' + c : (c||'');
function populateCountries() {
  FLAGS = {}; (DATA.countries||[]).forEach(c => FLAGS[c.code] = c.flag);
  const sel = document.getElementById('f-country');
  if (sel.options.length) return;  // populate once
  const all = document.createElement('option'); all.value=''; all.textContent='\uD83C\uDF0D All countries'; sel.appendChild(all);
  (DATA.countries||[]).forEach(c => {
    const o=document.createElement('option'); o.value=c.code; o.textContent=`${c.flag} ${c.name}`; sel.appendChild(o);
  });
  sel.value = 'US';  // default
}

function makeTable(el, cols, rows) {
  let sortIdx = -1, sortDir = -1;
  function render() {
    let r = rows.slice();
    if (sortIdx >= 0) {
      const key = cols[sortIdx].key;
      r.sort((a,b) => {
        let x=a[key], y=b[key];
        if (typeof x === 'number' || typeof y === 'number') return (x-y)*sortDir;
        return String(x).localeCompare(String(y))*sortDir;
      });
    }
    let html = '<thead><tr>' + cols.map((c,i) =>
      `<th data-i="${i}">${c.label}${i===sortIdx?(sortDir>0?' ▲':' ▼'):''}</th>`).join('') + '</tr></thead><tbody>';
    html += r.map(row => '<tr>' + cols.map(c =>
      `<td class="${c.num?'num':''}">${c.fmt?c.fmt(row[c.key],row):esc(row[c.key])}</td>`).join('') + '</tr>').join('');
    html += '</tbody>';
    el.innerHTML = html;
    el.querySelectorAll('th').forEach(th => th.onclick = () => {
      const i = +th.dataset.i;
      if (i===sortIdx) sortDir = -sortDir; else { sortIdx = i; sortDir = 1; }
      render();
    });
  }
  render();
  return rows.length;
}

const actionPill = a => `<span class="pill ${a}">${a}</span>`;
const deltaCell = v => `<span class="${v>=0?'pos':'neg'}">${v>=0?'+':''}${fmtNum(v)}</span>`;
const priceCell = v => (v==null) ? '<span class="muted">—</span>' : '$'+Number(v).toFixed(2);
const pctCell = v => (v==null) ? '<span class="muted">—</span>'
  : `<span class="${v>=0?'pos':'neg'}">${v>=0?'+':''}${Number(v).toFixed(2)}%</span>`;
const tickerCell = v => v ? `<b>${esc(v)}</b>` : '<span class="muted">—</span>';

function renderActions() {
  const q = document.getElementById('q-actions').value.toLowerCase();
  const side = document.getElementById('f-side').value;
  const country = document.getElementById('f-country').value;
  let rows = DATA.actions.filter(a =>
    (a.fund+' '+a.name+' '+(a.ticker||'')).toLowerCase().includes(q) &&
    (!side || (side==='buy' ? ['BUY','ADD'].includes(a.action) : ['SELL','TRIM'].includes(a.action))) &&
    (!country || (a.country||'US') === country)
  );
  const n = makeTable(document.getElementById('t-actions'), [
    {label:'Fund', key:'fund'},
    {label:'Date', key:'date', fmt:v=>v||'<span class="muted">—</span>'},
    {label:'Action', key:'action', fmt:actionPill},
    {label:'Stock', key:'name'},
    {label:'Ticker', key:'ticker', fmt:tickerCell},
    {label:'Price (live)', key:'price', num:true, fmt:priceCell},
    {label:'Day %', key:'day_pct', num:true, fmt:pctCell},
    {label:'Shares Δ', key:'shares_delta', num:true, fmt:deltaCell},
    {label:'Value', key:'value', num:true, fmt:v=>v?fmtUsd(v):'<span class="muted">—</span>'},
  ], rows);
  document.getElementById('c-actions').textContent = n + ' rows';
}

function renderCommon() {
  const q = document.getElementById('q-common').value.toLowerCase();
  const country = document.getElementById('f-country').value;
  if (country && country !== 'US') {
    document.getElementById('t-common').innerHTML = '';
    document.getElementById('c-common').textContent =
      `Shared-holdings needs 13F disclosure — only 🇺🇸 US. ${FLAGS[country]||''} ${country} managers are news-only.`;
    return;
  }
  let rows = DATA.common.filter(c =>
    (c.name+' '+(c.ticker||'')+' '+c.funds.join(' ')).toLowerCase().includes(q));
  const n = makeTable(document.getElementById('t-common'), [
    {label:'Stock', key:'name', fmt:(v,row)=>`<a class="stocklink" data-cusip="${esc(row.cusip)}">${esc(v)}</a>`},
    {label:'Ticker', key:'ticker', fmt:tickerCell},
    {label:'Price (live)', key:'price', num:true, fmt:priceCell},
    {label:'Day %', key:'day_pct', num:true, fmt:pctCell},
    {label:'# Funds', key:'num_funds', num:true},
    {label:'Total Value', key:'total_value', num:true, fmt:fmtUsd},
    {label:'Total Shares', key:'total_shares', num:true, fmt:fmtNum},
  ], rows);
  document.getElementById('c-common').textContent = n + ' rows';
}

function openStock(cusip) {
  const c = (DATA.common||[]).find(x => x.cusip === cusip);
  if (!c) return;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  document.getElementById('s-stock').classList.add('active');
  document.getElementById('stock-name').textContent = c.name + (c.ticker ? ' ('+c.ticker+')' : '');
  const kpis = [
    ['Ticker', c.ticker||'—'],
    ['Price (live)', c.price!=null ? '$'+Number(c.price).toFixed(2) : '—'],
    ['Day %', c.day_pct!=null ? (c.day_pct>=0?'+':'')+Number(c.day_pct).toFixed(2)+'%' : '—'],
    ['# Funds', fmtNum(c.num_funds)], ['Total Value', fmtUsd(c.total_value)],
    ['Total Shares', fmtNum(c.total_shares)],
  ];
  document.getElementById('stock-kpis').innerHTML = kpis.map(([l,val])=>
    `<div class="kpi"><div class="n">${val}</div><div class="l">${l}</div></div>`).join('');
  const acts = (DATA.actions||[]).filter(a => a.cusip === cusip)
    .slice().sort((a,b)=>String(b.date||'').localeCompare(String(a.date||'')));
  makeTable(document.getElementById('t-stock-actions'), [
    {label:'Date', key:'date', fmt:v=>v||'<span class="muted">—</span>'},
    {label:'Fund', key:'fund', fmt:v=>`<a class="fundlink" data-name="${esc(v)}">${esc(v)}</a>`},
    {label:'Action', key:'action', fmt:actionPill},
    {label:'Shares Δ', key:'shares_delta', num:true, fmt:deltaCell},
    {label:'Value', key:'value', num:true, fmt:v=>v?fmtUsd(v):'<span class="muted">—</span>'},
  ], acts);
  document.getElementById('held-sub').textContent = `(${c.num_funds})`;
  document.getElementById('held-funds').innerHTML = c.funds.map(nm =>
    `<span class="chip"><a class="fundlink" data-name="${esc(nm)}">${esc(nm)}</a></span>`).join('');
  window.scrollTo(0,0);
}

function renderNews() {
  const q = document.getElementById('q-news').value.toLowerCase();
  const src = document.getElementById('f-source').value;
  const country = document.getElementById('f-country').value;
  let rows = (DATA.news_feed||[]).filter(x =>
    (x.title+' '+(x.source||'')+' '+x.funds.join(' ')).toLowerCase().includes(q) &&
    (!src || x.source === src) && (!country || (x.country||'US') === country));
  const srcPill = v => `<span class="pill" style="background:rgba(91,157,255,.12);color:var(--accent)">${esc(v||'—')}</span>`;
  const headline = (v,row) => `<a href="${esc(row.link)}" target="_blank" rel="noopener" style="color:var(--fg);text-decoration:none;font-weight:600">${esc(v)}</a>`;
  const n = makeTable(document.getElementById('t-news'), [
    {label:'Date (UTC)', key:'published'},
    {label:'Country', key:'country', fmt:v=>countryFlag(v||'US')},
    {label:'Source', key:'source', fmt:srcPill},
    {label:'Headline', key:'title', fmt:headline},
    {label:'Fund(s)', key:'funds', fmt:v=>`<span class="muted">${esc(v.join(', '))}</span>`},
  ], rows);
  document.getElementById('c-news').textContent = n + ' items';
}

function populateSources() {
  const sel = document.getElementById('f-source');
  const set = [...new Set((DATA.news_feed||[]).map(x=>x.source).filter(Boolean))].sort();
  set.forEach(s => { const o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
}

function renderMarkets() {
  const m = DATA.markets || {};
  const num = v => v==null ? '<span class="muted">\u2014</span>' : Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
  const cols = [
    {label:'Name', key:'name'},
    {label:'Symbol', key:'symbol', fmt:v=>`<span class="muted">${esc(v)}</span>`},
    {label:'Price', key:'price', num:true, fmt:num},
    {label:'Change %', key:'change_pct', num:true, fmt:pctCell},
  ];
  makeTable(document.getElementById('t-indices'), cols, m.indices||[]);
  makeTable(document.getElementById('t-fx'), cols, m.currencies||[]);
  makeTable(document.getElementById('t-crypto'), cols, m.crypto||[]);
  makeTable(document.getElementById('t-active'), [
    {label:'Symbol', key:'symbol', fmt:v=>`<b>${esc(v)}</b>`},
    {label:'Name', key:'name'},
    {label:'Price', key:'price', num:true, fmt:v=>v==null?'\u2014':'$'+Number(v).toFixed(2)},
    {label:'Change %', key:'change_pct', num:true, fmt:pctCell},
    {label:'Volume', key:'volume', num:true, fmt:v=>v==null?'\u2014':fmtNum(v)},
  ], m.most_active||[]);
}

function renderBanner() {
  const country = document.getElementById('f-country').value;
  let items = (DATA.news_feed||[]);
  if (country) items = items.filter(x => (x.country||'US') === country);
  items = items.slice(0, 20);
  document.getElementById('banner-label').textContent =
    (country ? (FLAGS[country]||'') : '🌍') + ' Latest';
  const track = document.getElementById('banner-track');
  if (!items.length) { track.innerHTML = '<span class="banner-item muted">No recent news</span>'; return; }
  const html = items.map(n =>
    `<a class="banner-item" href="${esc(n.link)}" target="_blank" rel="noopener">\uD83D\uDCF0 ${esc(n.title)}<span class="b-src"> — ${esc(n.source||'')}</span></a>`
  ).join('');
  track.innerHTML = html + html;  // duplicated for a seamless loop
}

const newsLink = (v,row) => `<a href="${esc(row.link)}" target="_blank" rel="noopener" style="color:var(--fg);text-decoration:none;font-weight:600">${esc(v)}</a>`;

function renderSignals() {
  const q = document.getElementById('q-sig').value.toLowerCase();
  const s = document.getElementById('f-sig').value;
  const country = document.getElementById('f-country').value;
  let rows = (DATA.stock_signals||[]).filter(x =>
    (x.ticker+' '+x.company+' '+x.title).toLowerCase().includes(q) && (!s || x.signal === s) &&
    (!country || (x.country||'US') === country));
  const n = makeTable(document.getElementById('t-sig'), [
    {label:'Date (UTC)', key:'published'},
    {label:'Country', key:'country', fmt:v=>countryFlag(v||'US')},
    {label:'Stock', key:'ticker', fmt:v=>`<b>${esc(v)}</b>`},
    {label:'Signal', key:'signal', fmt:actionPill},
    {label:'Headline', key:'title', fmt:newsLink},
    {label:'Source', key:'source', fmt:v=>`<span class="muted">${esc(v||'')}</span>`},
  ], rows);
  document.getElementById('c-sig').textContent = n + ' signals';
}

function renderAnalysts() {
  const q = document.getElementById('q-an').value.toLowerCase();
  const a = document.getElementById('f-analyst').value;
  const country = document.getElementById('f-country').value;
  let rows = (DATA.analysts||[]).filter(x =>
    (x.analyst+' '+x.title+' '+(x.source||'')).toLowerCase().includes(q) && (!a || x.analyst === a) &&
    (!country || (x.country||'US') === country));
  const n = makeTable(document.getElementById('t-an'), [
    {label:'Date (UTC)', key:'published'},
    {label:'Country', key:'country', fmt:v=>countryFlag(v||'US')},
    {label:'Analyst', key:'analyst', fmt:v=>`<b>${esc(v)}</b>`},
    {label:'Headline', key:'title', fmt:newsLink},
    {label:'Source', key:'source', fmt:v=>`<span class="muted">${esc(v||'')}</span>`},
  ], rows);
  document.getElementById('c-an').textContent = n + ' items';
}

function populateAnalysts() {
  const sel = document.getElementById('f-analyst');
  const set = [...new Set((DATA.analysts||[]).map(x=>x.analyst).filter(Boolean))].sort();
  set.forEach(s => { const o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
}

function renderFunds() {
  const q = document.getElementById('q-funds').value.toLowerCase();
  const sort = document.getElementById('f-fundsort').value;
  const country = document.getElementById('f-country').value;
  let rows = DATA.funds.filter(f => f.name.toLowerCase().includes(q) &&
    (!country || (f.country||'US') === country));
  const lastAct = {};
  (DATA.actions||[]).forEach(a => {
    const d = a.date || '';
    if (!lastAct[a.fund] || d > lastAct[a.fund]) lastAct[a.fund] = d;
  });
  const keyFns = {
    value: f => f.total_value||0,
    date: f => f.filing_date||'',
    action: f => lastAct[f.name]||'',
    positions: f => f.positions||0,
    buys: f => f.buys||0,
    sells: f => f.sells||0,
  };
  const kf = keyFns[sort] || keyFns.value;
  rows = rows.slice().sort((a,b) => {
    const x=kf(a), y=kf(b);
    if (typeof x === 'number') return y-x;
    return String(y).localeCompare(String(x));
  });
  const n = makeTable(document.getElementById('t-funds'), [
    {label:'#', key:'rank', num:true},
    {label:'Country', key:'country', fmt:v=>countryFlag(v||'US')},
    {label:'Fund', key:'name', fmt:v=>`<a class="fundlink" data-name="${esc(v)}">${esc(v)}</a>`},
    {label:'Latest Filing', key:'filing_date'},
    {label:'Positions', key:'positions', num:true, fmt:fmtNum},
    {label:'Buys', key:'buys', num:true, fmt:v=>`<span class="pos">${fmtNum(v)}</span>`},
    {label:'Sells', key:'sells', num:true, fmt:v=>`<span class="neg">${fmtNum(v)}</span>`},
    {label:'News', key:'news', num:true, fmt:v=>fmtNum((v||[]).length)},
    {label:'Portfolio Value', key:'total_value', num:true, fmt:fmtUsd},
  ], rows);
  document.getElementById('c-funds').textContent = n + ' rows';
}

function openFund(name) {
  const f = DATA.funds.find(x => x.name === name);
  if (!f) return;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  document.getElementById('s-fund').classList.add('active');
  document.getElementById('fund-name').textContent = f.name;
  const kpis = [
    ['Latest Filing', f.filing_date||'—'], ['Positions', fmtNum(f.positions)],
    ['Buys', fmtNum(f.buys)], ['Sells', fmtNum(f.sells)],
    ['Portfolio Value', fmtUsd(f.total_value)],
  ];
  document.getElementById('fund-kpis').innerHTML = kpis.map(([l,n])=>
    `<div class="kpi"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
  const acts = DATA.actions.filter(a => a.fund === name);
  makeTable(document.getElementById('t-fund-actions'), [
    {label:'Date', key:'date', fmt:v=>v||'<span class="muted">—</span>'},
    {label:'Action', key:'action', fmt:actionPill},
    {label:'Stock', key:'name'},
    {label:'Ticker', key:'ticker', fmt:tickerCell},
    {label:'Price (live)', key:'price', num:true, fmt:priceCell},
    {label:'Day %', key:'day_pct', num:true, fmt:pctCell},
    {label:'Shares Δ', key:'shares_delta', num:true, fmt:deltaCell},
    {label:'Value', key:'value', num:true, fmt:v=>v?fmtUsd(v):'<span class="muted">—</span>'},
  ], acts);
  const news = f.news || [];
  document.getElementById('news-sub').textContent = news.length ? `(${news.length})` : '';
  document.getElementById('fund-news').innerHTML = news.length ? news.map(item =>
    `<div class="newsitem"><a href="${esc(item.link)}" target="_blank" rel="noopener">${esc(item.title)}</a>`
    + `<div class="newsmeta">${esc(item.source||'')}${item.published?' · '+esc(item.published):''}</div></div>`
  ).join('') : '<div class="muted">No recent news found for this fund.</div>';
  window.scrollTo(0,0);
}

document.addEventListener('click', e => {
  const link = e.target.closest('.fundlink');
  if (link) { e.preventDefault(); openFund(link.dataset.name); return; }
  const slink = e.target.closest('.stocklink');
  if (slink) { e.preventDefault(); openStock(slink.dataset.cusip); }
});
document.getElementById('backbtn').onclick = () => {
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  document.querySelector('.tab[data-t="funds"]').classList.add('active');
  document.getElementById('s-funds').classList.add('active');
};
document.getElementById('back-stock').onclick = () => {
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  document.querySelector('.tab[data-t="common"]').classList.add('active');
  document.getElementById('s-common').classList.add('active');
};

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('s-'+t.dataset.t).classList.add('active');
});
['q-actions','f-side'].forEach(id=>document.getElementById(id).addEventListener('input',renderActions));
document.getElementById('q-common').addEventListener('input',renderCommon);
document.getElementById('q-funds').addEventListener('input',renderFunds);
document.getElementById('f-fundsort').addEventListener('change',renderFunds);
document.getElementById('f-country').addEventListener('change',()=>{ renderFunds(); renderActions(); renderSignals(); renderAnalysts(); renderCommon(); renderBanner(); });
['q-sig','f-sig'].forEach(id=>document.getElementById(id).addEventListener('input',renderSignals));
['q-an','f-analyst'].forEach(id=>document.getElementById(id).addEventListener('input',renderAnalysts));

async function loadData(initial) {
  try {
    const d = await (await fetch('data.json?t=' + Date.now())).json();
    DATA = d;
    const priced = d.priced_at ? ` · live prices ${d.priced_at}` : '';
    document.getElementById('meta').textContent =
      `${d.funds.length} funds · updated ${d.generated}${priced}`;
    if (initial) { populateAnalysts(); }
    populateCountries();
    renderActions(); renderCommon(); renderFunds();
    renderSignals(); renderAnalysts(); renderBanner(); renderMarkets();
  } catch (e) {
    document.getElementById('meta').textContent = 'Failed to load data.json: ' + e;
  }
}
loadData(true);
setInterval(() => loadData(false), 20000);  // re-read local data.json every 20s (no external calls)
document.getElementById('refresh-btn').addEventListener('click', async () => {
  const b = document.getElementById('refresh-btn');
  b.textContent = '↻ Refreshing…'; b.disabled = true;
  await loadData(false);
  b.textContent = '↻ Refresh'; b.disabled = false;
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
