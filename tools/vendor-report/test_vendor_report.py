#!/usr/bin/env python3
"""Tests for vendor_report.py.

No network: the GitHub Search API is replaced by a fake that serves a synthetic
PR set and records every query, which is what makes the adaptive date-slicing
recursion testable. That recursion exists because Search silently caps any one
query at 1000 results, so getting it wrong loses PRs without any error.

Run: python tools/vendor-report/test_vendor_report.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import vendor_report as vr


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + ("" if condition else f" -- {detail}"))
    if not condition:
        FAILURES.append(label)


def check_equal(label: str, actual, expected) -> None:
    check(label, actual == expected, f"expected {expected!r}, got {actual!r}")


def check_close(label: str, actual, expected, tolerance=1e-6) -> None:
    ok = actual is not None and abs(actual - expected) <= tolerance
    check(label, ok, f"expected ~{expected}, got {actual}")


WINDOW_START = "2024-01-01"
WINDOW_END = "2024-12-31"
NOW = datetime(2025, 1, 15, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(number, created, *, merged_after=None, closed_after=None, labels=("platform: NXP",)):
    """A cache record, expressed in days relative to 2024-01-01."""

    created_at = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=created)
    state = "open"
    merged_at = closed_at = None
    if merged_after is not None:
        merged_at = closed_at = created_at + timedelta(days=merged_after)
        state = "closed"
    elif closed_after is not None:
        closed_at = created_at + timedelta(days=closed_after)
        state = "closed"
    return {
        "number": number,
        "state": state,
        "draft": False,
        "created_at": stamp(created_at),
        "closed_at": stamp(closed_at) if closed_at else None,
        "merged_at": stamp(merged_at) if merged_at else None,
        "labels": list(labels),
    }


# --------------------------------------------------------------------------
# Fake Search API
# --------------------------------------------------------------------------


class FakeApi:
    """Serves synthetic PRs and records queries, mimicking Search's 1000 cap."""

    def __init__(self, pulls: list[dict]):
        self.pulls = pulls
        self.queries: list[tuple[str, int]] = []
        self.token = "fake"
        self.requests = 0

    def get(self, query: str, page: int = 1) -> dict:
        self.queries.append((query, page))
        self.requests += 1

        matching = self.pulls
        window = re.search(r"created:(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)", query)
        if window:
            low, high = window.group(1), window.group(2)
            matching = [p for p in matching if low <= p["created_at"][:10] <= high]
        if "is:open" in query:
            matching = [p for p in matching if p["state"] == "open"]

        items = [
            {
                "number": p["number"],
                "state": p["state"],
                "draft": p["draft"],
                "created_at": p["created_at"],
                "closed_at": p["closed_at"],
                "labels": [{"name": name} for name in p["labels"]],
                "pull_request": {"merged_at": p["merged_at"]},
            }
            for p in matching
        ]
        # total_count reports the true total even when the API would refuse to
        # page past 1000 -- that asymmetry is exactly what triggers slicing.
        return {"total_count": len(items), "items": items[(page - 1) * 100 : page * 100]}


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_statistics() -> None:
    print("\nstatistics")
    check_equal("percentile of empty", vr.percentile([], 90), None)
    check_equal("percentile of one value", vr.percentile([7.0], 90), 7.0)
    check_close("median via percentile", vr.percentile([1, 2, 3, 4], 50), 2.5)
    check_close("p90 interpolates", vr.percentile([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 90), 90.0)

    check_equal("CI needs two samples", vr.bootstrap_median_ci([5.0]), (None, None))
    low, high = vr.bootstrap_median_ci([1.0, 2.0, 3.0, 10.0, 20.0], rounds=200)
    check("CI brackets the median", low <= 3.0 <= high, f"{low}..{high}")
    check_equal(
        "CI is reproducible for unchanged data",
        vr.bootstrap_median_ci([1.0, 2.0, 3.0, 10.0, 20.0], rounds=200),
        (low, high),
    )

    check_equal("quarter Q1", vr.quarter_of(datetime(2024, 3, 31, tzinfo=timezone.utc)), "2024Q1")
    check_equal("quarter Q2", vr.quarter_of(datetime(2024, 4, 1, tzinfo=timezone.utc)), "2024Q2")
    check_equal("quarter Q4", vr.quarter_of(datetime(2024, 12, 1, tzinfo=timezone.utc)), "2024Q4")


def test_metrics() -> None:
    print("\ncohort metrics")
    start = vr.parse_timestamp(WINDOW_START + "T00:00:00Z")
    end = vr.parse_timestamp(WINDOW_END + "T23:59:59Z")

    pulls = [
        record(1, 0, merged_after=2),      # merged in 2 days
        record(2, 10, merged_after=4),     # merged in 4 days
        record(3, 20, merged_after=30),    # merged in 30 days
        record(4, 30, closed_after=5),     # closed without merging
        record(5, 40),                     # still open, inside window
        record(6, -400),                   # created before the window, still open
        record(7, -300, merged_after=1),   # merged, but created before the window
    ]
    metrics = vr.compute_metrics([vr.to_pull_request(p) for p in pulls], start, end, NOW)

    check_equal("cohort excludes pre-window PRs", metrics.created, 5)
    check_equal("merged counts only in-window merges", metrics.merged, 3)
    check_equal("closed unmerged counted", metrics.closed_unmerged, 1)
    check_equal("open in cohort", metrics.open_in_cohort, 1)
    check_equal("decided excludes still-open", metrics.decided, 4)
    check_close("merge rate over decided PRs", metrics.merge_ratio, 3 / 4)
    check_close("throughput over whole cohort", metrics.throughput_ratio, 3 / 5)
    check_close("median latency", metrics.median_days, 4.0)
    check_close("mean latency", metrics.mean_days, (2 + 4 + 30) / 3)

    check_equal("backlog includes pre-window open PRs", metrics.backlog, 2)
    expected_age = ((NOW - vr.parse_timestamp(pulls[4]["created_at"])).total_seconds() / 86400.0
                    + (NOW - vr.parse_timestamp(pulls[5]["created_at"])).total_seconds() / 86400.0) / 2
    check_close("median open age uses the backlog, not the cohort",
                metrics.median_open_age, expected_age, tolerance=1e-6)

    check_equal("trend keyed on merge quarter", sorted(metrics.trend_median), ["2024Q1"])
    check_equal("created-by-quarter totals match the cohort",
                sum(metrics.trend_created.values()), metrics.created)
    check_equal("merged-by-quarter totals match merged",
                sum(metrics.trend_merged.values()), metrics.merged)

    empty = vr.compute_metrics([], start, end, NOW)
    check_equal("empty input yields no median", empty.median_days, None)
    check_equal("empty input yields no ratios", (empty.merge_ratio, empty.throughput_ratio), (None, None))


def test_vendor_grouping() -> None:
    print("\nvendor grouping")
    by_label = {
        "platform: NXP": [record(1, 0, merged_after=1), record(2, 1, merged_after=1)],
        # Same PR under a second NXP label: must not be double counted.
        "platform: NXP MCU": [record(2, 1, merged_after=1), record(3, 2, merged_after=1)],
        "platform: STM32": [record(4, 3, merged_after=1)],
        # Cross-vendor PR: counts for both vendors, once in the baseline.
        "platform: Infineon": [record(3, 2, merged_after=1)],
    }
    grouped = vr.group_by_vendor(by_label)
    check_equal("NXP labels unioned and deduplicated",
                sorted(p.number for p in grouped["NXP"]), [1, 2, 3])
    check_equal("cross-vendor PR counts for the other vendor too",
                [p.number for p in grouped["Infineon"]], [3])
    check("vendors with no PRs are absent", "TI" not in grouped, str(sorted(grouped)))

    report = vr.build_report(by_label, WINDOW_START, WINDOW_END, now=NOW)
    check_equal("baseline deduplicates across all labels", report.baseline.created, 4)


def test_threshold_pooling() -> None:
    print("\ninclusion threshold and Others row")
    big = [record(1000 + i, i % 300, merged_after=3) for i in range(vr.MIN_MERGED + 10)]
    small_a = [record(1, 0, merged_after=2), record(2, 1, merged_after=9)]
    small_b = [record(3, 2, merged_after=4), record(1, 0, merged_after=2)]  # shares PR 1

    report = vr.build_report(
        {"platform: NXP": big, "platform: STM32": small_a, "platform: ADI": small_b},
        WINDOW_START, WINDOW_END, now=NOW,
    )
    check_equal("only vendors over the threshold get a row", sorted(report.vendors), ["NXP"])
    check("small vendors are pooled, not dropped", report.others is not None)
    check_equal("pooled vendor names recorded",
                sorted(report.others.pooled_vendors), ["ADI", "ST"])
    check_equal("Others deduplicates a PR shared by two pooled vendors",
                report.others.created, 3)
    check_equal("NXP ranks first when it is the only vendor", report.rank_of("NXP"), 1)
    check_equal("unranked vendor returns no rank", report.rank_of("Nordic"), None)


def test_fetch_slicing() -> None:
    print("\nadaptive date slicing")
    # 1500 PRs spread across the year: the full range exceeds the 1000 cap, so
    # the fetch must split it rather than truncate.
    pulls = [record(2000 + i, i % 360, merged_after=1) for i in range(1500)]
    api = FakeApi(pulls)
    sink: dict[int, dict] = {}
    vr.collect_created_range(api, 'repo:x is:pr label:"platform: NXP"', WINDOW_START, WINDOW_END, sink)

    ranges = [
        re.search(r"created:(\S+)\.\.(\S+)", q).groups()
        for q, _ in api.queries if "created:" in q
    ]
    check("the over-cap range was split", len(ranges) > 1, str(ranges[:4]))
    check_equal("every PR was collected despite the cap", len(sink), 1500)
    check_equal("PR numbers are intact", min(sink), 2000)
    check("no sub-range exceeded the cap in the final fetches",
          all(len([p for p in pulls if lo <= p["created_at"][:10] <= hi]) <= vr.SEARCH_PAGE_LIMIT
              for lo, hi in ranges if (lo, hi) != (WINDOW_START, WINDOW_END)),
          str(ranges))

    print("\npagination")
    api2 = FakeApi([record(3000 + i, i % 10, merged_after=1) for i in range(250)])
    sink2: dict[int, dict] = {}
    vr.collect_created_range(api2, "repo:x is:pr", WINDOW_START, WINDOW_END, sink2)
    check_equal("all three pages fetched", len(sink2), 250)
    check_equal("pages requested", sorted({page for _, page in api2.queries}), [1, 2, 3])

    print("\nopen backlog fetch")
    api3 = FakeApi([record(4000, -500), record(4001, 5, merged_after=2), record(4002, -900)])
    sink3: dict[int, dict] = {}
    vr.collect_open(api3, "platform: NXP", sink3, WINDOW_END)
    check_equal("open PRs collected with no date bound", sorted(sink3), [4000, 4002])
    check("the open query carries no created: bound",
          all("created:" not in q for q, _ in api3.queries), str(api3.queries))

    print("\nsingle-day overflow")
    same_day = [record(5000 + i, 0, merged_after=1) for i in range(1200)]
    api4 = FakeApi(same_day)
    sink4: dict[int, dict] = {}
    # A single day cannot be split further; the code must cap and move on rather
    # than recurse forever.
    vr.collect_created_range(api4, "repo:x is:pr", "2024-01-01", "2024-01-01", sink4)
    check_equal("single-day overflow keeps the reachable page limit",
                len(sink4), vr.SEARCH_PAGE_LIMIT)


def test_cache(root: Path) -> None:
    print("\ncache")
    cache = root / "cache"
    api = FakeApi([record(1, 5, merged_after=2), record(2, 6)])
    vr.fetch_labels(api, cache, WINDOW_START, WINDOW_END)
    written = sorted(p.name for p in cache.glob("*.json"))
    check_equal("one cache file per label", len(written), len(vr.ALL_LABELS))

    by_label, missing = vr.load_cache(cache)
    check_equal("no labels missing after a full fetch", missing, [])
    check_equal("every label loaded", len(by_label), len(vr.ALL_LABELS))

    requests_before = api.requests
    vr.fetch_labels(api, cache, WINDOW_START, WINDOW_END)
    check_equal("a second run refetches nothing", api.requests, requests_before)

    # An incomplete file must be refetched, not trusted.
    target = cache / vr.cache_filename(vr.ALL_LABELS[0])
    target.write_text(json.dumps({"label": "x", "complete": False, "records": []}), encoding="utf-8")
    _, missing_after = vr.load_cache(cache)
    check_equal("incomplete cache reported missing", missing_after, [vr.ALL_LABELS[0]])
    vr.fetch_labels(api, cache, WINDOW_START, WINDOW_END)
    check("incomplete cache is refetched", api.requests > requests_before)

    # A corrupt file must not crash the load.
    target.write_text("{not json", encoding="utf-8")
    by_label2, missing2 = vr.load_cache(cache)
    check_equal("corrupt cache reported missing", missing2, [vr.ALL_LABELS[0]])
    check_equal("other labels still load", len(by_label2), len(vr.ALL_LABELS) - 1)

    missing_dir = vr.load_cache(root / "does-not-exist")
    check_equal("absent cache dir yields no data", missing_dir[0], {})
    check_equal("absent cache dir lists every label as missing",
                len(missing_dir[1]), len(vr.ALL_LABELS))


def test_outputs(root: Path) -> None:
    print("\ngenerated outputs")
    big = [record(1000 + i, i % 300, merged_after=(i % 20) + 1) for i in range(vr.MIN_MERGED + 40)]
    other = [record(1, 0, merged_after=2), record(2, 5)]
    report = vr.build_report(
        {"platform: NXP": big, "platform: STM32": other}, WINDOW_START, WINDOW_END, now=NOW
    )

    output = root / "out"
    vr.write_outputs(output, report)
    for name in ["index.html", "vendors.json", "vendors.csv"]:
        check(f"{name} written", (output / name).is_file())

    payload = json.loads((output / "vendors.json").read_text(encoding="utf-8"))
    check_equal("baseline row is last", payload["rows"][-1]["vendor"], "ALL VENDOR-LABELLED PRs")
    check("highlight row present", any(r["vendor"] == "NXP" for r in payload["rows"]))
    check_equal("ranked series lengths agree",
                {len(payload["ranked"][k]) for k in ("vendors", "median", "mean", "ciLow", "ciHigh", "colours")},
                {len(payload["ranked"]["vendors"])})
    check_equal("trend series length matches quarters",
                {len(s["data"]) for s in payload["trend"]["series"]} | {len(payload["trend"]["baseline"])},
                {len(payload["trend"]["quarters"])})
    check_equal("highlight gets its own colour",
                payload["ranked"]["colours"][payload["ranked"]["vendors"].index("NXP")],
                vr.HIGHLIGHT_COLOUR)

    import csv as _csv

    with (output / "vendors.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(_csv.DictReader(handle))
    check_equal("CSV header", list(rows[0]), vr.CSV_COLUMNS)
    check_equal("CSV row count matches the payload", len(rows), len(payload["rows"]))
    check("CSV writes empty strings rather than None",
          all("None" not in value for row in rows for value in row.values()), str(rows[-1]))

    html = (output / "index.html").read_text(encoding="utf-8")
    check("page references Chart.js as a sibling file, not inlined",
          'src="chart.umd.min.js"' in html and len(html) < 60000, f"{len(html)} bytes")
    check("page loads its data at view time", "fetch('vendors.json'" in html)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="vendor-report-test-"))
    try:
        test_statistics()
        test_metrics()
        test_vendor_grouping()
        test_threshold_pooling()
        test_fetch_slicing()
        test_cache(root)
        test_outputs(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
