"""Command-line entry point for Hedge Monitor.

Commands:
  holdings   Check tracked funds' latest 13F filings; report changes.
  news       Scan RSS feeds; report items matching watched tickers/keywords.
  prices     Check watched tickers for large daily moves.
  run        Run all of the above once.

Legal note: this uses only public data (SEC EDGAR 13F filings, public RSS
feeds, and public daily quotes). 13F holdings are disclosed quarterly, not
daily. There is no legal source of real-time hedge fund account activity.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import edgar as edgar_mod
from . import news as news_mod
from . import prices as prices_mod
from .config import Config, load_config
from .notifier import Notifier
from .storage import Storage


def _notifier(cfg: Config) -> Notifier:
    return Notifier(cfg.notify, cfg.email_password)


def cmd_holdings(cfg: Config, store: Storage, notifier: Notifier) -> None:
    client = edgar_mod.EdgarClient(cfg.sec_user_agent)
    for fund in cfg.funds:
        name = fund.get("name", fund.get("cik", "?"))
        cik = fund.get("cik", "")
        if not cik:
            continue
        print(f"[holdings] {name} (CIK {cik})")
        try:
            filing = client.latest_13f(cik)
        except Exception as exc:  # noqa: BLE001
            print(f"  error fetching filings: {exc}")
            continue
        if filing is None:
            print("  no 13F-HR filing found")
            continue

        state_key = f"holdings_{cik}"
        prev = store.load(state_key, default={})
        if prev.get("accession") == filing.accession:
            print(f"  no new filing (latest {filing.filing_date})")
            continue

        try:
            current = client.holdings(filing)
        except Exception as exc:  # noqa: BLE001
            print(f"  error parsing holdings: {exc}")
            continue

        diff = edgar_mod.diff_holdings(prev.get("holdings", []), current)
        n_changes = sum(len(v) for v in diff.values())
        print(
            f"  new filing {filing.filing_date}: {len(current)} positions, "
            f"{n_changes} changes"
        )

        if prev and n_changes:
            lines = [f"{name} filed a new 13F ({filing.filing_date}):"]
            for label in ("added", "removed", "increased", "decreased"):
                for item in diff[label][:20]:
                    lines.append(f"  {label.upper()}: {item['name']} ({item['cusip']})")
            notifier.send(f"13F change: {name}", "\n".join(lines))

        store.save(
            state_key,
            {
                "accession": filing.accession,
                "filing_date": filing.filing_date,
                "holdings": [
                    {"name": h.name, "cusip": h.cusip, "shares": h.shares, "value": h.value}
                    for h in current
                ],
            },
        )


def cmd_news(cfg: Config, store: Storage, notifier: Notifier) -> None:
    terms = cfg.watch_tickers + cfg.news_keywords + [f["name"] for f in cfg.funds]
    seen = set(store.load("news_seen", default=[]))
    new_seen = list(seen)

    for feed_url in cfg.news_feeds:
        try:
            items = news_mod.fetch_feed(feed_url)
        except Exception as exc:  # noqa: BLE001
            print(f"[news] error {feed_url}: {exc}")
            continue
        for item in items:
            if item.id in seen:
                continue
            new_seen.append(item.id)
            hits = news_mod.matches(item, terms)
            if hits:
                body = (
                    f"{item.title}\n{item.link}\n"
                    f"Matched: {', '.join(sorted(set(hits)))}\nSource: {item.source}"
                )
                notifier.send("News match", body)

    store.save("news_seen", new_seen[-2000:])


def cmd_prices(cfg: Config, store: Storage, notifier: Notifier) -> None:
    threshold = cfg.price_move_threshold_pct
    for ticker in cfg.watch_tickers:
        try:
            quote = prices_mod.get_quote(ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"[prices] error {ticker}: {exc}")
            continue
        if quote is None:
            print(f"[prices] no data for {ticker}")
            continue
        pct = quote.change_pct
        flag = " <== ALERT" if abs(pct) >= threshold else ""
        print(f"[prices] {ticker} {quote.date} price={quote.price} ({pct:+.2f}%){flag}")
        if abs(pct) >= threshold:
            notifier.send(
                f"Price move: {ticker} {pct:+.2f}%",
                f"{ticker} moved {pct:+.2f}% on {quote.date}. Price: {quote.price}",
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hedge-monitor", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yml", help="Path to config.yml")
    parser.add_argument("--data-dir", default="data", help="Directory for saved state")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("holdings", "news", "prices", "run"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    store = Storage(Path(args.data_dir))
    notifier = _notifier(cfg)

    commands = {
        "holdings": cmd_holdings,
        "news": cmd_news,
        "prices": cmd_prices,
    }
    if args.command == "run":
        for fn in commands.values():
            fn(cfg, store, notifier)
    else:
        commands[args.command](cfg, store, notifier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
