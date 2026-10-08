#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Script A: find every repo whose default branch has commits authored by you
in the configured month range and add the new ones to repos.yaml as TODO.

Existing entries in repos.yaml are never modified."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from ruamel.yaml import YAML

from common import load_config, month_range, month_bounds

SEARCH_LIMIT = 1000  # hard cap of the GitHub search API per query

HEADER = """\
Maintained by discover_repos.py (adds new repos as TODO) and collect_stats.py (fills first_commit).
Replace every TODO with a mapping. Only `path` is required:

  owner/name:
    path: ~/workspace/name       # local clone (absolute, ~, or relative to this file)
    branch: main                 # optional, default main; stats are read from origin/<branch>
    first_commit: '2025-10-17'   # optional, computed by collect_stats.py if missing
"""


def gh_search_month(month: str) -> set[str]:
    start, end = month_bounds(month)
    cmd = [
        "gh", "search", "commits",
        "--author=@me",
        f"--committer-date={start}..{end}",
        f"--limit={SEARCH_LIMIT}",
        "--json", "repository",
        "--jq", "[.[].repository.fullName]",
    ]
    for attempt in range(3):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            names = json.loads(res.stdout or "[]")
            if len(names) >= SEARCH_LIMIT:
                print(f"  WARNING: {month} hit the {SEARCH_LIMIT}-result search cap; "
                      "repos may be missing for this month", file=sys.stderr)
            return set(names)
        if "rate limit" in res.stderr.lower() or "429" in res.stderr:
            print(f"  rate limited, sleeping 60s (attempt {attempt + 1}/3)", file=sys.stderr)
            time.sleep(60)
            continue
        sys.exit(f"gh search failed for {month}:\n{res.stderr}")
    sys.exit(f"gh search kept failing for {month}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=Path(__file__).with_name("config.yaml"), type=Path)
    args = ap.parse_args()

    cfg, base = load_config(args.config)
    repos_path = base / cfg["repos_file"]

    yaml = YAML()
    yaml.preserve_quotes = True
    if repos_path.exists():
        doc = yaml.load(repos_path.read_text()) or {}
    else:
        doc = {}
    doc.setdefault("repos", {})
    if doc["repos"] is None:
        doc["repos"] = {}
    known = set(doc["repos"].keys())
    has_header = repos_path.exists() and repos_path.read_text().lstrip().startswith("# " + HEADER.splitlines()[0])
    if not has_header:
        doc.yaml_set_start_comment(HEADER)

    months = month_range(cfg["months"]["from"], cfg["months"]["to"])
    print(f"Searching {len(months)} month(s): {months[0]} .. {months[-1]}")

    found: dict[str, str] = {}  # repo -> first month seen
    for i, month in enumerate(months):
        names = gh_search_month(month)
        print(f"  {month}: {len(names)} repo(s)")
        for name in sorted(names):
            found.setdefault(name, month)
        if i < len(months) - 1:
            time.sleep(2)  # search API: 30 requests/min, each month may use up to 10 pages

    new = sorted(n for n in found if n not in known)
    for name in new:
        doc["repos"][name] = "TODO"
    if new or not has_header:
        with repos_path.open("w") as fh:
            yaml.dump(doc, fh)

    todo = sorted(n for n, v in doc["repos"].items() if v == "TODO" or v is None)
    print(f"\n{len(new)} new repo(s) added to {repos_path.name}: {', '.join(new) or '-'}")
    if todo:
        print("\nTODO: clone or locate these repos, then replace 'TODO' with  {path: /local/folder}:")
        for name in todo:
            print(f"  - {name}  (first seen {found.get(name, '?')})   gh repo clone {name}")
    else:
        print("All repos in repos.yaml have a local path.")


if __name__ == "__main__":
    main()
