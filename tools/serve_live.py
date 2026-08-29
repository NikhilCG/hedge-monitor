"""Serve the built site on your LAN and periodically refresh prices + news.

Run:
    python -m tools.serve_live                 # LAN + refresh every 15 min
    python -m tools.serve_live --every 10      # refresh every 10 min
    python -m tools.serve_live --host 127.0.0.1  # local only

Then open the printed http://<LAN-IP>:<port>/ URL on your phone (same Wi-Fi).
The web page itself re-reads the local data every 20 seconds, so it always shows
the newest data without extra network calls.

Note: 13F buy/sell data is quarterly and never changes intraday. Only prices and
news are refreshed on the interval; fetching faster than the SEC/Yahoo/Google
allow will get the IP rate-limited, so sub-minute source refresh is not supported.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

# Work from the repo root regardless of where this is launched from.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
os.chdir(_REPO)

from tools.build_site import serve, refresh_dynamic, write_site  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address. 0.0.0.0 allows phones on your Wi-Fi.")
    p.add_argument("--every", type=int, default=15,
                   help="Refresh prices + news every N minutes (min 5).")
    p.add_argument("--max-quotes", type=int, default=300)
    p.add_argument("--news-days", type=int, default=30)
    p.add_argument("--news-per-fund", type=int, default=8)
    args = p.parse_args()
    every = max(5, args.every)

    threading.Thread(target=serve, args=(args.port, args.host), daemon=True).start()
    print(f"Auto-refreshing prices + news every {every} min. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(every * 60)
            print("Refreshing prices + news...", flush=True)
            try:
                data = refresh_dynamic("config.yml", args.max_quotes, args.news_days,
                                       args.news_per_fund, True)
                write_site(data)
                print(f"  updated {data['generated']}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  refresh failed: {exc}", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
