# git-locs

Per-repo, per-month commit and line-of-code stats for everything you authored on GitHub.

## Files

| File | Role |
| --- | --- |
| `config.yaml` | Not committed. Copy from `config.example.yaml`. Month range, lookback, author emails, path excludes, file locations |
| `repos.yaml` | Not committed. Created by `discover_repos.py`; see `repos.example.yaml`. One entry per repo: local `path`, optional `branch` (default `main`), `first_commit` (filled in automatically) |
| `discover_repos.py` | Script A: queries GitHub month by month and appends unknown repos to `repos.yaml` as `TODO` |
| `collect_stats.py` | Script B: fetches each local clone, computes stats, maintains `cache/<month>.json`, writes `stats.html` |
| `common.py` | Shared helpers |

## Workflow

```sh
cp config.example.yaml config.yaml   # then set months and author_emails
uv run discover_repos.py          # needs an authenticated gh
# edit repos.yaml: replace each TODO with  {path: ~/some/clone}
uv run collect_stats.py           # writes cache/*.json and stats.html
open stats.html
```

`collect_stats.py --force` recomputes every month regardless of the cache.

## repos.yaml shape

```yaml
repos:
  octocat/hello-world:
    path: ~/workspace/hello-world  # required: local clone (absolute, ~, or relative to repos.yaml)
    branch: main                   # optional, default main; stats are read from origin/<branch>
    first_commit: '2025-10-17'     # optional; collect_stats.py computes and writes it
  octocat/spoon-knife: TODO        # written by discover_repos.py; collect_stats.py refuses to run until replaced
```

## Rules

- Only the default branch is searched (GitHub commit search limitation) and only `origin/<branch>` is read locally.
- Commits are matched by author email from `author_emails`. Merge commits are not counted.
- Changed = added + removed. Only files whose extension is in `include_extensions` count, minus paths matching
  `exclude_paths`. Binary files contribute no lines.
- Each cache file records the filters it was built with; changing either list rebuilds those months on the next run.
- Month bucketing uses the committer date.
- A month is final once `lookback_months` have passed since its end. Final months are served from cache when
  the cache was built after that point and covers exactly the repos whose `first_commit` is on or before the month end.
  Non-final months are rebuilt on every run, so their clones must be present. Once every month a repo appears
  in is final, its local clone can be deleted.
