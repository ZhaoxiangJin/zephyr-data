#!/usr/bin/env python3
"""Zephyr vendor PR velocity report.

Answers: how long does it take a silicon vendor's pull request to get merged
into Zephyr, and how does NXP compare?

Pipeline:

  1. Fetch PRs per `platform: *` label from the GitHub Search API into a
     per-label cache. Search items already carry created_at, closed_at, state
     and pull_request.merged_at, so no per-PR round trip is needed.
  2. Union each vendor's labels and deduplicate by PR number.
  3. Compute cohort metrics: latency percentiles, a bootstrap CI for the
     median, merge rate, open backlog, and a quarterly trend.
  4. Write index.html plus vendors.json / vendors.csv.

Methodology decisions that change how the numbers should be read:

  * Cohort basis. Rate metrics cover PRs *created* inside the window, so every
    vendor gets the same opportunity to merge. PRs created earlier are excluded
    from rate metrics but still count towards open backlog, because a PR open
    since 2021 is exactly what backlog management cares about.
  * Median first. Merge latency is heavily right-skewed; the median describes
    the typical PR while mean and P90 expose the tail.
  * Merge rate uses decided PRs only (merged + closed-unmerged). Still-open PRs
    are not counted as failures.
  * A PR carrying two vendors' labels counts for both vendors, but only once in
    the all-vendor baseline.
  * Vendors below the inclusion threshold are pooled into one "Others" row
    rather than dropped, so totals still reconcile.

Typical use:

  python vendor_report.py                 # fetch what is missing, then build
  python vendor_report.py --no-fetch      # rebuild from cache, no network
  python vendor_report.py --show          # print the table, write nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


REPO = "zephyrproject-rtos/zephyr"
SEARCH_URL = "https://api.github.com/search/issues"
SEARCH_PAGE_LIMIT = 1000  # hard cap the Search API puts on any single query
CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
CHARTJS_FILE = "chart.umd.min.js"

DEFAULT_WINDOW_START = "2023-09-01"
DEFAULT_WINDOW_END = "2026-08-31"

HIGHLIGHT = "NXP"

# A vendor needs this many merged PRs in the window to get its own row. The cut
# comes from the data, not taste: below roughly this point the 95% bootstrap CI
# of the median grows as wide as the median itself, so those vendors cannot be
# ranked against each other at all.
MIN_MERGED = 150
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 42  # fixed so a rebuild of unchanged data reproduces the CIs

# Zephyr spreads most silicon vendors across several labels; counting only the
# bare `platform: NXP` label undercounts NXP by roughly 20%.
VENDOR_LABELS = {
    "NXP": [
        "platform: NXP", "platform: NXP ADSP", "platform: NXP Drivers",
        "platform: NXP MCU", "platform: NXP MPU", "platform: NXP Robotics",
        "platform: NXP S32", "platform: NXP Xtensa",
    ],
    "Nordic": [
        "platform: nRF", "platform: nRF BSIM", "platform: nRF IronSide SE",
        "platform: nRFx BSIM",
    ],
    "ST": ["platform: STM32"],
    "TI": [
        "platform: TI", "platform: TI AM13", "platform: TI K3", "platform: TI MSPM",
        "platform: TI SimpleLink", "platform: TI Tiva C",
        "platform: Texas Instruments MSPM0",
    ],
    "Intel": [
        "platform: Intel", "platform: Intel ADSP", "platform: Intel ISH",
        "platform: Intel SoC FPGA Agilex",
    ],
    "Renesas": [
        "platform: Renesas", "platform: Renesas R-Car", "platform: Renesas R-Car ARM64",
        "platform: Renesas RA", "platform: Renesas RX", "platform: Renesas SmartBond",
        "platforms: Renesas RA", "platforms: Renesas RZ",
    ],
    "Microchip": [
        "platform: Microchip", "platform: Microchip MEC", "platform: Microchip PIC32",
        "platform: Microchip RISC-V", "platform: Microchip SAM",
        "platform: Microchip SmartFusion2",
    ],
    "Infineon": ["platform: Infineon"],
    "Silabs": ["platform: Silabs", "platform: Silabs SiM3U"],
    "Espressif": ["platform: ESP32"],
    "ADI": ["platform: ADI"],
    "Nuvoton": [
        "platform: Nuvoton", "platform: Nuvoton NPCM", "platform: Nuvoton NPCX",
        "platform: Nuvoton Numicro", "platform: Nuvoton Numicro Numaker",
    ],
    "Realtek": [
        "platform: Realtek", "platform: Realtek Ameba", "platform: Realtek Bee",
        "platform: Realtek EC", "platform: Realtek Fingerprint", "platform: Ameba",
    ],
    "Ambiq": ["platform: Ambiq"],
    "Raspberry Pi": ["platform: Raspberry Pi", "platform: Raspberry Pi Pico"],
    "Synopsys": ["platform: Synopsys"],
    "Xilinx/AMD": ["platform: Xilinx"],
    "GD32": ["platform: GD32"],
    "ITE": ["platform: ITE"],
    "Telink": ["platform: Telink"],
    "Alif": ["platform: Alif Semiconductor"],
    "Bouffalo Lab": ["platform: Bouffalo Lab"],
    "u-blox": ["platform: u-blox"],
    "Ezurio/Laird": ["platform: Ezurio", "platform: Laird Connectivity"],
}

ALL_LABELS = sorted({label for labels in VENDOR_LABELS.values() for label in labels})


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    created_at: datetime
    closed_at: Optional[datetime]
    merged_at: Optional[datetime]

    @property
    def merged(self) -> bool:
        return self.merged_at is not None


@dataclass
class Metrics:
    created: int = 0
    merged: int = 0
    closed_unmerged: int = 0
    open_in_cohort: int = 0
    decided: int = 0
    throughput_ratio: Optional[float] = None
    merge_ratio: Optional[float] = None
    median_days: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    mean_days: Optional[float] = None
    p90_days: Optional[float] = None
    backlog: int = 0
    median_open_age: Optional[float] = None
    trend_median: dict[str, float] = field(default_factory=dict)
    trend_created: dict[str, int] = field(default_factory=dict)
    trend_merged: dict[str, int] = field(default_factory=dict)
    pooled_vendors: list[str] = field(default_factory=list)


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def to_pull_request(record: dict) -> PullRequest:
    return PullRequest(
        number=record["number"],
        state=record["state"],
        created_at=parse_timestamp(record["created_at"]),
        closed_at=parse_timestamp(record.get("closed_at")),
        merged_at=parse_timestamp(record.get("merged_at")),
    )


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile, q in 0..100."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_median_ci(
    values: list[float], rounds: int = BOOTSTRAP_ROUNDS
) -> tuple[Optional[float], Optional[float]]:
    """95% percentile-bootstrap CI for the median.

    Reported so a reader can see which vendors are actually distinguishable.
    Width is driven by tail shape as well as sample size, so it cannot be
    inferred from the PR count alone.
    """

    count = len(values)
    if count < 2:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)
    medians = sorted(
        statistics.median([values[rng.randrange(count)] for _ in range(count)])
        for _ in range(rounds)
    )
    return medians[int(0.025 * rounds)], medians[int(0.975 * rounds)]


def quarter_of(moment: datetime) -> str:
    return f"{moment.year}Q{(moment.month - 1) // 3 + 1}"


def compute_metrics(
    pull_requests: Iterable[PullRequest],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> Metrics:
    cohort: list[PullRequest] = []
    backlog: list[PullRequest] = []
    for pull in pull_requests:
        if window_start <= pull.created_at <= window_end:
            cohort.append(pull)
        if pull.state == "open":
            backlog.append(pull)

    merged = [pull for pull in cohort if pull.merged]
    closed_unmerged = [pull for pull in cohort if pull.state == "closed" and not pull.merged]
    latency = [(pull.merged_at - pull.created_at).total_seconds() / 86400.0 for pull in merged]
    open_age = [(now - pull.created_at).total_seconds() / 86400.0 for pull in backlog]
    decided = len(merged) + len(closed_unmerged)
    ci_low, ci_high = bootstrap_median_ci(latency)

    # Trend is keyed on the quarter a PR was merged, so it reflects what
    # actually shipped in that quarter rather than when the work arrived.
    latency_by_quarter: dict[str, list[float]] = defaultdict(list)
    for pull in merged:
        latency_by_quarter[quarter_of(pull.merged_at)].append(
            (pull.merged_at - pull.created_at).total_seconds() / 86400.0
        )
    created_by_quarter: dict[str, int] = defaultdict(int)
    for pull in cohort:
        created_by_quarter[quarter_of(pull.created_at)] += 1
    merged_by_quarter: dict[str, int] = defaultdict(int)
    for pull in merged:
        merged_by_quarter[quarter_of(pull.merged_at)] += 1

    return Metrics(
        created=len(cohort),
        merged=len(merged),
        closed_unmerged=len(closed_unmerged),
        open_in_cohort=sum(1 for pull in cohort if pull.state == "open"),
        decided=decided,
        throughput_ratio=(len(merged) / len(cohort)) if cohort else None,
        merge_ratio=(len(merged) / decided) if decided else None,
        median_days=statistics.median(latency) if latency else None,
        ci_low=ci_low,
        ci_high=ci_high,
        mean_days=statistics.fmean(latency) if latency else None,
        p90_days=percentile(latency, 90),
        backlog=len(backlog),
        median_open_age=statistics.median(open_age) if open_age else None,
        trend_median={k: statistics.median(v) for k, v in latency_by_quarter.items()},
        trend_created=dict(created_by_quarter),
        trend_merged=dict(merged_by_quarter),
    )


def group_by_vendor(by_label: dict[str, list[dict]]) -> dict[str, list[PullRequest]]:
    """vendor -> its PRs, unioned across the vendor's labels and deduplicated."""

    grouped: dict[str, list[PullRequest]] = {}
    for vendor, labels in VENDOR_LABELS.items():
        merged: dict[int, dict] = {}
        for label in labels:
            for record in by_label.get(label, []):
                merged[record["number"]] = record
        if merged:
            grouped[vendor] = [to_pull_request(record) for record in merged.values()]
    return grouped


