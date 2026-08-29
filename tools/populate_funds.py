"""Populate config.yml's `funds` list with the top hedge funds from
hedgefollow.com, resolving each manager to its SEC EDGAR CIK.

The fund list mirrors the "Top Searched Hedge Funds" featured on
https://hedgefollow.com/. HedgeFollow does not publish SEC CIK numbers, so
each manager is resolved live against SEC EDGAR's company search (13F filers
only). This keeps CIKs correct without hard-coding them.

Usage (from the repo root):
    python -m tools.populate_funds            # write config.yml
    python -m tools.populate_funds --dry-run  # print resolved CIKs only
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from hedge_monitor.config import load_config
from hedge_monitor.edgar import EdgarClient

# (display name, SEC EDGAR search prefixes to try in order).
# The first prefix that matches a 13F filer wins. Prefixes are case-insensitive
# leading substrings of the SEC "conformed name".
FUNDS: list[tuple[str, list[str]]] = [
    ("Situational Awareness (Leopold Aschenbrenner)", ["Situational Awareness"]),
    ("Berkshire Hathaway (Warren Buffett)", ["Berkshire Hathaway"]),
    ("Duquesne Family Office (Stanley Druckenmiller)", ["Duquesne Family Office"]),
    ("Pershing Square (Bill Ackman)", ["Pershing Square Capital"]),
    ("Bridgewater Associates (Ray Dalio)", ["Bridgewater Associates"]),
    ("Renaissance Technologies (Jim Simons)", ["Renaissance Technologies"]),
    ("BlackRock", ["Blackrock Inc"]),
    ("Altimeter Capital (Brad Gerstner)", ["Altimeter Capital"]),
    ("TCI Fund Management (Chris Hohn)", ["TCI Fund Management"]),
    ("Appaloosa (David Tepper)", ["Appaloosa"]),
    ("Tudor Investment (Paul Tudor Jones)", ["Tudor Investment"]),
    ("Citadel Advisors (Ken Griffin)", ["Citadel Advisors"]),
    ("Himalaya Capital (Li Lu)", ["Himalaya Capital"]),
    ("ARK Investment Management (Cathie Wood)", ["Ark Investment Management"]),
    ("Jane Street Group", ["Jane Street Group"]),
    ("Oaktree Capital (Howard Marks)", ["Oaktree Capital"]),
    ("Point72 (Steven Cohen)", ["Point72 Asset Management"]),
    ("Coatue Management (Philippe Laffont)", ["Coatue Management"]),
    ("Dalal Street (Mohnish Pabrai)", ["Dalal Street"]),
    ("Fisher Asset Management (Ken Fisher)", ["Fisher Asset Management"]),
    ("Atreides Management (Gavin Baker)", ["Atreides Management"]),
    ("Soros Fund Management (George Soros)", ["Soros Fund Management"]),
    ("Valley Forge Capital (Dev Kantesaria)", ["Valley Forge Capital"]),
    ("Semper Augustus (Christopher Bloomstran)", ["Semper Augustus"]),
    ("Baupost Group (Seth Klarman)", ["Baupost Group"]),
    ("Markel (Tom Gayner)", ["Markel Group", "Markel Corp", "Markel"]),
    ("Tiger Global Management (Chase Coleman)", ["Tiger Global"]),
    ("Fundsmith (Terry Smith)", ["Fundsmith"]),
    ("Alpine Fox (Mike Alfred)", ["Alpine Fox"]),
    ("Whale Rock Capital (Alex Sacerdote)", ["Whale Rock Capital"]),
    ("Millennium Management (Israel Englander)", ["Millennium Management"]),
    ("Elliott Investment Management (Paul Singer)", ["Elliott Investment Management"]),
    ("Horizon Kinetics (Murray Stahl)", ["Horizon Kinetics"]),
    ("Robotti (Bob Robotti)", ["Robotti"]),
    ("D. E. Shaw (David Shaw)", ["D. E. Shaw", "D E Shaw", "Shaw D E"]),
    ("Giverny Capital (Francois Rochon)", ["Giverny Capital"]),
    ("Gates Foundation Trust (Bill Gates)", ["Bill & Melinda Gates Foundation"]),
    ("JPMorgan Chase", ["J.P. Morgan"]),
    ("Baker Bros Advisors (Julian Baker)", ["Baker Bros"]),
    ("Icahn (Carl Icahn)", ["Icahn Carl", "Icahn"]),
    ("Eminence Capital (Ricky Sandler)", ["Eminence Capital"]),
    ("D1 Capital Partners (Daniel Sundheim)", ["D1 Capital"]),
    ("Gotham Asset Management (Joel Greenblatt)", ["Gotham Asset Management"]),
    ("Ratan Capital (Nehal Chopra)", ["Ratan Capital"]),
    ("Fairfax Financial Holdings (Prem Watsa)", ["Fairfax Financial"]),
    ("Pentwater Capital Management", ["Pentwater Capital"]),
    ("H&H International Investment", ["H&H International", "H & H International"]),
    ("Goldman Sachs Group", ["Goldman Sachs Group"]),
    # --- Expanded list (additional famous funds / institutional managers) ---
    ("Lone Pine Capital (Stephen Mandel)", ["Lone Pine Capital"]),
    ("Viking Global Investors (Andreas Halvorsen)", ["Viking Global Investors"]),
    ("Third Point (Dan Loeb)", ["Third Point"]),
    ("Greenlight Capital (David Einhorn)", ["Greenlight Capital"]),
    ("Maverick Capital (Lee Ainslie)", ["Maverick Capital"]),
    ("Two Sigma Investments", ["Two Sigma Investments"]),
    ("Two Sigma Advisers", ["Two Sigma Advisers"]),
    ("AQR Capital Management (Cliff Asness)", ["AQR Capital Management"]),
    ("Marshall Wace", ["Marshall Wace"]),
    ("Balyasny Asset Management", ["Balyasny Asset Management"]),
    ("Hudson Bay Capital Management", ["Hudson Bay Capital"]),
    ("Farallon Capital Management", ["Farallon Capital"]),
    ("Davidson Kempner Capital", ["Davidson Kempner"]),
    ("Glenview Capital (Larry Robbins)", ["Glenview Capital"]),
    ("Trian Fund Management (Nelson Peltz)", ["Trian Fund Management"]),
    ("ValueAct Capital", ["ValueAct"]),
    ("Starboard Value (Jeff Smith)", ["Starboard Value"]),
    ("Sands Capital Management", ["Sands Capital"]),
    ("Durable Capital Partners", ["Durable Capital Partners"]),
    ("Egerton Capital", ["Egerton Capital"]),
    ("Baillie Gifford", ["Baillie Gifford"]),
    ("T. Rowe Price", ["Price T Rowe Associates", "Price T Rowe"]),
    ("Vanguard Group", ["Vanguard Group"]),
    ("State Street Corp", ["State Street Corp"]),
    ("Wellington Management Group", ["Wellington Management Group", "Wellington Management"]),
    ("Dodge & Cox", ["Dodge & Cox"]),
    ("Geode Capital Management", ["Geode Capital"]),
    ("Northern Trust", ["Northern Trust"]),
    ("Bank of America", ["Bank of America"]),
    ("Morgan Stanley", ["Morgan Stanley"]),
    ("Wells Fargo & Company", ["Wells Fargo"]),
    ("UBS Group", ["UBS Group"]),
    ("Deutsche Bank", ["Deutsche Bank"]),
    ("Barclays", ["Barclays"]),
    ("Susquehanna International Group", ["Susquehanna International Group", "Susquehanna"]),
    ("Schonfeld Strategic Advisors", ["Schonfeld Strategic Advisors"]),
    ("Verition Fund Management", ["Verition Fund Management"]),
    ("PDT Partners", ["PDT Partners"]),
    ("Akre Capital Management (Chuck Akre)", ["Akre Capital Management"]),
    ("Ruane Cunniff (Sequoia)", ["Ruane Cunniff"]),
    ("First Eagle Investment Management", ["First Eagle Investment"]),
    ("Southeastern Asset Management (Longleaf)", ["Southeastern Asset Management"]),
    ("Pzena Investment Management", ["Pzena Investment Management"]),
    ("Diamond Hill Capital", ["Diamond Hill Capital"]),
    ("Polen Capital Management", ["Polen Capital"]),
    ("Sanders Capital", ["Sanders Capital"]),
    ("Yacktman Asset Management", ["Yacktman Asset Management"]),
    ("Abrams Capital Management (David Abrams)", ["Abrams Capital"]),
    ("Jana Partners", ["Jana Partners"]),
    ("Sculptor Capital", ["Sculptor Capital"]),
    ("Corvex Management (Keith Meister)", ["Corvex Management"]),
    ("Sachem Head Capital (Scott Ferguson)", ["Sachem Head Capital"]),
    ("Senator Investment Group", ["Senator Investment Group"]),
    ("Light Street Capital", ["Light Street Capital"]),
    ("Tybourne Capital Management", ["Tybourne Capital"]),
    ("Matrix Capital Management", ["Matrix Capital Management"]),
    ("Steadfast Capital Management", ["Steadfast Capital Management"]),
    ("Brave Warrior Advisors (Glenn Greenberg)", ["Brave Warrior"]),
    ("Hillhouse (HHLR Advisors)", ["HHLR Advisors", "Hillhouse"]),
    ("Capital Research Global Investors", ["Capital Research Global Investors", "Capital Research"]),
    ("Slate Path Capital", ["Slate Path Capital"]),
]


def resolve(client: EdgarClient, prefixes: list[str], retries: int = 3) -> tuple[str, str] | None:
    for prefix in prefixes:
        for attempt in range(retries):
            try:
                hit = client.search_cik(prefix)
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                print(f"    error searching '{prefix}': {exc}", file=sys.stderr)
                hit = None
            break
        if hit:
            return hit
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="config.yml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print resolved CIKs; do not write config."
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    client = EdgarClient(cfg.sec_user_agent)

    resolved: list[dict[str, str]] = []
    unresolved: list[str] = []
    for display, prefixes in FUNDS:
        hit = resolve(client, prefixes)
        if hit is None:
            unresolved.append(display)
            print(f"[MISS] {display}")
            continue
        cik, conformed = hit
        resolved.append({"name": display, "cik": cik})
        print(f"[ OK ] {display} -> CIK {cik} ({conformed})")

    print(f"\nResolved {len(resolved)}/{len(FUNDS)} funds.")
    if unresolved:
        print("Unresolved:")
        for name in unresolved:
            print(f"  - {name}")

    if args.dry_run:
        return 0

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw["funds"] = resolved
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
    print(f"\nWrote {len(resolved)} funds to {config_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
