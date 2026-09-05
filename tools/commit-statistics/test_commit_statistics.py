#!/usr/bin/env python3
"""Tests for commit_statistics.py, run against a synthetic git repository.

A purpose-built repo is used instead of a real Zephyr checkout so every
expected number is known exactly. It deliberately includes the cases that are
easy to get wrong:

  * a side branch whose commits carry old author dates but land after a release
    tag, so range membership must come from revision walking, not date
    comparison;
  * email addresses that look like nxp.com but are not (notnxp.com,
    nxp.com.mx) alongside a real subdomain (corp.nxp.com);
  * commits whose Signed-off-by domain differs from the author domain;
  * authors tied on commit count, which must share a rank.

Run: python tools/commit-statistics/test_commit_statistics.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import commit_statistics as cs


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + ("" if condition else f" -- {detail}"))
    if not condition:
        FAILURES.append(label)


def check_equal(label: str, actual, expected) -> None:
    check(label, actual == expected, f"expected {expected!r}, got {actual!r}")


# --------------------------------------------------------------------------
# Synthetic repository
# --------------------------------------------------------------------------

DAY = 24 * 3600
BASE_EPOCH = 1_600_000_000  # 2020-09-13, arbitrary fixed point


def run(repo: Path, args: list[str], env: dict | None = None) -> str:
    environment = {**os.environ, **(env or {})}
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8", env=environment
    )


def commit(
    repo: Path,
    message: str,
    author: str,
    email: str,
    day: int,
    signoff: str | None = None,
    path: str = "file.txt",
) -> None:
    (repo / path).write_text(f"{message}\n", encoding="utf-8")
    run(repo, ["add", path])
    body = message if signoff is None else f"{message}\n\nSigned-off-by: Someone <{signoff}>"
    stamp = f"{BASE_EPOCH + day * DAY} +0000"
    run(
        repo,
        ["commit", "-q", "--no-verify", "-m", body],
        env={
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": stamp,
        },
    )


def build_repo(root: Path) -> Path:
    repo = root / "synthetic"
    repo.mkdir()
    run(repo, ["init", "-q", "-b", "main"])
    run(repo, ["config", "user.name", "Test"])
    run(repo, ["config", "user.email", "test@example.com"])

    day = 0
    # Twelve tagged releases; commit_statistics needs at least ten.
    for release in range(1, 13):
        commit(repo, f"work before v{release}", "Carol", "carol@intel.com", day)
        day += 10
        commit(repo, f"release {release}", "Carol", "carol@intel.com", day)
        run(repo, ["tag", f"v{release}.0.0"])
        day += 10

    # Post-v12 work: the counts below are what the ranges are asserted against.
    #   Alice   3 (nxp.com)
    #   Bob     3 (corp.nxp.com)      -> tied with Alice, must share rank 1
    #   Dave    2 (gmail.com)
    #   Eve     1 (nxp.com.mx  -> NOT nxp)
    #   Frank   1 (notnxp.com  -> NOT nxp)
    for index in range(3):
        commit(repo, f"alice {index}", "Alice", "alice@nxp.com", day); day += 1
    for index in range(3):
        commit(repo, f"bob {index}", "Bob", "bob@corp.nxp.com", day); day += 1
    for index in range(2):
        # Author is on gmail but the patch is signed off by an nxp address:
        # the organization ranking must follow the signoff, not the author.
        commit(repo, f"dave {index}", "Dave", "dave@gmail.com", day, signoff="grace@nxp.com")
        day += 1
    commit(repo, "eve 0", "Eve", "eve@nxp.com.mx", day); day += 1
    commit(repo, "frank 0", "Frank", "frank@notnxp.com", day); day += 1

    # A side branch written long ago (day 5, i.e. before v1.0.0) but merged now.
    # Date filtering would drop these; revision walking must keep them. It
    # touches its own file so the merge is conflict-free.
    head = run(repo, ["rev-parse", "HEAD"]).strip()
    run(repo, ["checkout", "-q", "-b", "side", "v1.0.0"])
    commit(repo, "backdated side work", "Heidi", "heidi@nxp.com", 5, path="side.txt")
    run(repo, ["checkout", "-q", "main"])
    run(
        repo,
        ["merge", "-q", "--no-ff", "-m", "Merge side branch", "side"],
        env={
            "GIT_AUTHOR_NAME": "Merger", "GIT_AUTHOR_EMAIL": "merger@intel.com",
            "GIT_COMMITTER_NAME": "Merger", "GIT_COMMITTER_EMAIL": "merger@intel.com",
            "GIT_AUTHOR_DATE": f"{BASE_EPOCH + day * DAY} +0000",
            "GIT_COMMITTER_DATE": f"{BASE_EPOCH + day * DAY} +0000",
        },
    )
    assert run(repo, ["rev-parse", "HEAD"]).strip() != head
    return repo


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_email_and_org_classification() -> None:
    print("\nemail / organization classification")
    for email, expected in [
        ("alice@nxp.com", True),
        ("bob@corp.nxp.com", True),
        ("c@a.b.nxp.com", True),
        ("eve@nxp.com.mx", False),
        ("frank@notnxp.com", False),
        ("g@nxp.completely.else", False),
        ("h@intel.com", False),
    ]:
        check_equal(f"nxp regex {email}", bool(cs.NXP_EMAIL_REGEX.match(email)), expected)

    for email, expected in [
        ("alice@nxp.com", "@nxp"),
        ("bob@corp.nxp.com", "@nxp"),
        ("dave@gmail.com", "@independent"),
        ("carol@intel.com", "@intel"),
        ("x@nordicsemi.no", "@nordicsemi"),
        ("y@some-startup.io", "@some-startup"),
        # Lookalike domains must not be absorbed into @nxp by the first-label
        # fallback, or the organization ranking would disagree with the
        # per-author NXP ranking.
        ("eve@nxp.com.mx", "@nxp.com.mx"),
        ("g@nxp.completely.else", "@nxp.completely.else"),
        ("frank@notnxp.com", "@notnxp"),
        ("broken", None),
    ]:
        check_equal(f"organization_of {email}", cs.organization_of(email), expected)


def test_ranking_ties_and_shares() -> None:
    print("\nranking: ties and shares")
    entries = cs.rank({"a": 5, "b": 5, "c": 3, "d": 1})
    check_equal("tied entries share rank 1", [e["rank"] for e in entries], [1, 1, 3, 4])
    # Shares are rounded per entry, so they only sum to ~100.
    check("shares sum to about 100", abs(sum(e["share"] for e in entries) - 100) < 0.5,
          str(sum(e["share"] for e in entries)))
    check_equal("ordered by commits", [e["name"] for e in entries], ["a", "b", "c", "d"])
    check_equal("empty input", cs.rank({}), [])


def test_ranges_and_counts(repo: Path) -> None:
    print("\nranges and counts")
    commits = list(cs.stream_commits(repo, "HEAD"))
    ranges = {entry.id: entry for entry in cs.build_ranges(repo, "HEAD", commits)}

    check_equal(
        "range ids",
        sorted(ranges),
        sorted(["all", *[f"release-{n}" for n in range(1, 11)],
                "week", "month", "quarter", "half-year", "year"]),
    )

    # 12 releases x 2 commits + 10 post-release commits + 1 side + 1 merge = 36
    check_equal("all-history commit count", len(ranges["all"].commits), 36)
    check_equal("all-history base label", ranges["all"].base_label, "(root)")

    # release-1 == v12.0.0..HEAD == 10 post-release + side + merge = 12.
    # The side commit has an author date before v1.0.0, so this number proves
    # membership is computed by revision walking rather than by date.
    check_equal("release-1 includes backdated merged work", len(ranges["release-1"].commits), 12)
    check_equal("release-1 base label", ranges["release-1"].base_label, "v12.0.0")
    # Each older release adds its 2 commits.
    check_equal("release-2 commit count", len(ranges["release-2"].commits), 14)
    check_equal("release-10 commit count", len(ranges["release-10"].commits), 30)

    individual = {e["name"]: e for e in cs.rank(cs.count_commits(ranges["release-1"].commits, "individual"))}
    check_equal("Alice counted", individual["Alice <alice@nxp.com>"]["commits"], 3)
    check_equal("Bob counted", individual["Bob <bob@corp.nxp.com>"]["commits"], 3)
    check_equal("Alice and Bob tie at rank 1",
                {individual["Alice <alice@nxp.com>"]["rank"], individual["Bob <bob@corp.nxp.com>"]["rank"]},
                {1})

    nxp = {e["name"]: e["commits"] for e in cs.rank(cs.count_commits(ranges["release-1"].commits, "nxp"))}
    check_equal("nxp ranking members", sorted(nxp),
                ["Alice <alice@nxp.com>", "Bob <bob@corp.nxp.com>", "Heidi <heidi@nxp.com>"])
    check_equal("nxp excludes nxp.com.mx and notnxp.com",
                all("Eve" not in name and "Frank" not in name for name in nxp), True)
    check_equal("nxp total", sum(nxp.values()), 7)

    company = {e["name"]: e["commits"] for e in cs.rank(cs.count_commits(ranges["release-1"].commits, "company"))}
    # Alice 3 + Bob 3 + Heidi 1 + Dave's 2 signed off by @nxp = 9.
    check_equal("organization follows signoff over author", company["@nxp"], 9)
    check_equal("@independent not credited for signed-off commits",
                company.get("@independent"), None)
    check_equal("merge commit credited to its author domain", company["@intel"], 1)
    check_equal("lookalike domain kept out of @nxp", company.get("@nxp.com.mx"), 1)
    check_equal("notnxp.com kept out of @nxp", company.get("@notnxp"), 1)
    check_equal("company total equals range size", sum(company.values()),
                len(ranges["release-1"].commits))

    # The organization and per-author NXP views must differ only by commits
    # authored outside NXP but signed off by an NXP address (Dave's two).
    check_equal("org @nxp minus author-side NXP equals signoff-only commits",
                company["@nxp"] - sum(nxp.values()), 2)


def test_time_windows(repo: Path) -> None:
    print("\ntime windows")
    commits = list(cs.stream_commits(repo, "HEAD"))
    ranges = {entry.id: entry for entry in cs.build_ranges(repo, "HEAD", commits)}
    sizes = {key: len(ranges[key].commits) for key in ["week", "month", "quarter", "half-year", "year"]}
    print(f"        window sizes: {sizes}")
    check("windows are monotonically non-decreasing",
          sizes["week"] <= sizes["month"] <= sizes["quarter"] <= sizes["half-year"] <= sizes["year"],
          str(sizes))
    check("year window is a subset of all history", sizes["year"] <= len(ranges["all"].commits), str(sizes))
    check("week window is non-empty", sizes["week"] > 0, str(sizes))


def test_artifacts(repo: Path, output: Path) -> None:
    print("\ngenerated artifacts")
    commits = list(cs.stream_commits(repo, "HEAD"))
    ranges = cs.build_ranges(repo, "HEAD", commits)
    cs.write_artifacts(output, repo, "HEAD", ranges, "2026-01-01T00:00:00+00:00")

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    check_equal("manifest range count", len(manifest["ranges"]), 16)
    check_equal("manifest ranking types",
                [t["id"] for t in manifest["ranking_types"]], ["individual", "nxp", "company"])
    check_equal("all-history is first range", manifest["ranges"][0]["id"], "all")
    check("index.html written", (output / "index.html").is_file())

    missing = [
        f"{entry['id']}-{ranking['id']}.{extension}"
        for entry in manifest["ranges"]
        for ranking in manifest["ranking_types"]
        for extension in ("json", "csv")
        if not (output / "ranges" / f"{entry['id']}-{ranking['id']}.{extension}").is_file()
    ]
    check("every range x type artifact exists", not missing, str(missing))

    # Every JSON must use the key its manifest entry advertises, or the
    # dashboard renders "undefined".
    for entry in manifest["ranges"]:
        for ranking in manifest["ranking_types"]:
            payload = json.loads(
                (output / "ranges" / f"{entry['id']}-{ranking['id']}.json").read_text(encoding="utf-8")
            )
            if not payload["ranking"]:
                continue
            first = payload["ranking"][0]
            if ranking["key"] not in first:
                check(f"{entry['id']}-{ranking['id']} uses key {ranking['key']}", False, str(first))
                return
    check("every JSON uses its advertised entry key", True)

    payload = json.loads((output / "ranges" / "all-nxp.json").read_text(encoding="utf-8"))
    check_equal("all-nxp meta range", payload["meta"]["range"], "all")
    check_equal("all-nxp commits_in_range", payload["meta"]["commits_in_range"], 36)
    check_equal("all-nxp counted only nxp authors", payload["meta"]["commits_counted"], 7)
    check("all-nxp entries carry rank/commits/share",
          all({"rank", "author", "commits", "share"} <= set(e) for e in payload["ranking"]))

    header = (output / "ranges" / "all-company.csv").read_text(encoding="utf-8").splitlines()[0]
    check_equal("company CSV header", header, "rank,company,commits,share_percent")


def test_shallow_clone_is_rejected() -> None:
    print("\nshallow clone guard")
    zephyr = Path("C:/repos/workspace/zephyrproject/zephyr")
    if not zephyr.exists():
        print("        skipped (no local zephyr clone)")
        return
    try:
        cs.assert_usable_repo(zephyr, "HEAD")
    except SystemExit as error:
        check("shallow clone rejected with actionable message",
              "shallow" in str(error) and "unshallow" in str(error), str(error))
        return
    check("shallow clone rejected", False, "assert_usable_repo accepted a shallow clone")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="commit-stats-test-"))
    try:
        print(f"building synthetic repository in {root}")
        repo = build_repo(root)

        test_email_and_org_classification()
        test_ranking_ties_and_shares()
        test_ranges_and_counts(repo)
        test_time_windows(repo)
        test_artifacts(repo, root / "out")
        test_shallow_clone_is_rejected()
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
