# NXP Zephyr Reports

Static portal for three automatically refreshable Zephyr reports:

- **Device PM by Board**: `reports/device-pm/index.html`, plus `boards.csv`,
  `drivers.csv`, `boards.json` and `summary.json` beside it. For each NXP board
  it lists the IPs its devicetree describes and whether the driver behind each
  one implements Device PM, with the detected `PM_DEVICE_ACTION_*` transitions.
- **Vendor Report**: `reports/vendor/index.html`, plus `vendors.csv` and
  `vendors.json`. Median/mean/P90 days-to-merge per silicon vendor with
  bootstrap confidence intervals, merge rate, open backlog and a quarterly
  trend, with NXP highlighted against the all-vendor baseline.
- **Commit Statistics**: `reports/commit-statistics/index.html`. Pick a range
  (the whole branch history, the latest one through ten stable releases, or a
  rolling week, month, three months, six months or year) and a ranking:
  individual authors (mailmap-normalized), NXP authors only (`@*.nxp.com`), or
  organizations (from the first `Signed-off-by` email domain, falling back to
  the author's). Each view is downloadable as CSV.

The published site lives at a persistent GitHub Pages URL:

`https://zhaoxiangjin.github.io/zephyr-data/`

## Reports are not stored in this repository

`reports/` is git-ignored. The reports are derived data -- every number in them
is recomputed from the Zephyr history or the GitHub API, and nothing ever read a
previous version -- so committing them only bought a few hundred thousand lines
of churn per refresh. `build-and-deploy.yml` regenerates everything into
`_site/` and hands that directly to Pages, so there is one workflow, no bot
commits, and the workflow needs no write access to the repository.

It runs on every push to `main`, daily at 16:17 UTC (00:17 China time), and on
manual dispatch. `_site/` mirrors the layout this repository used to have, so published
URLs did not change.

Two consequences worth knowing:

- A homepage-only edit still triggers a full regeneration, because the site is
  built rather than checked out. With a warm vendor cache that is a few minutes.
- The vendor API snapshot in `actions/cache` is now the only state that survives
  between runs. GitHub evicts cache entries untouched for 7 days, so a run after
  a longer gap pays a cold fetch of tens of minutes. The fetch is resumable and
  the cache is saved even when a run fails.

If a run fails nothing is deployed, and Pages keeps serving the last successful
build.

## Running the generators locally

They write into `reports/` by default, which is ignored, so a local run never
dirties the tree:

Set `ZEPHYR_PATH` when generating Device PM reports outside this workstation:

```powershell
python tools/device-pm/device_pm.py --repo C:\path\to\zephyr

# statistics only, nothing written
python tools/device-pm/device_pm.py --repo C:\path\to\zephyr --show
python tools/device-pm/device_pm.py --repo C:\path\to\zephyr --show frdm_mcxn947

python tools/device-pm/test_device_pm.py
```

Device PM analysis reads the working tree, not history, so a shallow Zephyr
clone is fine here.

The vendor report needs GitHub API access; the fetch is rate limited and takes
tens of minutes on a cold cache, so the cache is per label and resumable:

```powershell
$env:GH_VALID_TOKEN = "<token with public repo read access>"
python tools/vendor-report/vendor_report.py

# rebuild from the existing cache, no network
python tools/vendor-report/vendor_report.py --no-fetch

# print the table, write nothing
python tools/vendor-report/vendor_report.py --no-fetch --show

python tools/vendor-report/test_vendor_report.py
```

To generate commit statistics locally, provide a Zephyr Git checkout with full
history (not a shallow clone — the generator refuses one, because history-wide
and release ranges would be wrong):

```powershell
# regenerate every artifact plus the dashboard
python tools/commit-statistics/commit_statistics.py --repo C:\path\to\zephyr

# ad-hoc lookup printed to the terminal, nothing written
python tools/commit-statistics/commit_statistics.py --repo C:\path\to\zephyr --show all --type nxp --top 20
```

`commit_statistics.py` is the only generator: it loads commit metadata in a
single `git log` pass, then derives every range and ranking from that one pass
and writes `ranges/*.{json,csv}`, `manifest.json` and `index.html` together, so
the dashboard never advertises a dropdown entry without data behind it.

Each generator has a test suite beside it. None needs the network or a Zephyr
checkout -- they build synthetic inputs with known expected results -- and all
three run in CI before anything is generated:

```powershell
python tools/commit-statistics/test_commit_statistics.py
python tools/device-pm/test_device_pm.py
python tools/vendor-report/test_vendor_report.py
```

## One-time repository setup

Both steps need a human with admin rights; neither can be done from a workflow.

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.** Without
   this the deploy fails and the site returns 404. It cannot be automated:
   creating a Pages site over the API needs `administration: write`, which
   `GITHUB_TOKEN` cannot be granted, so `configure-pages` with
   `enablement: true` fails with *Resource not accessible by integration*.
2. **Add a `DASHBOARD_GITHUB_TOKEN` secret** with read access to
   `zephyrproject-rtos/zephyr`. It raises the GitHub Search API rate limit for
   the vendor fetch — without it the fetch still works, but roughly three times
   slower.