@dataclass
class Report:
    vendors: dict[str, Metrics]
    others: Optional[Metrics]
    baseline: Metrics
    window_start: str
    window_end: str
    generated_at: str
    missing_labels: list[str]

    @property
    def ranked(self) -> list[str]:
        """Vendors ordered fastest median first."""

        return sorted(
            (v for v, m in self.vendors.items() if m.median_days is not None),
            key=lambda v: self.vendors[v].median_days,
        )

    @property
    def by_volume(self) -> list[str]:
        return sorted(self.vendors, key=lambda v: -self.vendors[v].created)

    def rank_of(self, vendor: str) -> Optional[int]:
        ranked = self.ranked
        return ranked.index(vendor) + 1 if vendor in ranked else None


def build_report(
    by_label: dict[str, list[dict]],
    window_start: str,
    window_end: str,
    missing_labels: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> Report:
    now = now or datetime.now(timezone.utc)
    start = parse_timestamp(window_start + "T00:00:00Z")
    end = parse_timestamp(window_end + "T23:59:59Z")

    per_vendor = group_by_vendor(by_label)
    metrics = {
        vendor: compute_metrics(pulls, start, end, now) for vendor, pulls in per_vendor.items()
    }

    included = {v: m for v, m in metrics.items() if m.merged >= MIN_MERGED}
    pooled = sorted((v for v in metrics if v not in included), key=lambda v: -metrics[v].merged)

    others = None
    if pooled:
        # Deduplicate across the pooled vendors so a PR labelled for two of them
        # is not double counted in the Others row.
        tail: dict[int, PullRequest] = {}
        for vendor in pooled:
            for pull in per_vendor[vendor]:
                tail[pull.number] = pull
        others = compute_metrics(tail.values(), start, end, now)
        others.pooled_vendors = pooled

    everything: dict[int, dict] = {}
    for records in by_label.values():
        for record in records:
            everything[record["number"]] = record
    baseline = compute_metrics(
        [to_pull_request(r) for r in everything.values()], start, end, now
    )

    return Report(
        vendors=included,
        others=others,
        baseline=baseline,
        window_start=window_start,
        window_end=window_end,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        missing_labels=sorted(missing_labels or []),
    )


# --------------------------------------------------------------------------
# GitHub Search API
# --------------------------------------------------------------------------


class SearchApi:
    """Rate-limit-aware GitHub Search client."""

    def __init__(self, token: str = "", sleep: Callable[[float], None] = time.sleep):
        # Unauthenticated search allows 10 requests/min, authenticated allows 30.
        self.min_interval = 2.2 if token else 6.4
        self.token = token
        self.sleep = sleep
        self.last_call = 0.0
        self.requests = 0

    def get(self, query: str, page: int = 1) -> dict:
        for attempt in range(8):
            gap = time.time() - self.last_call
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)

            url = f"{SEARCH_URL}?q={urllib.parse.quote(query)}&per_page=100&page={page}"
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "zephyr-vendor-report",
            }
            if self.token:
                headers["Authorization"] = "Bearer " + self.token

            try:
                response = urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=90
                )
                body = json.load(response)
                self.last_call = time.time()
                self.requests += 1
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")
                if remaining is not None and reset and int(remaining) <= 1:
                    wait = int(reset) - int(time.time()) + 3
                    if wait > 0:
                        print(f"      quota exhausted, sleeping {wait}s", flush=True)
                        self.sleep(wait)
                return body
            except urllib.error.HTTPError as error:
                self.last_call = time.time()
                if error.code in (403, 429):
                    reset = error.headers.get("X-RateLimit-Reset")
                    wait = max(5, int(reset) - int(time.time()) + 3) if reset else 62
                    print(
                        f"      HTTP {error.code} -> backoff {min(wait, 300)}s "
                        f"(try {attempt + 1})",
                        flush=True,
                    )
                    self.sleep(min(wait, 300))
                    continue
                if error.code >= 500:
                    print(f"      HTTP {error.code} -> retry 15s", flush=True)
                    self.sleep(15)
                    continue
                raise
            except Exception as error:  # transient network trouble
                self.last_call = time.time()
                print(f"      network error {error} -> retry 15s", flush=True)
                self.sleep(15)
                continue
        raise RuntimeError(f"giving up after repeated failures: {query}")


