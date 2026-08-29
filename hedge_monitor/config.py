"""Configuration loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    @property
    def sec_user_agent(self) -> str:
        ua = self.raw.get("sec_user_agent", "").strip()
        if not ua or ua.startswith("Your Name"):
            raise ValueError(
                "Set 'sec_user_agent' in config.yml to a real 'Name email' string. "
                "SEC EDGAR blocks requests without a valid contact."
            )
        return ua

    @property
    def funds(self) -> list[dict[str, str]]:
        return self.raw.get("funds", []) or []

    @property
    def watch_tickers(self) -> list[str]:
        return [t.upper() for t in (self.raw.get("watch_tickers", []) or [])]

    @property
    def price_move_threshold_pct(self) -> float:
        return float(self.raw.get("price_move_threshold_pct", 5.0))

    @property
    def news_keywords(self) -> list[str]:
        return [str(k) for k in (self.raw.get("news_keywords", []) or [])]

    @property
    def news_feeds(self) -> list[str]:
        return self.raw.get("news_feeds", []) or []

    @property
    def notify(self) -> dict[str, Any]:
        return self.raw.get("notify", {}) or {}

    @property
    def email_password(self) -> str:
        return os.environ.get("HEDGE_SMTP_PASSWORD") or (
            self.notify.get("email", {}).get("password", "")
        )


def load_config(path: str | os.PathLike[str]) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found: {p}. Copy config.example.yml to config.yml and edit it."
        )
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw=raw, path=p)
