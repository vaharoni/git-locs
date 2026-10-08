#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Script B: compute per-repo, per-month commit and line stats from local clones.

- repos.yaml must have no TODO entries.
- Missing `first_commit` dates are computed and written back to repos.yaml.
- One cache file per month lives in cache_dir; a month is reused when it is
  final (month end + lookback_months is in the past), was processed after that
  point, and covers exactly the repos that existed by that month.
- Finally a self-contained HTML report is written to html_out."""

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from ruamel.yaml import YAML

from common import add_months, load_config, month_bounds, month_range

NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")
BRACE_RENAME_RE = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def numstat_path(raw: str) -> str:
    """Destination path of a numstat entry; handles `a => b` and `dir/{a => b}/f` renames."""
    if "{" in raw and " => " in raw:
        return re.sub(r"/+", "/", BRACE_RENAME_RE.sub(lambda m: m.group(2), raw)).lstrip("/")
    return raw.split(" => ")[-1]


class FileFilter:
    def __init__(self, include_extensions: list[str], exclude_paths: list[str]):
        self.include = tuple(include_extensions)
        self.exclude = list(exclude_paths)

    def counts(self, path: str) -> bool:
        if self.include and not path.lower().endswith(self.include):
            return False
        return not any(fnmatch(path, pat) for pat in self.exclude)

    def fingerprint(self) -> dict:
        return {"include_extensions": list(self.include), "exclude_paths": self.exclude}


# ----------------------------------------------------------------------------- git

def git(path: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {path}:\n{res.stderr}")
    return res.stdout


class Repo:
    def __init__(self, name: str, spec: dict, base: Path):
        self.name = name
        self.path = Path(str(spec["path"])).expanduser()
        if not self.path.is_absolute():
            self.path = base / self.path
        self.branch = spec.get("branch") or "main"
        fc = spec.get("first_commit")
        self.first_commit: dt.date | None = fc if isinstance(fc, dt.date) else (
            dt.date.fromisoformat(str(fc)) if fc else None)
        self._fetched = False

    @property
    def ref(self) -> str:
        return f"origin/{self.branch}"

    def exists(self) -> bool:
        return (self.path / ".git").exists() or (self.path / "HEAD").exists()

    def fetch(self) -> None:
        if not self._fetched:
            print(f"  fetching {self.name} ({self.path})")
            git(self.path, "fetch", "--quiet", "origin", self.branch)
            self._fetched = True

    def author_args(self, emails: list[str]) -> list[str]:
        args = ["--no-merges", "--fixed-strings"]
        for e in emails:
            args += ["--author", e]
        return args

    def find_first_commit(self, emails: list[str]) -> dt.date | None:
        self.fetch()
        out = git(self.path, "log", self.ref, *self.author_args(emails), "--reverse", "--format=%cs")
        first = out.split("\n", 1)[0].strip()
        return dt.date.fromisoformat(first) if first else None

    def stats(self, emails: list[str], months: list[str], ffilter: "FileFilter") -> dict[str, dict]:
        """Return {month: {commits, added, removed}} for the given months."""
        self.fetch()
        start = month_bounds(months[0])[0] - dt.timedelta(days=2)
        end = month_bounds(months[-1])[1] + dt.timedelta(days=2)
        out = git(
            self.path, "log", self.ref, *self.author_args(emails),
            f"--since={start.isoformat()}", f"--until={end.isoformat()}",
            "--format=%x00%H %cs", "--numstat",
        )
        wanted = set(months)
        result = {m: {"commits": 0, "added": 0, "removed": 0} for m in months}
        current = None
        for line in out.splitlines():
            if line.startswith("\x00"):
                month = line[1:].split(" ", 1)[1][:7]
                current = result[month] if month in wanted else None
                if current is not None:
                    current["commits"] += 1
                continue
            if current is None:
                continue
            m = NUMSTAT_RE.match(line)
            if not m or m.group(1) == "-":
                continue  # not a numstat line, or a binary file
            if not ffilter.counts(numstat_path(m.group(3))):
                continue
            current["added"] += int(m.group(1))
            current["removed"] += int(m.group(2))
        return result


# --------------------------------------------------------------------------- repos.yaml

def load_repos(repos_path: Path, base: Path) -> tuple[YAML, dict, list[Repo]]:
    yaml = YAML()
    yaml.preserve_quotes = True
    doc = yaml.load(repos_path.read_text()) if repos_path.exists() else None
    entries = (doc or {}).get("repos") or {}
    todo = [n for n, v in entries.items() if not isinstance(v, dict) or not v.get("path")]
    if todo:
        sys.exit("repos.yaml still has entries without a local path:\n  " + "\n  ".join(todo)
                 + "\nReplace each with  {path: /local/folder}  (optional: branch, first_commit).")
    repos = [Repo(n, v, base) for n, v in entries.items()]
    return yaml, doc, repos


def ensure_first_commits(yaml: YAML, doc: dict, repos_path: Path, repos: list[Repo], emails: list[str]) -> None:
    missing = [r for r in repos if r.first_commit is None]
    if not missing:
        return
    print(f"Resolving first_commit for {len(missing)} repo(s)")
    absent = [r for r in missing if not r.exists()]
    if absent:
        sys.exit("Cannot resolve first_commit, local clone missing:\n  "
                 + "\n  ".join(f"{r.name}: {r.path}" for r in absent))
    for r in missing:
        fc = r.find_first_commit(emails)
        if fc is None:
            sys.exit(f"{r.name}: no commits by {emails} on {r.ref}. "
                     "Check author_emails in config.yaml or the branch in repos.yaml.")
        r.first_commit = fc
        doc["repos"][r.name]["first_commit"] = fc.isoformat()
        print(f"  {r.name}: {fc}")
    with repos_path.open("w") as fh:
        yaml.dump(doc, fh)


# -------------------------------------------------------------------------------- cache

def relevant_repos(repos: list[Repo], month: str) -> list[Repo]:
    end = month_bounds(month)[1]
    return [r for r in repos if r.first_commit is not None and r.first_commit <= end]


def final_after(month: str, lookback: int) -> dt.date:
    return add_months(month_bounds(month)[1], lookback)


def cache_valid(cache: dict | None, month: str, lookback: int, repos: list[Repo], ffilter: FileFilter) -> bool:
    if not cache or cache.get("filters") != ffilter.fingerprint():
        return False
    processed = dt.date.fromisoformat(cache["processed_at"][:10])
    if processed <= final_after(month, lookback):
        return False
    return set(cache["repos"]) == {r.name for r in repos}


# --------------------------------------------------------------------------------- html

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git LOC stats</title>
<style>
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#ddd; --head:#f4f4f4; --accent:#2563eb; --warn:#b45309; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111; --fg:#eee; --muted:#999; --line:#333; --head:#1c1c1c; --accent:#60a5fa; --warn:#fbbf24; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font: 14px/1.45 -apple-system, system-ui, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.controls { display:flex; flex-wrap:wrap; gap:16px 24px; align-items:flex-end; margin-bottom:8px; }
label { display:block; font-size:12px; color:var(--muted); margin-bottom:2px; }
select, button { font: inherit; color: var(--fg); background: var(--bg); border:1px solid var(--line); border-radius:6px; padding:4px 8px; }
button { cursor:pointer; }
.repos { display:flex; flex-wrap:wrap; gap:4px 12px; margin:8px 0 0; }
.repos label { display:inline-flex; gap:4px; align-items:center; font-size:13px; color:var(--fg); cursor:pointer; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { background: var(--head); cursor: pointer; user-select: none; position: sticky; top: 0; }
th.sorted::after { content: " ▾"; color: var(--accent); }
th.sorted.asc::after { content: " ▴"; }
tr.total td { font-weight: 600; border-top: 2px solid var(--line); }
.prov { color: var(--warn); font-size: 11px; margin-left: 4px; }
.wrap { overflow-x: auto; }
.num { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<main>
<h1>Git LOC stats</h1>
<p class="sub">Generated __GENERATED__ · commits authored by __EMAILS__ · merge commits excluded · changed = added + removed<br>__FILES__</p>

<div class="controls">
  <div><label>From</label><select id="from"></select></div>
  <div><label>To</label><select id="to"></select></div>
  <div><button id="all">All repos</button> <button id="none">No repos</button></div>
  <div><label><input type="checkbox" id="hideEmpty" checked> hide repos with no commits in range</label></div>
</div>
<div class="repos" id="repos"></div>

<h2>Per repo <span id="rangeLabel" class="sub"></span></h2>
<div class="wrap"><table id="byRepo"><thead></thead><tbody></tbody></table></div>

<h2>Per month <span class="sub">(selected repos)</span></h2>
<div class="wrap"><table id="byMonth"><thead></thead><tbody></tbody></table></div>

</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const COLS = ['commits', 'added', 'removed', 'changed'];
const fmt = n => n.toLocaleString();
const $ = id => document.getElementById(id);
const state = { from: DATA.months[0], to: DATA.months[DATA.months.length - 1],
                repos: new Set(DATA.repos), sort: { byRepo: ['changed', false], byMonth: ['month', true] } };

function fill(sel, value) {
  sel.innerHTML = DATA.months.map(m => `<option value="${m}">${m}</option>`).join('');
  sel.value = value;
}
fill($('from'), state.from); fill($('to'), state.to);
$('from').onchange = e => { state.from = e.target.value; if (state.to < state.from) { state.to = state.from; $('to').value = state.to; } render(); };
$('to').onchange = e => { state.to = e.target.value; if (state.from > state.to) { state.from = state.to; $('from').value = state.from; } render(); };
$('hideEmpty').onchange = render;
$('all').onclick = () => { state.repos = new Set(DATA.repos); renderRepoChecks(); render(); };
$('none').onclick = () => { state.repos = new Set(); renderRepoChecks(); render(); };

function renderRepoChecks() {
  $('repos').innerHTML = DATA.repos.map(r =>
    `<label><input type="checkbox" data-repo="${r}" ${state.repos.has(r) ? 'checked' : ''}> ${r}</label>`).join('');
  $('repos').querySelectorAll('input').forEach(cb => cb.onchange = e => {
    e.target.checked ? state.repos.add(e.target.dataset.repo) : state.repos.delete(e.target.dataset.repo);
    render();
  });
}

const zero = () => ({ commits: 0, added: 0, removed: 0, changed: 0 });
function addTo(acc, s) { acc.commits += s.commits; acc.added += s.added; acc.removed += s.removed; acc.changed += s.added + s.removed; }
const monthsInRange = () => DATA.months.filter(m => m >= state.from && m <= state.to);

function table(id, keyCol, rows, total, keyRender) {
  const [sortKey, asc] = state.sort[id];
  rows.sort((a, b) => (a[sortKey] > b[sortKey] ? 1 : a[sortKey] < b[sortKey] ? -1 : 0) * (asc ? 1 : -1));
  const t = $(id);
  t.querySelector('thead').innerHTML = '<tr>' + [keyCol, ...COLS].map(c =>
    `<th data-col="${c}" class="${c === sortKey ? 'sorted' + (asc ? ' asc' : '') : ''}">${c}</th>`).join('') + '</tr>';
  t.querySelector('tbody').innerHTML = rows.map(r =>
    `<tr><td>${keyRender(r)}</td>${COLS.map(c => `<td class="num">${fmt(r[c])}</td>`).join('')}</tr>`).join('')
    + `<tr class="total"><td>Total (${rows.length})</td>${COLS.map(c => `<td class="num">${fmt(total[c])}</td>`).join('')}</tr>`;
  t.querySelectorAll('th').forEach(th => th.onclick = () => {
    const col = th.dataset.col;
    state.sort[id] = [col, col === state.sort[id][0] ? !state.sort[id][1] : col === keyCol];
    render();
  });
}

function render() {
  const months = monthsInRange();
  $('rangeLabel').textContent = `(${state.from}${state.from !== state.to ? ' – ' + state.to : ''})`;

  const byRepo = DATA.repos.map(repo => {
    const acc = zero();
    months.forEach(m => { const s = DATA.stats[m][repo]; if (s) addTo(acc, s); });
    return { repo, ...acc };
  }).filter(r => !$('hideEmpty').checked || r.commits > 0);
  const totalRepo = zero();
  byRepo.forEach(r => { totalRepo.commits += r.commits; totalRepo.added += r.added; totalRepo.removed += r.removed; totalRepo.changed += r.changed; });
  table('byRepo', 'repo', byRepo, totalRepo, r => r.repo);

  const byMonth = months.map(month => {
    const acc = zero();
    state.repos.forEach(repo => { const s = DATA.stats[month][repo]; if (s) addTo(acc, s); });
    return { month, ...acc };
  });
  const totalMonth = zero();
  byMonth.forEach(r => { totalMonth.commits += r.commits; totalMonth.added += r.added; totalMonth.removed += r.removed; totalMonth.changed += r.changed; });
  table('byMonth', 'month', byMonth, totalMonth,
        r => r.month + (DATA.final[r.month] ? '' : '<span class="prov" title="not final yet; rebuilt on every run">provisional</span>'));
}
renderRepoChecks();
render();
</script>
</body>
</html>
"""