def search_record(item: dict) -> dict:
    pull = item.get("pull_request") or {}
    return {
        "number": item["number"],
        "state": item["state"],
        "draft": item.get("draft", False),
        "created_at": item["created_at"],
        "closed_at": item.get("closed_at"),
        "merged_at": pull.get("merged_at"),
        "labels": [label["name"] for label in item.get("labels", [])],
    }


def collect_created_range(
    api: SearchApi, base_query: str, start: str, end: str, sink: dict[int, dict], depth: int = 0
) -> None:
    """Fetch base_query restricted to created:start..end, halving if over the cap.

    Search refuses to return more than 1000 results for one query, so a range
    reporting more is split and recursed rather than silently truncated.
    """

    query = f"{base_query} created:{start}..{end}"
    first = api.get(query, page=1)
    total = first.get("total_count", 0)
    if total == 0:
        return

    if total > SEARCH_PAGE_LIMIT:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        if start_date >= end_date:
            print(
                f"      !! {total} results on the single day {start}, "
                f"keeping the first {SEARCH_PAGE_LIMIT}",
                flush=True,
            )
        else:
            midpoint = start_date + (end_date - start_date) // 2
            print(f"      {'  ' * depth}split {start}..{end} ({total} results)", flush=True)
            collect_created_range(api, base_query, start, midpoint.isoformat(), sink, depth + 1)
            collect_created_range(
                api, base_query, (midpoint + timedelta(days=1)).isoformat(), end, sink, depth + 1
            )
            return

    for item in first.get("items", []):
        sink[item["number"]] = search_record(item)
    pages = (min(total, SEARCH_PAGE_LIMIT) + 99) // 100
    for page in range(2, pages + 1):
        for item in api.get(query, page=page).get("items", []):
            sink[item["number"]] = search_record(item)


