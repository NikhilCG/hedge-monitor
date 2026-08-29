"""Simple JSON state store to detect changes between runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, data_dir: str | Path = "data") -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load(self, name: str, default: Any = None) -> Any:
        f = self._file(name)
        if not f.exists():
            return default
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def save(self, name: str, data: Any) -> None:
        self._file(name).write_text(
            json.dumps(data, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