def write_html(out: Path, months: list[str], caches: dict[str, dict], repos: list[Repo],
               emails: list[str], lookback: int, ffilter: FileFilter) -> None:
    today = dt.date.today()
    data = {
        "months": months,
        "repos": sorted(r.name for r in repos),
        "final": {m: today > final_after(m, lookback) for m in months},
        "stats": {m: caches[m]["repos"] for m in months},
    }
    page = (HTML_TEMPLATE
            .replace("__GENERATED__", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__EMAILS__", html.escape(", ".join(emails)))
            .replace("__FILES__", html.escape(
                "counted extensions: " + (" ".join(ffilter.include) or "all")
                + (" · excluded paths: " + " ".join(ffilter.exclude) if ffilter.exclude else "")))
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/")))
    out.write_text(page)


# --------------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=Path(__file__).with_name("config.yaml"), type=Path)
    ap.add_argument("--force", action="store_true", help="ignore the cache and recompute every month")
    args = ap.parse_args()

    cfg, base = load_config(args.config)
    emails = [str(e) for e in cfg["author_emails"]]
    if not emails:
        sys.exit("config.yaml: author_emails is empty")
    ffilter = FileFilter(cfg["include_extensions"], [str(p) for p in cfg["exclude_paths"]])
    lookback = int(cfg["lookback_months"])
    cache_dir = base / cfg["cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)

    yaml, doc, repos = load_repos(base / cfg["repos_file"], base)
    if not repos:
        sys.exit("repos.yaml has no repos; run discover_repos.py first")
    ensure_first_commits(yaml, doc, base / cfg["repos_file"], repos, emails)

    months = month_range(cfg["months"]["from"], cfg["months"]["to"])
    caches: dict[str, dict] = {}
    todo: dict[str, list[Repo]] = {}
    for month in months:
        path = cache_dir / f"{month}.json"
        cached = json.loads(path.read_text()) if path.exists() else None
        rel = relevant_repos(repos, month)
        if not args.force and cache_valid(cached, month, lookback, rel, ffilter):
            caches[month] = cached
        else:
            todo[month] = rel

    print(f"{len(months)} month(s): {len(caches)} cached, {len(todo)} to compute")
    if todo:
        needed = {r.name: r for rel in todo.values() for r in rel}
        absent = [r for r in needed.values() if not r.exists()]
        if absent:
            lines = []
            for r in absent:
                ms = [m for m, rel in todo.items() if r in rel]
                lines.append(f"{r.name}: {r.path}  (needed for {ms[0]}..{ms[-1]})")
            sys.exit("Local clone missing for repos needed by non-final months:\n  " + "\n  ".join(lines))

        per_repo: dict[str, dict[str, dict]] = {}
        todo_months = sorted(todo)
        for r in needed.values():
            mine = [m for m in todo_months if r in todo[m]]
            per_repo[r.name] = r.stats(emails, mine, ffilter)

        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        for month in todo_months:
            entry = {"month": month, "processed_at": now,
                     "final_after": final_after(month, lookback).isoformat(),
                     "filters": ffilter.fingerprint(),
                     "repos": {r.name: per_repo[r.name][month] for r in todo[month]}}
            (cache_dir / f"{month}.json").write_text(json.dumps(entry, indent=2) + "\n")
            caches[month] = entry
            c = sum(v["commits"] for v in entry["repos"].values())
            a = sum(v["added"] for v in entry["repos"].values())
            d = sum(v["removed"] for v in entry["repos"].values())
            print(f"  {month}: {c} commits, +{a} -{d} across {len(entry['repos'])} repo(s)")

    out = base / cfg["html_out"]
    write_html(out, months, caches, repos, emails, lookback, ffilter)
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