def collect_open(api: SearchApi, label: str, sink: dict[int, dict], window_end: str) -> None:
    """Open PRs with no date bound, so pre-window backlog is not lost."""

    base = f'repo:{REPO} is:pr is:open label:"{label}"'
    body = api.get(base, page=1)
    total = body.get("total_count", 0)
    if total == 0:
        return
    if total > SEARCH_PAGE_LIMIT:
        collect_created_range(api, base, "2015-01-01", window_end, sink)
        return
    for item in body.get("items", []):
        sink[item["number"]] = search_record(item)
    for page in range(2, (total + 99) // 100 + 1):
        for item in api.get(base, page=page).get("items", []):
            sink[item["number"]] = search_record(item)


def cache_filename(label: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in label) + ".json"


def fetch_labels(api: SearchApi, cache_dir: Path, window_start: str, window_end: str) -> None:
    """Populate the per-label cache, skipping labels already complete.

    Per-label files make a run resumable: a rate-limited or interrupted run only
    refetches what is still missing.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    print(
        f"window {window_start} .. {window_end}  |  {len(ALL_LABELS)} labels  "
        f"|  auth={bool(api.token)}",
        flush=True,
    )

    for index, label in enumerate(ALL_LABELS, 1):
        path = cache_dir / cache_filename(label)
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("complete"):
                    print(
                        f"[{index}/{len(ALL_LABELS)}] {label} -- cached "
                        f"({len(cached['records'])} PRs)",
                        flush=True,
                    )
                    continue
            except (OSError, ValueError, KeyError):
                pass  # unreadable or truncated cache: refetch it

        print(f"[{index}/{len(ALL_LABELS)}] {label}", flush=True)
        sink: dict[int, dict] = {}
        collect_created_range(
            api, f'repo:{REPO} is:pr label:"{label}"', window_start, window_end, sink
        )
        in_window = len(sink)
        collect_open(api, label, sink, window_end)

        path.write_text(
            json.dumps(
                {
                    "label": label,
                    "window": [window_start, window_end],
                    "complete": True,
                    "records": list(sink.values()),
                }
            ),
            encoding="utf-8",
        )
        print(
            f"      {in_window} PRs in window, {len(sink)} with open backlog  "
            f"[{api.requests} requests, {(time.time() - started) / 60:.1f} min]",
            flush=True,
        )

    print(f"DONE: {api.requests} requests, {(time.time() - started) / 60:.1f} minutes", flush=True)


def load_cache(cache_dir: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """(label -> records, labels that are missing or incomplete)."""

    by_label: dict[str, list[dict]] = {}
    missing: list[str] = []
    for label in ALL_LABELS:
        path = cache_dir / cache_filename(label)
        if not path.is_file():
            missing.append(label)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(label)
            continue
        if not payload.get("complete"):
            missing.append(label)
            continue
        by_label[label] = payload["records"]
    return by_label, missing


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

PALETTE = [
    "#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#17becf", "#7f7f7f",
    "#bcbd22", "#e377c2", "#546e7a", "#00897b", "#5c6bc0", "#8d6e63",
    "#26a69a", "#78909c", "#ab47bc", "#66bb6a", "#42a5f5", "#d4a017",
]
HIGHLIGHT_COLOUR = "#ff6a00"

CSV_COLUMNS = [
    "vendor", "created", "merged", "merged_over_created", "median_days",
    "ci_low", "ci_high", "mean_days", "p90_days", "merge_rate",
    "closed_unmerged", "open_backlog", "median_open_age_days",
]


def colour_for(vendor: str, index: int) -> str:
    return HIGHLIGHT_COLOUR if vendor == HIGHLIGHT else PALETTE[index % len(PALETTE)]


def metrics_row(vendor: str, metrics: Metrics) -> dict:
    return {
        "vendor": vendor,
        "created": metrics.created,
        "merged": metrics.merged,
        "merged_over_created": metrics.throughput_ratio,
        "median_days": metrics.median_days,
        "ci_low": metrics.ci_low,
        "ci_high": metrics.ci_high,
        "mean_days": metrics.mean_days,
        "p90_days": metrics.p90_days,
        "merge_rate": metrics.merge_ratio,
        "closed_unmerged": metrics.closed_unmerged,
        "open_backlog": metrics.backlog,
        "median_open_age_days": metrics.median_open_age,
    }


def report_rows(report: Report) -> list[dict]:
    rows = [metrics_row(vendor, report.vendors[vendor]) for vendor in report.by_volume]
    if report.others is not None:
        row = metrics_row(f"Others ({len(report.others.pooled_vendors)} vendors)", report.others)
        row["pooled_vendors"] = report.others.pooled_vendors
        rows.append(row)
    rows.append(metrics_row("ALL VENDOR-LABELLED PRs", report.baseline))
    return rows


def chart_payload(report: Report) -> dict:
    ranked = report.ranked
    by_volume = report.by_volume
    quarters = sorted(
        {q for m in report.vendors.values() for q in m.trend_median}
        | set(report.baseline.trend_median)
    )

    # Keep the trend chart readable: the biggest vendors plus the highlighted one.
    trend_vendors = by_volume[:9]
    if HIGHLIGHT in report.vendors and HIGHLIGHT not in trend_vendors:
        trend_vendors.append(HIGHLIGHT)

    volume_vendors = [v for v in by_volume if report.vendors[v].created > 0][:18]
    round2 = lambda value: None if value is None else round(value, 2)

    return {
        "highlight": HIGHLIGHT,
        "minMerged": MIN_MERGED,
        "bootstrapRounds": BOOTSTRAP_ROUNDS,
        "windowStart": report.window_start,
        "windowEnd": report.window_end,
        "generatedAt": report.generated_at,
        "missingLabels": report.missing_labels,
        "nxpLabelCount": len(VENDOR_LABELS.get(HIGHLIGHT, [])),
        "rows": report_rows(report),
        "highlightRank": report.rank_of(HIGHLIGHT),
        "rankedCount": len(ranked),
        "ranked": {
            "vendors": ranked,
            "median": [round2(report.vendors[v].median_days) for v in ranked],
            "mean": [round2(report.vendors[v].mean_days) for v in ranked],
            "ciLow": [round2(report.vendors[v].ci_low) for v in ranked],
            "ciHigh": [round2(report.vendors[v].ci_high) for v in ranked],
            "colours": [colour_for(v, i) for i, v in enumerate(ranked)],
        },
        "volume": {
            "vendors": volume_vendors,
            "created": [report.vendors[v].created for v in volume_vendors],
            "merged": [report.vendors[v].merged for v in volume_vendors],
            "colours": [colour_for(v, i) for i, v in enumerate(volume_vendors)],
        },
        "trend": {
            "quarters": quarters,
            "series": [
                {
                    "vendor": vendor,
                    "colour": colour_for(vendor, index),
                    "data": [round2(report.vendors[vendor].trend_median.get(q)) for q in quarters],
                }
                for index, vendor in enumerate(trend_vendors)
            ],
            # Pooled across every vendor-labelled PR rather than a
            # median-of-medians, which would weight a 10-PR vendor like a
            # 3000-PR one.
            "baseline": [round2(report.baseline.trend_median.get(q)) for q in quarters],
        },
        "baselineMedian": round2(report.baseline.median_days),
    }


def write_outputs(report_dir: Path, report: Report) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = chart_payload(report)

    (report_dir / "vendors.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    with (report_dir / "vendors.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow(
                {
                    key: ("" if row.get(key) is None else row.get(key))
                    for key in CSV_COLUMNS
                }
            )

    ensure_chartjs(report_dir)
    (report_dir / "index.html").write_text(DASHBOARD_HTML, encoding="utf-8")


def ensure_chartjs(report_dir: Path) -> None:
    """Keep Chart.js beside the page so viewing needs no external request.

    Referenced as a sibling file rather than inlined: the old report embedded
    205 kB of library into every rebuild of index.html.
    """

    target = report_dir / CHARTJS_FILE
    if target.is_file() and target.stat().st_size > 1000:
        return
    source = Path(__file__).with_name(CHARTJS_FILE)
    if source.is_file():
        target.write_bytes(source.read_bytes())
        return
    try:
        print(f"downloading {CHARTJS_FILE} for offline use...", flush=True)
        with urllib.request.urlopen(CHARTJS_CDN, timeout=60) as response:
            body = response.read()
        target.write_bytes(body)
        source.write_bytes(body)
    except Exception as error:
        print(f"  could not fetch Chart.js ({error}); the page will fall back to the CDN")


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zephyr Vendor PR Velocity</title>
<script src="chart.umd.min.js"></script>
<style>
:root{--ink:#1a2027;--mut:#5b6b7a;--line:#e3e8ee;--bg:#f5f7fa;--nxp:#ff6a00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:32px 24px 64px}
header h1{margin:0 0 6px;font-size:26px;letter-spacing:-.3px}header .meta{color:var(--mut);font-size:13px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:22px;margin:22px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
h2{font-size:17px;margin:0 0 4px}h2 .n{color:var(--mut);font-weight:600;margin-right:8px}
.hint{color:var(--mut);font-size:12.5px;margin:0 0 18px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}
.kpi{background:#fff;border:1px solid var(--line);border-left:4px solid var(--nxp);border-radius:10px;padding:16px 18px}
.kpi .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.4px}
.kpi .val{font-size:27px;font-weight:700;margin:6px 0 2px}.kpi .val .u{font-size:14px;font-weight:500;color:var(--mut)}
.kpi .sub{font-size:12px;color:var(--mut)}.kpi .sub.good{color:#1b7f3b;font-weight:600}.kpi .sub.bad{color:#c62828;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#fbfcfd;font-size:11.5px;text-transform:uppercase;letter-spacing:.3px;color:var(--mut);cursor:pointer;position:sticky;top:0;border-bottom:2px solid var(--line)}
th:hover{color:var(--ink)}th.g,td.v{text-align:left}
tr.hl{background:#fff6ef}tr.hl td.v{font-weight:700;color:#c25100;border-left:3px solid var(--nxp)}
tr.base{background:#f3f6f9;font-style:italic}tr.base td{border-top:2px solid #cfd8e3}
tr.low td{color:#8b97a3}
td.ci{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.tag{font-size:10px;background:#eef1f5;color:var(--mut);padding:2px 6px;border-radius:9px;font-style:normal;font-weight:600;text-transform:uppercase;letter-spacing:.3px}
.chart{position:relative;height:380px}.chart.tall{height:470px}
.warn{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:12px 16px;font-size:13px}
.note{font-size:12.5px;color:var(--mut)}.note li{margin:4px 0}
.scroll{overflow:auto;max-height:620px}
.error{color:#c62828;font-weight:bold}
a.dl{color:#1f77b4;margin-right:14px}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="wrap">
<header><h1>Zephyr Vendor PR Velocity</h1><div class="meta" id="meta">Loading report data&hellip;</div></header>
<div id="warn"></div>
<div class="kpis" id="kpis"></div>

<div class="card">
  <h2><span class="n">1</span>Vendor summary</h2>
  <p class="hint">Click any column header to sort. Median merge time is the headline latency figure; mean and P90 expose the tail.
     <a class="dl" href="vendors.csv">vendors.csv</a><a class="dl" href="vendors.json">vendors.json</a></p>
  <div class="scroll"><table id="t"><thead><tr>
    <th class="g">Vendor</th><th>Created</th><th>Merged</th><th>Merged&nbsp;/&nbsp;Created</th>
    <th>Median&nbsp;days</th><th>95%&nbsp;CI</th><th>Mean&nbsp;days</th><th>P90&nbsp;days</th>
    <th>Merge&nbsp;rate</th><th>Closed&nbsp;unmerged</th><th>Open&nbsp;backlog</th><th>Median&nbsp;open&nbsp;age</th>
  </tr></thead><tbody id="rows"></tbody></table></div>
</div>

<div class="card">
  <h2><span class="n">2</span>Merge latency by vendor</h2>
  <p class="hint">Bars are median days from PR creation to merge, ascending (shorter is better). Dots mark the mean &mdash;
     a dot far right of its bar means a heavy tail of slow PRs. Horizontal whiskers are the 95% confidence interval:
     <b>where two vendors' whiskers overlap, the gap between them is not statistically meaningful.</b>
     The dashed line is the all-vendor baseline.</p>
  <div class="chart tall"><canvas id="c_lat"></canvas></div>
</div>

<div class="card">
  <h2><span class="n">3</span>Created vs merged volume</h2>
  <p class="hint">Throughput in the window. A merged bar well below its created bar means work is arriving faster than it clears.</p>
  <div class="chart"><canvas id="c_vol"></canvas></div>
</div>

<div class="card">
  <h2><span class="n">4</span>Merge latency trend by quarter</h2>
  <p class="hint">Median days to merge, bucketed by the quarter the PR was merged. Click legend entries to isolate vendors.</p>
  <div class="chart tall"><canvas id="c_trend"></canvas></div>
</div>

<div class="card"><h2>Methodology</h2><ul class="note" id="method"></ul></div>

<script>
const fmt = (value, digits = 1, suffix = '') =>
  value === null || value === undefined ? '\\u2014' : value.toFixed(digits) + suffix;
const fmtPct = value => value === null || value === undefined ? '\\u2014' : (value * 100).toFixed(0) + '%';
const escapeHtml = value => {
  const element = document.createElement('span');
  element.textContent = value === null || value === undefined ? '' : value;
  return element.innerHTML;
};

fetch('vendors.json', {cache: 'no-cache'}).then(response => response.json()).then(D => {
  document.querySelector('#meta').innerHTML =
    `Repository <b>${escapeHtml('zephyrproject-rtos/zephyr')}</b> &middot; PRs created ${escapeHtml(D.windowStart)}`
    + ` &rarr; ${escapeHtml(D.windowEnd)} &middot; generated ${escapeHtml(D.generatedAt)} &middot; source: GitHub Search API`;

  if (D.missingLabels && D.missingLabels.length) {
    document.querySelector('#warn').innerHTML =
      `<div class="warn"><b>Partial data.</b> ${D.missingLabels.length} label(s) were unavailable when this `
      + `report was generated: ${D.missingLabels.slice(0, 12).map(escapeHtml).join(', ')}. Re-run the generator.</div>`;
  }

  const baseline = D.rows[D.rows.length - 1];
  const nxp = D.rows.find(row => row.vendor === D.highlight);
  if (nxp) {
    const delta = nxp.median_days === null || !baseline.median_days
      ? null : nxp.median_days - baseline.median_days;
    document.querySelector('#kpis').innerHTML =
        `<div class="kpi"><div class="k">${escapeHtml(D.highlight)} median merge time</div>`
      + `<div class="val">${fmt(nxp.median_days)} <span class="u">days</span></div>`
      + `<div class="sub ${delta !== null && delta < 0 ? 'good' : 'bad'}">`
      + `${delta === null ? '\\u2014' : (delta > 0 ? '+' : '') + delta.toFixed(1) + ' d vs baseline'}</div></div>`
      + `<div class="kpi"><div class="k">Speed rank</div><div class="val">`
      + `${D.highlightRank ? '#' + D.highlightRank + ' of ' + D.rankedCount : '\\u2014'}</div>`
      + `<div class="sub">among vendors with &ge;${D.minMerged} merged PRs</div></div>`
      + `<div class="kpi"><div class="k">Created / Merged</div>`
      + `<div class="val">${nxp.created} <span class="u">/</span> ${nxp.merged}</div>`
      + `<div class="sub">${fmtPct(nxp.merged_over_created)} of created PRs merged</div></div>`
      + `<div class="kpi"><div class="k">Open backlog</div><div class="val">${nxp.open_backlog}</div>`
      + `<div class="sub">median age ${fmt(nxp.median_open_age_days, 0)} days</div></div>`;
  }

  document.querySelector('#rows').innerHTML = D.rows.map((row, index) => {
    const last = index === D.rows.length - 1;
    const pooled = Array.isArray(row.pooled_vendors);
    const cls = last ? ' class="base"' : (row.vendor === D.highlight ? ' class="hl"' : (pooled ? ' class="low"' : ''));
    const tag = last
      ? ' <span class="tag">baseline</span>'
      : (pooled ? ` <span class="tag" title="${escapeHtml(row.pooled_vendors.join(', '))}">below ${D.minMerged} merged PRs</span>` : '');
    const ci = row.ci_low === null || row.ci_low === undefined
      ? '\\u2014' : `${row.ci_low.toFixed(1)}\\u2013${row.ci_high.toFixed(1)}`;
    return `<tr${cls}><td class="v">${escapeHtml(row.vendor)}${tag}</td>`
      + `<td>${row.created}</td><td>${row.merged}</td><td>${fmtPct(row.merged_over_created)}</td>`
      + `<td><b>${fmt(row.median_days)}</b></td><td class="ci">${ci}</td>`
      + `<td>${fmt(row.mean_days)}</td><td>${fmt(row.p90_days, 0)}</td>`
      + `<td>${fmtPct(row.merge_rate)}</td><td>${row.closed_unmerged}</td>`
      + `<td>${row.open_backlog}</td><td>${fmt(row.median_open_age_days, 0)}</td></tr>`;
  }).join('');

  document.querySelector('#method').innerHTML = [
    `<b>Vendor grouping.</b> Zephyr spreads a vendor across several labels (${D.highlight} alone uses `
      + `${D.nxpLabelCount} of them). Every label belonging to a vendor is unioned and de-duplicated by PR number.`,
    `<b>Cohort basis.</b> Rate metrics cover PRs <i>created</i> in the window, so all vendors get the same window of `
      + `opportunity to merge. Open backlog additionally includes still-open PRs created before the window.`,
    `<b>Median first.</b> PR latency is heavily right-skewed; the median describes the typical PR, and mean/P90 expose the tail.`,
    `<b>Inclusion threshold.</b> A vendor needs ${D.minMerged} merged PRs in the window to get its own row. Below roughly `
      + `that point the 95% CI of the median becomes as wide as the median itself, so those vendors cannot be ranked. `
      + `They are pooled into the <i>Others</i> row, which keeps totals complete.`,
    `<b>Confidence intervals</b> are percentile bootstrap (${D.bootstrapRounds} resamples) on the median, with a fixed seed `
      + `so an unchanged dataset reproduces the same interval. Width is driven by tail shape as well as sample count.`,
    `<b>Merge rate</b> counts decided PRs only (merged + closed-unmerged); still-open PRs are not treated as failures.`,
    `<b>Baseline</b> is every vendor-labelled PR de-duplicated once. It is not a repo-wide figure: PRs with no `
      + `<code>platform:</code> label are out of scope.`,
    `<b>Caveat.</b> Latency measures calendar time, not effort. It mixes review responsiveness, CI turnaround and how `
      + `fast the submitter answers review comments &mdash; a slow number is not automatically the community's fault.`,
  ].map(item => `<li>${item}</li>`).join('');

  // Sortable table: the baseline row is pinned to the bottom.
  const body = document.querySelector('#rows');
  document.querySelectorAll('#t th').forEach((header, column) => {
    let ascending = false;
    header.addEventListener('click', () => {
      ascending = !ascending;
      const all = [...body.rows];
      const base = all.filter(row => row.classList.contains('base'));
      const rest = all.filter(row => !row.classList.contains('base'));
      const num = text => {
        const value = parseFloat(String(text).replace(/[^0-9.\\-]/g, ''));
        return isNaN(value) ? -Infinity : value;
      };
      rest.sort((a, b) => {
        const x = a.cells[column].textContent.trim(), y = b.cells[column].textContent.trim();
        return column === 0
          ? (ascending ? x.localeCompare(y) : y.localeCompare(x))
          : (ascending ? num(x) - num(y) : num(y) - num(x));
      });
      rest.concat(base).forEach(row => body.appendChild(row));
    });
  });

  if (!window.Chart) return;
  Chart.defaults.font.family = 'Segoe UI, Roboto, Helvetica, Arial, sans-serif';
  Chart.defaults.color = '#5b6b7a';
  const grid = {color: '#eef1f5'};

  new Chart(document.getElementById('c_lat'), {
    data: {
      labels: D.ranked.vendors,
      datasets: [
        {type: 'bar', label: 'Median days to merge', data: D.ranked.median,
         backgroundColor: D.ranked.colours, borderRadius: 3, order: 2},
        {type: 'scatter', label: 'Mean days to merge',
         data: D.ranked.mean.map((value, index) => ({x: value, y: index})),
         pointStyle: 'circle', radius: 4.5, backgroundColor: '#1a2027',
         borderColor: '#fff', borderWidth: 1.5, order: 1},
      ],
    },
    options: {
      indexAxis: 'y', maintainAspectRatio: false,
      scales: {x: {title: {display: true, text: 'days'}, grid, beginAtZero: true},
               y: {grid: {display: false}}},
      plugins: {legend: {position: 'bottom'},
                tooltip: {callbacks: {label: c => `${c.dataset.label}: ${c.parsed.x.toFixed(1)} d`}}},
    },
    plugins: [{
      id: 'whiskers',
      afterDraw(chart) {
        const ctx = chart.ctx, xs = chart.scales.x, ys = chart.scales.y, area = chart.chartArea;
        ctx.save();
        ctx.strokeStyle = 'rgba(26,32,39,.55)';
        ctx.lineWidth = 1.4;
        D.ranked.ciLow.forEach((low, index) => {
          if (low === null) return;
          const y = ys.getPixelForValue(index);
          const left = xs.getPixelForValue(low), right = xs.getPixelForValue(D.ranked.ciHigh[index]);
          const cap = Math.min(5, Math.max(3, ys.height / D.ranked.ciLow.length / 5));
          ctx.beginPath();
          ctx.moveTo(left, y); ctx.lineTo(right, y);
          ctx.moveTo(left, y - cap); ctx.lineTo(left, y + cap);
          ctx.moveTo(right, y - cap); ctx.lineTo(right, y + cap);
          ctx.stroke();
        });
        ctx.restore();

        if (D.baselineMedian === null || D.baselineMedian === undefined) return;
        const x = xs.getPixelForValue(D.baselineMedian);
        ctx.save();
        ctx.setLineDash([5, 4]); ctx.strokeStyle = '#c62828'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(x, area.top); ctx.lineTo(x, area.bottom); ctx.stroke();
        ctx.setLineDash([]); ctx.fillStyle = '#c62828';
        ctx.font = '600 11px Segoe UI'; ctx.textAlign = 'left';
        ctx.fillText(`baseline ${D.baselineMedian.toFixed(1)} d`, x + 5, area.top + 12);
        ctx.restore();
      },
    }],
  });

  new Chart(document.getElementById('c_vol'), {
    type: 'bar',
    data: {
      labels: D.volume.vendors,
      datasets: [
        {label: 'Created', data: D.volume.created, backgroundColor: '#c6d3e0', borderRadius: 3},
        {label: 'Merged', data: D.volume.merged, backgroundColor: D.volume.colours, borderRadius: 3},
      ],
    },
    options: {
      maintainAspectRatio: false,
      scales: {y: {title: {display: true, text: 'PRs'}, grid, beginAtZero: true},
               x: {grid: {display: false}}},
      plugins: {legend: {position: 'bottom'}},
    },
  });

  new Chart(document.getElementById('c_trend'), {
    type: 'line',
    data: {
      labels: D.trend.quarters,
      datasets: D.trend.series.map(series => ({
        label: series.vendor, data: series.data, borderColor: series.colour,
        backgroundColor: series.colour, spanGaps: true, tension: .3,
        borderWidth: series.vendor === D.highlight ? 3.5 : 1.6,
        pointRadius: series.vendor === D.highlight ? 4 : 2.5,
      })).concat([{
        label: 'All-vendor median', data: D.trend.baseline, borderColor: '#c62828',
        borderDash: [6, 4], borderWidth: 2, pointRadius: 0, spanGaps: true, tension: .3,
      }]),
    },
    options: {
      maintainAspectRatio: false,
      interaction: {mode: 'nearest', intersect: false},
      scales: {y: {title: {display: true, text: 'median days to merge'}, grid, beginAtZero: true},
               x: {grid: {display: false}}},
      plugins: {legend: {position: 'bottom', labels: {boxWidth: 12, usePointStyle: true}}},
    },
  });
}).catch(() => {
  document.querySelector('#meta').innerHTML = '<span class="error">Unable to load vendors.json.</span>';
});
</script>
</div></body></html>"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def print_table(report: Report) -> None:
    print(f"\nwindow {report.window_start} .. {report.window_end}")
    if report.missing_labels:
        print(f"WARNING: {len(report.missing_labels)} label(s) missing from cache")
    header = f"{'vendor':<16}{'created':>8}{'merged':>8}{'median':>8}{'95% CI':>14}{'open':>7}"
    print(f"\n{header}\n{'-' * len(header)}")
    for row in report_rows(report):
        median = "-" if row["median_days"] is None else f"{row['median_days']:.1f}"
        ci = (
            "-"
            if row["ci_low"] is None
            else f"{row['ci_low']:.1f}-{row['ci_high']:.1f}"
        )
        print(
            f"{row['vendor'][:16]:<16}{row['created']:>8}{row['merged']:>8}"
            f"{median:>8}{ci:>14}{row['open_backlog']:>7}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Zephyr vendor PR velocity report.")
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("VENDOR_REPORT_CACHE_DIR", str(here / "cache")),
        help="Per-label API cache (default: $VENDOR_REPORT_CACHE_DIR)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get(
            "VENDOR_REPORT_OUTPUT_DIR", str(here.parents[1] / "reports" / "vendor")
        ),
        help="Where the report and data files go (default: $VENDOR_REPORT_OUTPUT_DIR)",
    )
    parser.add_argument("--window-start", default=os.environ.get("WIN_START", DEFAULT_WINDOW_START))
    parser.add_argument("--window-end", default=os.environ.get("WIN_END", DEFAULT_WINDOW_END))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Build from the existing cache without contacting GitHub",
    )
    parser.add_argument(
        "--show", action="store_true", help="Print the summary table instead of writing files"
    )
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)

    if not args.no_fetch:
        token = (os.environ.get("GH_VALID_TOKEN") or "").strip()
        fetch_labels(SearchApi(token), cache_dir, args.window_start, args.window_end)

    by_label, missing = load_cache(cache_dir)
    if not by_label:
        raise SystemExit(
            f"error: no usable cache in {cache_dir}. Run without --no-fetch to populate it."
        )

    report = build_report(by_label, args.window_start, args.window_end, missing)
    print(
        f"\nlabels loaded: {len(by_label)} (missing {len(missing)}); "
        f"{len(report.vendors)} vendors reported, "
        f"{len(report.others.pooled_vendors) if report.others else 0} pooled into Others "
        f"(< {MIN_MERGED} merged)"
    )

    if args.show:
        print_table(report)
        return 0

    output_dir = Path(args.output_dir)
    write_outputs(output_dir, report)
    print(f"Wrote index.html, vendors.json, vendors.csv to {output_dir}")
    print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
