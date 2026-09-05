#!/usr/bin/env python3
"""Zephyr commit-statistics dashboard generator.

Builds contribution rankings for the Zephyr git history across three
independent dimensions:

  * range   -- the whole history, the latest N stable releases, or a rolling
               time window measured back from the head commit.
  * ranking -- per author, per author restricted to @*.nxp.com addresses, or
               per organization derived from Signed-off-by email domains.
  * output  -- per range/ranking CSV + JSON artifacts plus the static HTML
               dashboard that reads them.

The whole matrix is computed from a single `git log` pass: commit metadata is
loaded once, then every range is a membership test over that list. Only the
release ranges need extra git calls (one `rev-list` each to resolve exact
`base..head` membership, which a date comparison cannot do on a branchy
history).

Typical use:

  # regenerate every artifact plus the dashboard (what CI runs)
  python commit_statistics.py --repo path/to/zephyr

  # ad-hoc lookup printed to the terminal, nothing written
  python commit_statistics.py --repo path/to/zephyr --show all --type nxp --top 20
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Stable Zephyr releases. Patch releases live on release branches and are not
# ancestors of main, so `--merged <head>` leaves just the minor releases.
RELEASE_TAG_REGEX = re.compile(r"^(?:zephyr-)?v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")

# nxp.com plus any subdomain of it: alice@nxp.com, bob@corp.nxp.com.
NXP_EMAIL_REGEX = re.compile(r"^[^@]+@(?:[\w-]+\.)*nxp\.com$", re.IGNORECASE)

SIGNOFF_REGEX = re.compile(r"^Signed-off-by:\s*.+?\s*<(?P<email>[^>]+)>\s*$", re.IGNORECASE)

# Domains whose organization name the "first label of the domain" heuristic
# gets wrong.
DOMAIN_TO_ORG = {
    "nxp.com": "@nxp",
    "nordicsemi.no": "@nordicsemi",
    "linaro.org": "@linaro",
    "baylibre.com": "@baylibre",
    "google.com": "@google",
    "googlemail.com": "@google",
    "zephyrproject.org": "@zephyrproject",
    "linuxfoundation.org": "@linuxfoundation",
    "weidmueller.com": "@weidmueller",
    "bytesatwork.ch": "@bytesatwork",
    "gmail.com": "@independent",
    "outlook.com": "@independent",
    "hotmail.com": "@independent",
    "protonmail.com": "@independent",
    "proton.me": "@independent",
    "users.noreply.github.com": "@independent",
}

KNOWN_ORGS = frozenset(DOMAIN_TO_ORG.values())

RELEASE_RANGE_COUNT = 10

# Rolling windows, as (id, label, months, days). Anchored on the head commit
# date rather than wall-clock time so a rebuild of an unchanged history
# reproduces the same numbers.
TIME_WINDOWS = [
    ("week", "Last week", 0, 7),
    ("month", "Last month", 1, 0),
    ("quarter", "Last 3 months", 3, 0),
    ("half-year", "Last 6 months", 6, 0),
    ("year", "Last year", 12, 0),
]


@dataclass(frozen=True)
class RankingType:
    id: str
    label: str
    key: str  # field name used for the entry in generated JSON/CSV
    heading: str  # table column label in the dashboard


RANKING_TYPES = [
    RankingType("individual", "Individual commits", "author", "Author"),
    RankingType("nxp", "NXP authors only", "author", "Author (@*.nxp.com)"),
    RankingType("company", "Organization commits", "company", "Organization"),
]


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str  # mailmap-resolved "Name <email>"
    author_email: str  # mailmap-resolved
    committed_at: datetime
    signoff_emails: tuple[str, ...]


@dataclass
class Range:
    id: str
    label: str
    description: str
    commits: list[Commit]
    base_label: str
    base_date: Optional[str]


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def git(repo: Path, args: list[str]) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.check_output(
            command, text=True, encoding="utf-8", errors="replace", stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        raise SystemExit("error: 'git' not found on PATH")
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"error: {' '.join(command)}\n{error.stderr.strip()}")


def assert_usable_repo(repo: Path, head: str) -> None:
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise SystemExit(f"error: {repo} is not a git repository")
    if git(repo, ["rev-parse", "--is-shallow-repository"]).strip() == "true":
        raise SystemExit(
            f"error: {repo} is a shallow clone, so history-wide and release ranges would be "
            "wrong. Run 'git fetch --unshallow' (CI uses actions/checkout with fetch-depth: 0)."
        )
    git(repo, ["rev-parse", "--verify", f"{head}^{{commit}}"])


def stream_commits(repo: Path, head: str) -> Iterator[Commit]:
    """Yield every commit reachable from head, newest first, in one git pass.

    Records are separated by \\x1e and fields by \\x1f, which cannot appear in
    git metadata, so commit messages containing newlines parse unambiguously.
    The output is streamed because the full Zephyr history is on the order of
    100 MB of text.
    """

    command = [
        "git",
        "-C",
        str(repo),
        "log",
        "--use-mailmap",
        head,
        "--format=%H%x1f%aN%x1f%aE%x1f%cI%x1f%B%x1e",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1024 * 1024,
    )
    assert process.stdout is not None

    pending = ""
    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        pending += chunk
        records = pending.split("\x1e")
        pending = records.pop()
        for record in records:
            commit = _parse_commit(record)
            if commit is not None:
                yield commit

    commit = _parse_commit(pending)
    if commit is not None:
        yield commit

    stderr = process.stderr.read() if process.stderr else ""
    if process.wait() != 0:
        raise SystemExit(f"error: git log failed\n{stderr.strip()}")


def _parse_commit(record: str) -> Optional[Commit]:
    fields = record.lstrip("\n").split("\x1f")
    if len(fields) < 5:
        return None

    sha, name, email, committed, body = fields[0], fields[1], fields[2], fields[3], fields[4]
    sha = sha.strip()
    if not sha:
        return None

    signoffs = []
    for line in body.splitlines():
        match = SIGNOFF_REGEX.match(line.strip())
        if match:
            signoffs.append(match.group("email").strip())

    return Commit(
        sha=sha,
        author=f"{name.strip()} <{email.strip()}>",
        author_email=email.strip(),
        committed_at=datetime.fromisoformat(committed.strip()),
        signoff_emails=tuple(signoffs),
    )


def release_tags(repo: Path, head: str) -> list[tuple[str, str]]:
    """Stable release tags reachable from head, newest first, as (tag, commit)."""

    out = git(
        repo,
        [
            "for-each-ref",
            "refs/tags",
            "--merged",
            head,
            "--format=%(refname:short)%09%(objectname)%09%(*objectname)",
        ],
    )

    tags: list[tuple[tuple[int, int, int], str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, objectname = parts[0], parts[1]
        peeled = parts[2] if len(parts) > 2 else ""
        match = RELEASE_TAG_REGEX.match(name)
        if not match:
            continue
        version = (int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
        # Annotated tags expose the commit via *objectname; lightweight tags
        # point at the commit directly.
        tags.append((version, name, peeled or objectname))

    tags.sort(reverse=True)
    return [(name, commit) for _, name, commit in tags]


def commits_in_range(repo: Path, base: str, head: str) -> set[str]:
    return set(git(repo, ["rev-list", f"{base}..{head}"]).split())


# --------------------------------------------------------------------------
# Range construction
# --------------------------------------------------------------------------


def _shift(moment: datetime, months: int, days: int) -> datetime:
    if months:
        index = moment.month - 1 - months
        year = moment.year + index // 12
        month = index % 12 + 1
        moment = moment.replace(
            year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1])
        )
    if days:
        moment = moment - timedelta(days=days)
    return moment


def build_ranges(repo: Path, head: str, commits: list[Commit]) -> list[Range]:
    if not commits:
        raise SystemExit(f"error: no commits reachable from {head}")

    head_date = commits[0].committed_at
    oldest_date = min(commit.committed_at for commit in commits)

    ranges = [
        Range(
            id="all",
            label="All history",
            description="Every commit in the branch history, with no base revision.",
            commits=commits,
            base_label="(root)",
            base_date=oldest_date.isoformat(),
        )
    ]

    tags = release_tags(repo, head)
    if len(tags) < RELEASE_RANGE_COUNT:
        raise SystemExit(
            f"error: only {len(tags)} stable release tags are reachable from {head}, "
            f"need {RELEASE_RANGE_COUNT}. Fetch tags with 'git fetch --tags'."
        )

    by_sha = {commit.sha: commit for commit in commits}
    for count in range(1, RELEASE_RANGE_COUNT + 1):
        tag, tag_commit = tags[count - 1]
        members = commits_in_range(repo, tag_commit, head)
        plural = "s" if count > 1 else ""
        ranges.append(
            Range(
                id=f"release-{count}",
                label=f"Latest {count} release{plural}",
                description=f"Commits since the {count} most recent stable Zephyr release{plural} ({tag}).",
                commits=[commit for commit in commits if commit.sha in members],
                base_label=tag,
                base_date=(
                    by_sha[tag_commit].committed_at.isoformat() if tag_commit in by_sha else None
                ),
            )
        )

    for window_id, label, months, days in TIME_WINDOWS:
        cutoff = _shift(head_date, months, days)
        window = label[len("Last ") :] if label.startswith("Last ") else label
        ranges.append(
            Range(
                id=window_id,
                label=label,
                description=f"Commits that landed in the {window} before "
                f"{head_date.date().isoformat()}.",
                commits=[commit for commit in commits if commit.committed_at > cutoff],
                base_label=cutoff.date().isoformat(),
                base_date=cutoff.isoformat(),
            )
        )

    return ranges


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def organization_of(email: str) -> Optional[str]:
    """Map an email address to an organization key.

    Known domains match exactly or as a true subdomain, so 'corp.nxp.com'
    resolves like 'nxp.com'. Everything else falls back to the first label of
    the domain, except where that would silently merge a lookalike domain
    ('nxp.com.mx', 'nxp.example.test') into a known organization -- those keep
    their full domain so the organization ranking stays consistent with the
    per-author NXP ranking, which uses NXP_EMAIL_REGEX.
    """

    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower().strip(".")
    if not domain:
        return None

    for suffix, organization in DOMAIN_TO_ORG.items():
        if domain == suffix or domain.endswith("." + suffix):
            return organization

    candidate = f"@{domain.split('.', 1)[0]}"
    return f"@{domain}" if candidate in KNOWN_ORGS else candidate


def count_commits(commits: list[Commit], ranking_type: str) -> Counter:
    counts: Counter = Counter()

    if ranking_type == "individual":
        for commit in commits:
            counts[commit.author] += 1
    elif ranking_type == "nxp":
        for commit in commits:
            if NXP_EMAIL_REGEX.match(commit.author_email):
                counts[commit.author] += 1
    elif ranking_type == "company":
        for commit in commits:
            # The first Signed-off-by is the originator of the patch; commits
            # with no trailer fall back to the author's own address.
            email = commit.signoff_emails[0] if commit.signoff_emails else commit.author_email
            organization = organization_of(email)
            if organization:
                counts[organization] += 1
    else:
        raise ValueError(f"unknown ranking type: {ranking_type}")

    return counts


def rank(counts: Counter) -> list[dict]:
    """Ordered entries with competition ranking (equal counts share a rank)."""

    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))

    entries: list[dict] = []
    previous_commits: Optional[int] = None
    previous_rank = 0
    for index, (name, commits) in enumerate(ordered, start=1):
        position = previous_rank if commits == previous_commits else index
        entries.append(
            {
                "rank": position,
                "name": name,
                "commits": commits,
                "share": round(commits / total * 100, 2) if total else 0.0,
            }
        )
        previous_commits, previous_rank = commits, position

    return entries


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_artifacts(
    output_dir: Path, repo: Path, head: str, ranges: list[Range], generated_at: str
) -> None:
    ranges_dir = output_dir / "ranges"
    ranges_dir.mkdir(parents=True, exist_ok=True)

    for entry in ranges:
        for ranking_type in RANKING_TYPES:
            entries = rank(count_commits(entry.commits, ranking_type.id))
            stem = ranges_dir / f"{entry.id}-{ranking_type.id}"

            with (stem.with_suffix(".csv")).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["rank", ranking_type.key, "commits", "share_percent"])
                for item in entries:
                    writer.writerow([item["rank"], item["name"], item["commits"], item["share"]])

            payload = {
                "meta": {
                    "repo": str(repo),
                    "head": head,
                    "range": entry.id,
                    "range_label": entry.label,
                    "type": ranking_type.id,
                    "base_label": entry.base_label,
                    "base_date": entry.base_date,
                    "commits_in_range": len(entry.commits),
                    "commits_counted": sum(item["commits"] for item in entries),
                    "contributors": len(entries),
                    "generated_at": generated_at,
                },
                "ranking": [
                    {
                        "rank": item["rank"],
                        ranking_type.key: item["name"],
                        "commits": item["commits"],
                        "share": item["share"],
                    }
                    for item in entries
                ],
            }
            stem.with_suffix(".json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "head": head,
                "ranges": [
                    {
                        "id": entry.id,
                        "label": entry.label,
                        "description": entry.description,
                        "commits": len(entry.commits),
                    }
                    for entry in ranges
                ],
                "ranking_types": [
                    {"id": t.id, "label": t.label, "key": t.key, "heading": t.heading}
                    for t in RANKING_TYPES
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    (output_dir / "index.html").write_text(DASHBOARD_HTML, encoding="utf-8")


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zephyr Commit Statistics</title><style>
:root{--navy:#173f73;--blue:#4472c4;--page:#f5f7fa;--line:#d9e0ea;--ink:#172033;--muted:#667085}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 Calibri,Arial,sans-serif}
header{background:var(--navy);color:#fff;padding:32px max(24px,calc((100% - 1200px)/2))}h1{margin:0 0 6px;font-size:29px}header p{margin:0;color:#dbeafe}
main{max-width:1200px;margin:auto;padding:26px 24px 48px}.panel{background:#fff;border:1px solid var(--line);border-radius:9px;padding:20px;box-shadow:0 1px 2px #1018280d}
.controls{display:grid;gap:16px;grid-template-columns:1fr 2fr 1fr;margin-bottom:18px}.field{display:grid;gap:5px;font-weight:bold}.field span{font-size:12px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
select{background:#fff;border:1px solid #aebdce;border-radius:5px;color:var(--ink);font:inherit;padding:9px}.meta{color:var(--muted);margin:0 0 15px}.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:15px}
a.button{background:var(--blue);border-radius:5px;color:#fff;font-weight:bold;padding:9px 13px;text-decoration:none}.table-wrap{max-height:70vh;overflow:auto}table{border-collapse:collapse;width:100%}th{background:#edf3fa;position:sticky;top:0;text-align:left}th,td{padding:8px 12px;border-bottom:1px solid #edf0f4}td:first-child,td:nth-child(3),td:last-child{text-align:right}.error{color:#b42318;font-weight:bold}@media(max-width:700px){.controls{grid-template-columns:1fr}header{padding-top:26px;padding-bottom:26px}}
</style></head><body><header><h1>Zephyr Commit Statistics</h1><p>Contribution rankings by individual, NXP author and organization, over the whole history, recent releases or a rolling window.</p></header>
<main><section class="panel"><div class="controls"><label class="field"><span>Ranking</span><select id="type"></select></label><label class="field"><span>Range</span><select id="range"></select></label><label class="field"><span>Rows</span><select id="rows"><option value="20">Top 20</option><option value="50" selected>Top 50</option><option value="100">Top 100</option><option value="0">All</option></select></label></div><p class="meta" id="meta">Loading report data&hellip;</p><div class="actions"><a class="button" id="download" href="#">Download selected CSV</a></div><div class="table-wrap"><table><thead><tr><th>Rank</th><th id="name-heading">Author</th><th>Commits</th><th>Share</th></tr></thead><tbody id="results"></tbody></table></div></section></main>
<script>
const type = document.querySelector("#type"), range = document.querySelector("#range"), rows = document.querySelector("#rows");
const meta = document.querySelector("#meta"), results = document.querySelector("#results");
const download = document.querySelector("#download"), heading = document.querySelector("#name-heading");
let ranges = [], rankingTypes = [];

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value === undefined || value === null ? "" : value;
  return element.innerHTML;
}

async function refresh() {
  const limit = Number(rows.value);
  // Fall back to the first entry: a stale selection must not throw before the
  // try block and leave the page dead with no message.
  const selectedRange = ranges.find(item => item.id === range.value) || ranges[0];
  const selectedType = rankingTypes.find(item => item.id === type.value) || rankingTypes[0];
  const basePath = `ranges/${selectedRange.id}-${selectedType.id}`;
  download.href = `${basePath}.csv`;
  download.download = `${selectedRange.id}-${selectedType.id}.csv`;
  heading.textContent = selectedType.heading;
  meta.textContent = "Loading report data\\u2026";
  results.innerHTML = "";
  try {
    const data = await (await fetch(`${basePath}.json`, {cache: "no-cache"})).json();
    const ranking = limit ? data.ranking.slice(0, limit) : data.ranking;
    const key = selectedType.key;
    meta.textContent = `${selectedRange.description} Git range: ${data.meta.base_label}..${data.meta.head}. `
      + `${data.meta.commits_counted} of ${data.meta.commits_in_range} commits in range, `
      + `${data.meta.contributors} entries. Generated: ${data.meta.generated_at}.`;
    results.innerHTML = ranking.map(entry =>
      `<tr><td>${entry.rank}</td><td>${escapeHtml(entry[key])}</td><td>${entry.commits}</td><td>${entry.share}%</td></tr>`
    ).join("");
  } catch (error) {
    meta.innerHTML = '<span class="error">Unable to load the selected ranking.</span>';
  }
}

fetch("manifest.json", {cache: "no-cache"}).then(response => response.json()).then(data => {
  ranges = data.ranges || [];
  rankingTypes = data.ranking_types || [];
  if (!ranges.length || !rankingTypes.length) throw new Error("incomplete manifest");
  type.innerHTML = rankingTypes.map(item => `<option value="${item.id}">${item.label}</option>`).join("");
  range.innerHTML = ranges.map(item => `<option value="${item.id}">${item.label}</option>`).join("");
  refresh();
}).catch(() => { meta.innerHTML = '<span class="error">Unable to load the report manifest.</span>'; });

[type, range, rows].forEach(control => control.addEventListener("change", refresh));
</script></body></html>"""


