"""Helpers shared by discover_repos.py and collect_stats.py."""

import calendar
import datetime as dt
from pathlib import Path

from ruamel.yaml import YAML


def load_config(path: Path) -> tuple[dict, Path]:
    if not path.exists():
        example = path.with_name("config.example.yaml")
        raise SystemExit(f"{path} not found. Create it from the example:\n  cp {example} {path}")
    cfg = YAML(typ="safe").load(path.read_text())
    cfg.setdefault("months", {})
    cfg["months"].setdefault("from", dt.date.today().strftime("%Y-%m"))
    cfg["months"].setdefault("to", None)
    cfg.setdefault("lookback_months", 2)
    cfg.setdefault("author_emails", [])
    cfg.setdefault("exclude_paths", [])
    cfg["include_extensions"] = sorted({
        (e if str(e).startswith(".") else f".{e}").lower()
        for e in (cfg.get("include_extensions") or []) for e in [str(e)]})
    cfg.setdefault("repos_file", "repos.yaml")
    cfg.setdefault("cache_dir", "cache")
    cfg.setdefault("html_out", "stats.html")
    return cfg, path.resolve().parent


def parse_month(s) -> tuple[int, int]:
    if isinstance(s, dt.date):
        return s.year, s.month
    y, m = str(s).split("-")[:2]
    return int(y), int(m)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    y, m = parse_month(month)
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_range(start, end=None) -> list[str]:
    y, m = parse_month(start)
    if end is None:
        today = dt.date.today()
        ey, em = today.year, today.month
    else:
        ey, em = parse_month(end)
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out
