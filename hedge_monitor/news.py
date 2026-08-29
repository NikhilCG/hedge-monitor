"""News monitoring via RSS feeds (free, no API key)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import feedparser
import requests


@dataclass
class NewsItem:
    title: str
    link: str
    published: str
    source: str

    @property
    def id(self) -> str:
        return hashlib.sha1(self.link.encode("utf-8")).hexdigest()[:16]


def fetch_feed(url: str, timeout: int = 20) -> list[NewsItem]:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    source = parsed.feed.get("title", url) if parsed.feed else url
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        items.append(
            NewsItem(
                title=entry.get("title", "(no title)"),
                link=link,
                published=entry.get("published", entry.get("updated", "")),
                source=source,
            )
        )
    return items


def matches(item: NewsItem, terms: list[str]) -> list[str]:
    text = item.title.lower()
    hits = []
    for term in terms:
        t = term.strip().lower()
        if t and t in text:
            hits.append(term)
    return hits