def print_ranking(entry: Range, ranking_type: RankingType, top: Optional[int]) -> None:
    entries = rank(count_commits(entry.commits, ranking_type.id))
    print(f"Range:   {entry.label} ({entry.base_label}..HEAD)")
    print(f"Ranking: {ranking_type.label}")
    print(f"Commits: {sum(item['commits'] for item in entries)} of {len(entry.commits)} in range")
    print()

    shown = entries[:top] if top else entries
    if not shown:
        print("No commits matched.")
        return

    name_width = max(len(ranking_type.heading), max(len(item["name"]) for item in shown))
    print(f"{'Rank':>5}  {'Commits':>7}  {'Share':>6}  {ranking_type.heading:<{name_width}}")
    print("-" * (5 + 2 + 7 + 2 + 6 + 2 + name_width))
    for item in shown:
        print(
            f"{item['rank']:>5}  {item['commits']:>7}  {item['share']:>5.1f}%  "
            f"{item['name']:<{name_width}}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Zephyr commit-statistics rankings and the static dashboard."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("ZEPHYR_PATH", os.getcwd()),
        help="Zephyr git checkout with full history (default: $ZEPHYR_PATH or cwd)",
    )
    parser.add_argument("--head", default="HEAD", help="Revision to rank (default: HEAD)")
    parser.add_argument(
        "--output-dir",
        default=os.environ.get(
            "COMMIT_STATISTICS_OUTPUT_DIR",
            str(Path(__file__).parents[2] / "reports" / "commit-statistics"),
        ),
        help="Where artifacts and index.html go (default: $COMMIT_STATISTICS_OUTPUT_DIR)",
    )
    parser.add_argument(
        "--show",
        metavar="RANGE",
        help="Print one ranking to the terminal instead of writing artifacts "
        f"(one of: all, release-1..release-{RELEASE_RANGE_COUNT}, "
        f"{', '.join(window[0] for window in TIME_WINDOWS)})",
    )
    parser.add_argument(
        "--type",
        choices=[ranking_type.id for ranking_type in RANKING_TYPES],
        default="individual",
        help="[--show] Ranking to print (default: individual)",
    )
    parser.add_argument(
        "--top", type=int, default=25, help="[--show] Rows to print, 0 for all (default: 25)"
    )

    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    assert_usable_repo(repo, args.head)

    print(f"Loading commit metadata from {repo} ({args.head})...", file=sys.stderr)
    commits = list(stream_commits(repo, args.head))
    print(f"Loaded {len(commits)} commits.", file=sys.stderr)

    ranges = build_ranges(repo, args.head, commits)

    if args.show:
        selected = next((entry for entry in ranges if entry.id == args.show), None)
        if selected is None:
            raise SystemExit(
                f"error: unknown range {args.show!r}; available: "
                f"{', '.join(entry.id for entry in ranges)}"
            )
        ranking_type = next(item for item in RANKING_TYPES if item.id == args.type)
        print()
        print_ranking(selected, ranking_type, None if args.top == 0 else args.top)
        return 0

    output_dir = Path(args.output_dir)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_artifacts(output_dir, repo, args.head, ranges, generated_at)

    print(
        f"Wrote {len(ranges) * len(RANKING_TYPES) * 2} artifacts, manifest.json and index.html "
        f"to {output_dir}"
    )
    for entry in ranges:
        print(f"  {entry.id:<12} {len(entry.commits):>7} commits  ({entry.base_label}..{args.head})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
