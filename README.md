# Hedge Monitor

Track institutional ("hedge fund") activity, stock news, and price moves using
**only public, legal data sources** -- then get notified.

## What it does

| Feature | Source | Frequency |
|---|---|---|
| Fund holdings + changes | SEC EDGAR **13F** filings | Quarterly (when filed) |
| Stock news matching | Public **RSS** feeds | On demand / scheduled |
| Daily price moves | Yahoo Finance quotes | Daily |
| Alerts | Desktop (notify-send) + email (SMTP) | On match |

### Reality check
- Hedge funds disclose US stock holdings **quarterly** via 13F filings -- that is
  the real, legal source. There is **no legal real-time feed** of hedge fund
  trades, and scraping private brokerage/social accounts is not supported.
- Everything uses official/public endpoints and respects SEC fair-access limits.

## Setup

```bash
cd ~/hedge-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yml config.yml
# Edit config.yml: set sec_user_agent to "Your Name your@email.com",
# then add funds (by CIK), tickers, and (optionally) email settings.
```

## Usage

```bash
python -m hedge_monitor holdings   # check 13F filings for changes
python -m hedge_monitor news       # scan RSS feeds for matches
python -m hedge_monitor prices     # check watched tickers for big moves
python -m hedge_monitor run        # all of the above, once
```

State is stored in `data/` so repeated runs only alert on *new* changes.
The first run establishes a baseline silently (no alerts).

## Schedule it (daily at 08:00)

`crontab -e`:

```
0 8 * * * cd ~/hedge-monitor && ./.venv/bin/python -m hedge_monitor run >> data/run.log 2>&1
```

## Find a fund's CIK
Search the manager name at https://www.sec.gov/cgi-bin/browse-edgar and copy the
CIK into config.yml.

## Email alerts
Use an app-specific password. Prefer the environment variable:

```bash
export HEDGE_SMTP_PASSWORD="your-app-password"
```
