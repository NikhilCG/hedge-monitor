"""SEC EDGAR client for 13F institutional holdings.

13F filings are the legal, public disclosure of US equity holdings by
institutional managers (hedge funds). They are filed quarterly, so this is
the correct source for "what hedge funds hold" -- there is no legal daily feed.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests

SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"


@dataclass
class Holding:
    name: str
    cusip: str
    value: int
    shares: int

    def key(self) -> str:
        return self.cusip


@dataclass
class Filing:
    cik: str
    accession: str
    filing_date: str
    form: str


class EdgarClient:
    def __init__(self, user_agent: str, min_interval: float = 0.2) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._min_interval = min_interval
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _get(self, url: str) -> requests.Response:
        self._throttle()
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        return resp

    @staticmethod
    def _pad_cik(cik: str) -> str:
        return str(cik).lstrip("0").zfill(10)

    def search_cik(self, query: str) -> tuple[str, str] | None:
        """Resolve a manager name to (cik, conformed_name) via EDGAR company search.

        Matches 13F filers only. SEC does a case-insensitive prefix match on the
        conformed company name, so pass a leading substring of the real name.
        Returns None if no 13F filer matches.
        """
        from urllib.parse import quote

        url = (
            f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcompany"
            f"&company={quote(query)}&type=13F-HR&dateb=&owner=include"
            f"&count=10&output=atom"
        )
        text = self._get(url).text
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        cik = ""
        name = query
        for el in root.iter():
            if local(el.tag) == "cik" and (el.text or "").strip():
                cik = (el.text or "").strip()
            elif local(el.tag) == "conformed-name" and (el.text or "").strip():
                name = (el.text or "").strip()
            if cik:
                break
        if not cik:
            return None
        return self._pad_cik(cik), name

    def latest_13f(self, cik: str) -> Filing | None:
        padded = self._pad_cik(cik)
        url = f"{SEC_DATA}/submissions/CIK{padded}.json"
        data: dict[str, Any] = self._get(url).json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        for form, acc, date in zip(forms, accessions, dates):
            if form.startswith("13F-HR"):
                return Filing(cik=padded, accession=acc, filing_date=date, form=form)
        return None

    def recent_13f(self, cik: str, limit: int = 2) -> list[Filing]:
        """Return up to `limit` most recent 13F-HR filings, newest first."""
        padded = self._pad_cik(cik)
        url = f"{SEC_DATA}/submissions/CIK{padded}.json"
        data: dict[str, Any] = self._get(url).json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        out: list[Filing] = []
        for form, acc, date in zip(forms, accessions, dates):
            if form.startswith("13F-HR"):
                out.append(Filing(cik=padded, accession=acc, filing_date=date, form=form))
                if len(out) >= limit:
                    break
        return out

    def holdings(self, filing: Filing) -> list[Holding]:
        acc_nodash = filing.accession.replace("-", "")
        cik_int = str(int(filing.cik))
        base = f"{SEC_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}"
        index = self._get(f"{base}/index.json").json()

        info_table_name = None
        for item in index.get("directory", {}).get("item", []):
            fname = item.get("name", "")
            low = fname.lower()
            if low.endswith(".xml") and (
                "form13f" not in low and "primary_doc" not in low
            ):
                info_table_name = fname
                break
        if info_table_name is None:
            return []

        xml_text = self._get(f"{base}/{info_table_name}").text
        return self._parse_info_table(xml_text)

    @staticmethod
    def _parse_info_table(xml_text: str) -> list[Holding]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        holdings: list[Holding] = []
        for el in root.iter():
            if local(el.tag) != "infoTable":
                continue
            fields: dict[str, str] = {}
            for child in el.iter():
                fields[local(child.tag)] = (child.text or "").strip()
            try:
                holdings.append(
                    Holding(
                        name=fields.get("nameOfIssuer", "?"),
                        cusip=fields.get("cusip", ""),
                        value=int(float(fields.get("value", "0") or 0)),
                        shares=int(float(fields.get("sshPrnamt", "0") or 0)),
                    )
                )
            except (ValueError, TypeError):
                continue
        return holdings


def diff_holdings(
    old: list[dict[str, Any]], new: list[Holding]
) -> dict[str, list[dict[str, Any]]]:
    old_by = {h["cusip"]: h for h in old if h.get("cusip")}
    new_by = {h.cusip: h for h in new if h.cusip}

    added, removed, increased, decreased = [], [], [], []

    for cusip, h in new_by.items():
        if cusip not in old_by:
            added.append({"name": h.name, "cusip": cusip, "shares": h.shares})
        else:
            prev = int(old_by[cusip].get("shares", 0))
            if h.shares > prev:
                increased.append(
                    {"name": h.name, "cusip": cusip, "from": prev, "to": h.shares}
                )
            elif h.shares < prev:
                decreased.append(
                    {"name": h.name, "cusip": cusip, "from": prev, "to": h.shares}
                )

    for cusip, h in old_by.items():
        if cusip not in new_by:
            removed.append(
                {"name": h.get("name", "?"), "cusip": cusip, "shares": h.get("shares", 0)}
            )

    return {
        "added": added,
        "removed": removed,
        "increased": increased,
        "decreased": decreased,
    }
