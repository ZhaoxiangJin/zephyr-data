#!/usr/bin/env python3
"""NXP Zephyr Device PM coverage statistics.

Answers one question: for each NXP board, which NXP IPs does its devicetree
describe, and does the driver behind each IP implement Device PM?

Pipeline:

  1. Read the `NXP Platform Drivers` files-regex block from MAINTAINERS.yml to
     get the set of driver sources in scope.
  2. Statically analyse each matching driver for a DEVICE*DEFINE registration,
     a PM device object or callback, and which PM_DEVICE_ACTION_* branches do
     real work rather than falling through.
  3. Walk boards/nxp/**/*.dts (following /include/ and #include), collect the
     `compatible` strings, and join them to the analysed drivers.
  4. Aggregate per board and overall.

Outputs, all under the report directory:

  index.html    interactive board/IP browser (loads boards.json at view time)
  boards.json   per-board IPs, statuses and driver details
  summary.json  aggregate counts by status, subsystem and board
  boards.csv    flat board x IP x status table, diff-friendly in git
  drivers.csv   per-driver analysis, one row per source file

Typical use:

  python device_pm.py --repo path/to/zephyr
  python device_pm.py --repo path/to/zephyr --show          # summary only
  python device_pm.py --repo path/to/zephyr --show frdm_mcxn947
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

MAINTAINERS_SECTION = "NXP Platform Drivers:"

# Subsystems whose drivers are out of scope: Wi-Fi and Bluetooth are mostly
# vendor blobs rather than Zephyr device drivers, and S32 is a separate
# maintainer group with its own PM story.
EXCLUDED_PATH_PREFIXES = ("drivers/wifi/", "drivers/bluetooth/")
EXCLUDED_PATH_PATTERN = re.compile(r"s32", re.IGNORECASE)

PM_ACTIONS = ("TURN_ON", "TURN_OFF", "SUSPEND", "RESUME")

DT_DRV_COMPAT_RE = re.compile(r"^\s*#\s*define\s+DT_DRV_COMPAT\s+([A-Za-z0-9_]+)", re.MULTILINE)
DEVICE_DEFINE_RE = re.compile(
    r"\b(?:[A-Z][A-Z0-9_]*_)?(?:DEVICE_DT_INST_DEFINE|DEVICE_DT_DEFINE|DEVICE_DEFINE)\s*\("
)
PM_DEFINE_RE = re.compile(r"\bPM_DEVICE_(?:DT_INST_DEFINE|DT_DEFINE|DEFINE)\s*\(")
PM_RUNTIME_RE = re.compile(r"\bpm_device_runtime_(?:enable|auto_enable)\s*\(")
PM_CALLBACK_RE = re.compile(
    r"\b(?:static\s+)?int\s+([A-Za-z0-9_]*pm[A-Za-z0-9_]*)\s*"
    r"\(\s*const\s+struct\s+device\s+\*\s*[^,]+,\s*enum\s+pm_device_action",
    re.IGNORECASE | re.MULTILINE,
)
COMPATIBLE_PROPERTY_RE = re.compile(r"\bcompatible\s*=\s*((?:\"[^\"]+\"\s*,?\s*)+);", re.MULTILINE)
INCLUDE_RE = re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE)

STATUS_ENABLED = "Enabled"
STATUS_RUNTIME_ONLY = "Runtime PM only"
STATUS_NOT_ENABLED = "Not enabled"
STATUS_PARTIAL = "Partial"
STATUS_NOT_APPLICABLE = "Not applicable"

# Worst first: this drives both the dashboard's default ordering and the CSV,
# so the rows that need attention are the ones you see.
STATUS_ORDER = {
    STATUS_NOT_ENABLED: 0,
    STATUS_NOT_APPLICABLE: 1,
    STATUS_PARTIAL: 2,
    STATUS_RUNTIME_ONLY: 3,
    STATUS_ENABLED: 4,
}


@dataclass
class Driver:
    source: str
    subsystem: str
    status: str
    actions: list[str]
    noop_actions: list[str]
    runtime_pm: bool
    callbacks: list[str]
    note: str


@dataclass
class BoardIp:
    compatible: str
    status: str
    actions: list[str]
    drivers: list[Driver]


@dataclass
class Board:
    name: str
    variants: list[str]
    ips: list[BoardIp] = field(default_factory=list)

    @property
    def applicable(self) -> int:
        return len(self.ips)

    @property
    def enabled(self) -> int:
        return sum(ip.status == STATUS_ENABLED for ip in self.ips)

    @property
    def coverage(self) -> Optional[float]:
        return (self.enabled / self.applicable) if self.applicable else None


# --------------------------------------------------------------------------
# Driver source analysis
# --------------------------------------------------------------------------


def strip_c_comments(code: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)


def braced_body(text: str, opening_brace: int) -> str:
    """Text from an opening brace through its matching close, inclusive."""

    depth = 0
    for offset, character in enumerate(text[opening_brace:], opening_brace):
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[opening_brace : offset + 1]
    return ""


def callback_body(text: str, callback: str) -> str:
    match = re.search(rf"\b{re.escape(callback)}\s*\([^;]*?\)\s*\{{", text, re.DOTALL)
    if not match:
        return ""
    return braced_body(text, match.end() - 1)


def is_noop_branch(code: str) -> bool:
    """True when a PM action branch does nothing observable.

    A driver that switches on the action but leaves SUSPEND empty is not
    suspending anything, so reporting the action as implemented would overstate
    coverage. Preprocessor lines, ARG_UNUSED and `(void)x;` casts are noise.
    """

    code = strip_c_comments(code)
    code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)
    code = re.sub(r"\bARG_UNUSED\s*\([^;]*\)\s*;", "", code)
    code = re.sub(r"\(\s*void\s*\)\s*[A-Za-z_]\w*\s*;", "", code)
    code = re.sub(r"[{};\s]", "", code)
    return code in {"", "break", "return0", "returntrue", "returnfalse"} or bool(
        re.fullmatch(r"return-?(?:E[A-Z0-9_]+|\d+)", code)
    )


def action_branch(body: str, action: str) -> Optional[str]:
    """The code a callback runs for one PM action, or None if it has no branch.

    Handles both `switch (action)` with fallthrough between empty cases and the
    `if (action == PM_DEVICE_ACTION_x)` form.
    """

    labels = list(
        re.finditer(
            r"\b(?:case\s+PM_DEVICE_ACTION_(TURN_ON|TURN_OFF|SUSPEND|RESUME)|default)\s*:", body
        )
    )
    for index, label in enumerate(labels):
        if label.group(1) != action:
            continue
        end = labels[index + 1].start() if index + 1 < len(labels) else len(body)
        branch = body[label.end() : end]
        # An empty case falls through to the next one, which is the code that
        # actually runs for this action.
        while not strip_c_comments(branch).strip() and index + 1 < len(labels):
            index += 1
            end = labels[index + 1].start() if index + 1 < len(labels) else len(body)
            branch = body[labels[index].end() : end]
        return branch

    conditional = re.search(
        rf"\b(?:else\s+)?if\s*\(\s*action\s*==\s*PM_DEVICE_ACTION_{action}\s*\)", body
    )
    if conditional:
        opening_brace = body.find("{", conditional.end())
        if opening_brace != -1:
            return braced_body(body, opening_brace)
    return None


def detect_actions(text: str, callbacks: list[str]) -> tuple[list[str], list[str]]:
    """(actions that do work, actions whose branch is an explicit no-op)."""

    effective: set[str] = set()
    noops: set[str] = set()
    for callback in callbacks:
        body = callback_body(text, callback)
        for action in PM_ACTIONS:
            branch = action_branch(body, action)
            if branch is None:
                continue
            if is_noop_branch(branch):
                noops.add(action)
            else:
                effective.add(action)

    if not callbacks:
        # No callback was matched, so fall back to whichever action constants
        # the source mentions. Comments are stripped first: a commented-out or
        # documented action is not an implemented one.
        code = strip_c_comments(text)
        effective = {action for action in PM_ACTIONS if f"PM_DEVICE_ACTION_{action}" in code}

    return sorted(effective), sorted(noops)


def explain(
    text: str,
    callbacks: list[str],
    actions: list[str],
    noop_actions: list[str],
    has_pm: bool,
    has_device: bool,
) -> str:
    if not has_device:
        return (
            "No DEVICE*DEFINE registration was detected in this matched driver source. "
            "Classified as Not enabled by this report's policy."
        )
    if actions or noop_actions:
        notes = []
        if actions:
            notes.append(f"Implemented PM actions: {', '.join(actions)}.")
        if noop_actions:
            notes.append(f"Explicit no-op PM branch(es), reported as No: {', '.join(noop_actions)}.")
        return " ".join(notes)
    if callbacks and not has_pm and "pm_device_driver_init(" in text:
        return (
            "Legacy Device PM pattern: pm_device_driver_init() registers an action callback, "
            "but DEVICE_*DEFINE uses a NULL PM argument and no PM_DEVICE_*DEFINE object is present. "
            "The callback ignores standard lifecycle actions; source review is required."
        )
    if not has_pm and not callbacks:
        return "No PM device object or PM callback detected in this driver."
    if not callbacks:
        return "PM device object found, but the PM callback body was not identified by the static scan."
    if "ARG_UNUSED(action)" in callback_body(text, callbacks[0]):
        return (
            "PM stub callback: the action parameter is intentionally ignored. "
            "No Device PM lifecycle transition is implemented; the PM object provides "
            "pm_base/wakeup support."
        )
    return (
        "PM callback is registered, but no standard PM_DEVICE_ACTION_* branch was detected "
        "in the callback body; review the callback or macro expansion."
    )


def analyse_driver(path: str, text: str) -> Driver:
    has_device = bool(DEVICE_DEFINE_RE.search(text))
    has_pm = bool(PM_DEFINE_RE.search(text))
    has_runtime = bool(PM_RUNTIME_RE.search(text))
    callbacks = PM_CALLBACK_RE.findall(text)
    actions, noop_actions = detect_actions(text, callbacks)

    if has_device and (has_pm or (callbacks and "pm_device_driver_init(" in text)):
        status = STATUS_ENABLED
    elif has_device and has_runtime:
        status = STATUS_RUNTIME_ONLY
    else:
        status = STATUS_NOT_ENABLED

    return Driver(
        source=path,
        subsystem=Path(path).parts[1].upper(),
        status=status,
        actions=actions,
        noop_actions=noop_actions,
        runtime_pm=has_runtime,
        callbacks=callbacks,
        note=explain(text, callbacks, actions, noop_actions, has_pm, has_device),
    )


# --------------------------------------------------------------------------
# Zephyr tree access
# --------------------------------------------------------------------------


class Zephyr:
    def __init__(self, root: Path):
        self.root = root
        if not (root / "MAINTAINERS.yml").is_file():
            raise SystemExit(f"error: {root} does not look like a Zephyr checkout (no MAINTAINERS.yml)")
        self.include_roots = [
            root / "dts",
            root / "dts" / "arm",
            root / "dts" / "arm64",
            root / "dts" / "xtensa",
            root / "dts" / "riscv",
        ]

    def git(self, *args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(self.root), *args], text=True, encoding="utf-8", errors="replace"
            ).strip()
        except FileNotFoundError:
            raise SystemExit("error: 'git' not found on PATH")
        except subprocess.CalledProcessError as error:
            raise SystemExit(f"error: git failed in {self.root}: {error}")

    def scope_patterns(self) -> list[re.Pattern]:
        """The files-regex entries under the NXP Platform Drivers maintainer block."""

        lines = (self.root / "MAINTAINERS.yml").read_text(encoding="utf-8").splitlines()
        try:
            start = next(i for i, line in enumerate(lines) if line == MAINTAINERS_SECTION)
        except StopIteration:
            raise SystemExit(
                f"error: '{MAINTAINERS_SECTION}' not found in MAINTAINERS.yml; "
                "the maintainer block may have been renamed upstream"
            )

        patterns: list[str] = []
        in_regexes = False
        for line in lines[start + 1 :]:
            if line and not line.startswith((" ", "\t")):
                break  # next top-level block
            if line.strip() == "files-regex:":
                in_regexes = True
                continue
            match = re.match(r"\s*-\s+(.+)$", line) if in_regexes else None
            if match:
                patterns.append(match.group(1))
            elif in_regexes and line.strip() and not line.startswith("    -"):
                break

        if not patterns:
            raise SystemExit(f"error: no files-regex entries under '{MAINTAINERS_SECTION}'")
        return [re.compile(pattern) for pattern in patterns]

    def in_scope(self, path: str, patterns: list[re.Pattern]) -> bool:
        if not path.endswith(".c") or path.startswith(EXCLUDED_PATH_PREFIXES):
            return False
        if EXCLUDED_PATH_PATTERN.search(path):
            return False
        return any(pattern.search(path) for pattern in patterns)

    def dts_text(self, path: Path, visited: Optional[set[Path]] = None) -> str:
        """A DTS file concatenated with everything it includes, transitively."""

        visited = visited if visited is not None else set()
        path = path.resolve()
        if path in visited:
            return ""
        visited.add(path)

        text = path.read_text(encoding="utf-8", errors="replace")
        included = []
        for name in INCLUDE_RE.findall(text):
            candidates = [path.parent / name] + [root / name for root in self.include_roots]
            resolved = next((c for c in candidates if c.is_file()), None)
            if resolved:
                included.append(self.dts_text(resolved, visited))
        return "\n".join(included + [text])

    def dts_compatibles(self, path: Path) -> set[str]:
        compatibles: set[str] = set()
        for declaration in COMPATIBLE_PROPERTY_RE.findall(self.dts_text(path)):
            compatibles.update(re.findall(r'"([^"]+)"', declaration))
        return compatibles


def normalise_compatible(value: str) -> str:
    """DTS 'nxp,lpc-lpadc' and C 'nxp_lpc_lpadc' name the same binding."""

    return value.replace(",", "_").replace("-", "_").lower()


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def driver_catalogue(zephyr: Zephyr) -> dict[str, list[Driver]]:
    """compatible -> drivers implementing it, keyed by normalised name."""

    patterns = zephyr.scope_patterns()
    catalogue: dict[str, list[Driver]] = defaultdict(list)
    for path in zephyr.git("ls-files", "drivers").splitlines():
        if not zephyr.in_scope(path, patterns):
            continue
        text = (zephyr.root / path).read_text(encoding="utf-8", errors="replace")
        compat = DT_DRV_COMPAT_RE.search(text)
        if not compat:
            continue
        catalogue[normalise_compatible(compat.group(1))].append(analyse_driver(path, text))
    return catalogue


def roll_up_status(statuses: set[str]) -> str:
    """Board-level status for one IP backed by possibly several drivers."""

    applicable = statuses - {STATUS_NOT_APPLICABLE}
    if not applicable:
        return STATUS_NOT_APPLICABLE
    if applicable == {STATUS_ENABLED}:
        return STATUS_ENABLED
    if STATUS_ENABLED in applicable:
        return STATUS_PARTIAL
    if STATUS_RUNTIME_ONLY in applicable:
        return STATUS_RUNTIME_ONLY
    return STATUS_NOT_ENABLED


def collect_boards(zephyr: Zephyr, catalogue: dict[str, list[Driver]]) -> list[Board]:
    raw: dict[str, dict] = {}
    for dts in (zephyr.root / "boards" / "nxp").rglob("*.dts"):
        if "common" in dts.parts:
            continue
        board = dts.parent.name
        entry = raw.setdefault(board, {"variants": [], "ips": {}})
        entry["variants"].append(dts.stem)
        for compatible in zephyr.dts_compatibles(dts):
            drivers = catalogue.get(normalise_compatible(compatible))
            if not drivers:
                continue
            ip = entry["ips"].setdefault(compatible, {"drivers": [], "sources": set()})
            for driver in drivers:
                if driver.source not in ip["sources"]:
                    ip["drivers"].append(driver)
                    ip["sources"].add(driver.source)

    boards = []
    for name, entry in raw.items():
        ips = []
        for compatible, ip in entry["ips"].items():
            drivers = ip["drivers"]
            ips.append(
                BoardIp(
                    compatible=compatible,
                    status=roll_up_status({driver.status for driver in drivers}),
                    actions=sorted({action for driver in drivers for action in driver.actions}),
                    drivers=drivers,
                )
            )
        ips.sort(key=lambda ip: (STATUS_ORDER[ip.status], ip.compatible))
        boards.append(Board(name=name, variants=sorted(entry["variants"]), ips=ips))

    return sorted(boards, key=lambda board: board.name)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def unique_drivers(catalogue: dict[str, list[Driver]]) -> dict[str, Driver]:
    """Analysed drivers keyed by source path.

    A driver source can implement several compatibles, so it appears in more
    than one catalogue entry; the analysis is per file, so deduplicate on it.
    """

    return {driver.source: driver for group in catalogue.values() for driver in group}


def summarise(boards: list[Board], catalogue: dict[str, list[Driver]]) -> dict:
    mapping_status = Counter(ip.status for board in boards for ip in board.ips)
    total_mappings = sum(mapping_status.values())

    drivers = unique_drivers(catalogue)
    driver_status = Counter(driver.status for driver in drivers.values())

    by_subsystem: dict[str, Counter] = defaultdict(Counter)
    for driver in drivers.values():
        by_subsystem[driver.subsystem][driver.status] += 1

    action_counts = Counter(
        action for driver in drivers.values() for action in driver.actions
    )

    covered = [board for board in boards if board.applicable]
    return {
        "boards": len(boards),
        "boards_with_mapped_ips": len(covered),
        "board_ip_mappings": total_mappings,
        "mapping_status": dict(mapping_status),
        "mapping_enabled_share": (
            mapping_status[STATUS_ENABLED] / total_mappings if total_mappings else None
        ),
        "drivers": len(drivers),
        "compatibles": len(catalogue),
        "driver_status": dict(driver_status),
        "driver_action_counts": {action: action_counts.get(action, 0) for action in PM_ACTIONS},
        "drivers_by_subsystem": {
            subsystem: dict(counts) for subsystem, counts in sorted(by_subsystem.items())
        },
        "boards_fully_enabled": sum(
            1 for board in covered if board.enabled == board.applicable
        ),
        "boards_without_any_pm": sum(1 for board in covered if board.enabled == 0),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_outputs(
    report_dir: Path,
    zephyr: Zephyr,
    boards: list[Board],
    catalogue: dict[str, list[Driver]],
    generated_at: str,
) -> dict:
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = summarise(boards, catalogue)
    meta = {
        "generated_at": generated_at,
        "zephyr_commit": zephyr.git("rev-parse", "--short", "HEAD"),
        "scope": (
            f"'{MAINTAINERS_SECTION}' files-regex in MAINTAINERS.yml; "
            "Wi-Fi, Bluetooth and S32 excluded"
        ),
    }

    (report_dir / "boards.json").write_text(
        json.dumps(
            {
                "meta": meta,
                # Drivers are emitted once and referenced by source path: the
                # same driver backs an IP on dozens of boards, and inlining its
                # note everywhere inflated this file several times over.
                "drivers": {
                    driver.source: {
                        "subsystem": driver.subsystem,
                        "status": driver.status,
                        "actions": driver.actions,
                        "noop_actions": driver.noop_actions,
                        "runtime_pm": driver.runtime_pm,
                        "note": driver.note,
                    }
                    for driver in unique_drivers(catalogue).values()
                },
                "boards": [
                    {
                        "name": board.name,
                        "variants": board.variants,
                        "applicable": board.applicable,
                        "enabled": board.enabled,
                        "coverage": board.coverage,
                        "ips": [
                            {
                                "compatible": ip.compatible,
                                "status": ip.status,
                                "actions": ip.actions,
                                "sources": [driver.source for driver in ip.drivers],
                            }
                            for ip in board.ips
                        ],
                    }
                    for board in boards
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    (report_dir / "summary.json").write_text(
        json.dumps({"meta": meta, "summary": summary}, indent=2), encoding="utf-8"
    )

    # Flat board x IP table: this is what the Excel workbook used to be for,
    # in a format git can diff.
    with (report_dir / "boards.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["board", "variants", "compatible", "status", *PM_ACTIONS, "drivers", "subsystems"]
        )
        for board in boards:
            for ip in board.ips:
                writer.writerow(
                    [
                        board.name,
                        " ".join(board.variants),
                        ip.compatible,
                        ip.status,
                        *["Yes" if action in ip.actions else "No" for action in PM_ACTIONS],
                        " ".join(driver.source for driver in ip.drivers),
                        " ".join(sorted({driver.subsystem for driver in ip.drivers})),
                    ]
                )

    drivers = sorted(
        unique_drivers(catalogue).values(),
        key=lambda driver: (STATUS_ORDER[driver.status], driver.source),
    )
    with (report_dir / "drivers.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["source", "subsystem", "status", *PM_ACTIONS, "noop_actions", "runtime_pm", "note"]
        )
        for driver in drivers:
            writer.writerow(
                [
                    driver.source,
                    driver.subsystem,
                    driver.status,
                    *["Yes" if action in driver.actions else "No" for action in PM_ACTIONS],
                    " ".join(driver.noop_actions),
                    "Yes" if driver.runtime_pm else "No",
                    driver.note,
                ]
            )

    (report_dir / "index.html").write_text(DASHBOARD_HTML, encoding="utf-8")
    return summary


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NXP Zephyr Device PM Coverage</title><style>
:root{--blue:#173f73;--blue2:#4472c4;--ink:#19212c;--muted:#667085;--bg:#f5f7fa;--line:#d9e0ea;--green:#147d50;--red:#bd3030;--amber:#a15c00;--grey:#697586}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Arial,sans-serif}
header{background:var(--blue);color:#fff;padding:32px max(24px,calc((100% - 1440px)/2))}h1{font-size:28px;margin:0 0 7px}header p{margin:0;color:#dbeafe}
main{max-width:1440px;margin:auto;padding:24px}
.note{background:#eef5ff;border-left:4px solid var(--blue2);padding:13px 16px;margin:0 0 20px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:15px;margin-bottom:22px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px}.kpi b{display:block;font-size:27px;color:var(--blue)}.kpi span{color:var(--muted)}
.filters{display:flex;gap:10px;align-items:center;margin:16px 0;flex-wrap:wrap}
input,select,button{font:inherit;padding:9px 11px;border:1px solid #b6c1d0;border-radius:5px;background:#fff}
input{width:min(450px,100%)}button{cursor:pointer;color:#fff;background:var(--blue2);border-color:var(--blue2)}
#count{margin-left:auto;color:var(--muted)}
.board{background:#fff;border:1px solid var(--line);border-radius:8px;margin:10px 0;overflow:hidden}
summary{cursor:pointer;padding:14px 16px;display:flex;align-items:center;gap:12px;list-style:none}summary::-webkit-details-marker{display:none}
.board-name{font-weight:bold;font-size:17px;min-width:220px}.variant{color:var(--muted);font-size:12px;flex:1}
.coverage{font-weight:bold;color:var(--blue)}.bar{height:7px;width:110px;background:#e5eaf2;border-radius:4px;overflow:hidden}.bar i{height:100%;display:block;background:var(--green)}
.table-wrap{overflow-x:auto;border-top:1px solid var(--line)}table{border-collapse:collapse;width:100%;min-width:840px}
th{background:#f0f4f9;color:#344054;text-align:left;font-size:12px;padding:9px 12px}td{border-top:1px solid #edf0f4;padding:9px 12px;vertical-align:top}
.compat{font-family:Consolas,monospace;font-size:12px}
.badge{display:inline-block;border-radius:12px;padding:2px 8px;font-weight:bold;font-size:12px;white-space:nowrap}
.enabled{background:#d9f2e5;color:var(--green)}.not-enabled{background:#fde4e4;color:var(--red)}
.runtime-pm-only,.partial,.not-applicable{background:#fff0d6;color:var(--amber)}
.action{display:inline-block;margin:1px 3px 1px 0;border:1px solid #bcd1e9;background:#eef5ff;color:#24528b;border-radius:3px;padding:1px 5px;font-size:11px}
.none{color:var(--grey)}.source{font-family:Consolas,monospace;font-size:11px;color:#4b5563}
.note-cell{color:var(--muted);font-size:12px;max-width:520px}
footer{color:var(--muted);font-size:12px;padding:20px 0}.error{color:var(--red);font-weight:bold}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}.variant,.bar{display:none}summary{gap:8px}.board-name{min-width:0;flex:1}#count{margin-left:0}}
</style></head><body>
<header><h1>NXP Zephyr Device PM Coverage</h1><p>Board-level view of NXP IPs described by Devicetree and their static Device PM implementation status.</p></header>
<main>
<div class="note"><b>Reading this report.</b> Each board lists the IPs whose DTS <code>compatible</code> resolves to an NXP Platform Drivers implementation. PM actions are detected from the driver source; a branch that does nothing observable is reported as not implemented. Sources with no <code>DEVICE*DEFINE</code> registration are classified as Not enabled. Legacy <code>pm_device_driver_init()</code> callbacks count as enabled. Disabled DTS nodes are included because they still describe available SoC IP.</div>
<section class="kpis" id="kpis"></section>
<div class="filters">
  <input id="search" placeholder="Search board, compatible, or driver path..." autofocus>
  <select id="status"><option value="">All PM states</option></select>
  <button id="expand">Expand all</button>
  <a id="csv" href="boards.csv" style="color:var(--blue2)">boards.csv</a>
  <a id="json" href="summary.json" style="color:var(--blue2)">summary.json</a>
  <span id="count"></span>
</div>
<section id="boards"><p id="loading">Loading report data&hellip;</p></section>
<footer id="footer"></footer>
</main>
<script>
const root = document.querySelector('#boards'), search = document.querySelector('#search');
const statusPicker = document.querySelector('#status'), count = document.querySelector('#count');
let boards = [], drivers = {}, meta = {};

const escapeHtml = value => {
  const element = document.createElement('span');
  element.textContent = value === undefined || value === null ? '' : value;
  return element.innerHTML;
};
const badge = status => `<span class="badge ${status.toLowerCase().replaceAll(' ', '-')}">${escapeHtml(status)}</span>`;
const actionTags = list => list.length
  ? list.map(a => `<span class="action">${escapeHtml(a.replace('_', ' '))}</span>`).join('')
  : '<span class="none">Not detected</span>';
// Drivers are stored once and referenced by source path; an unknown path would
// otherwise render as "undefined".
const driverOf = source => drivers[source] || {subsystem: '?', status: '?', note: ''};

function render() {
  const query = search.value.trim().toLowerCase(), want = statusPicker.value;
  let shown = 0;
  root.innerHTML = boards.map(board => {
    const ips = board.ips
      .filter(ip => !want || ip.status === want)
      .filter(ip => !query || `${board.name} ${board.variants.join(' ')} ${ip.compatible} `
        + `${ip.sources.join(' ')}`.toLowerCase().includes(query));
    if (!ips.length && (query || want)) return '';
    shown++;
    const enabled = ips.filter(ip => ip.status === 'Enabled').length;
    const pct = ips.length ? enabled / ips.length * 100 : 0;
    return `<details class="board"><summary>`
      + `<span class="board-name">${escapeHtml(board.name)}</span>`
      + `<span class="variant">${escapeHtml(board.variants.join(', '))}</span>`
      + `<span>${ips.length} IPs</span>`
      + `<span class="coverage">${enabled} / ${ips.length} enabled</span>`
      + `<span class="bar"><i style="width:${pct}%"></i></span></summary>`
      + `<div class="table-wrap"><table><thead><tr><th>DT compatible</th><th>PM status</th>`
      + `<th>Implemented PM actions</th><th>Driver / subsystem</th><th>Notes</th></tr></thead><tbody>`
      + ips.map(ip => `<tr><td class="compat">${escapeHtml(ip.compatible)}</td>`
        + `<td>${badge(ip.status)}</td><td>${actionTags(ip.actions)}</td>`
        + `<td>${ip.sources.map(source => `<div><span class="source">${escapeHtml(source)}</span>`
          + ` (${escapeHtml(driverOf(source).subsystem)}; ${escapeHtml(driverOf(source).status)})</div>`).join('')}</td>`
        + `<td class="note-cell">${[...new Set(ip.sources.map(s => driverOf(s).note))].map(escapeHtml).join('<br>')}</td>`
        + `</tr>`).join('')
      + `</tbody></table></div></details>`;
  }).join('');
  count.textContent = `${shown} of ${boards.length} boards shown`;
}

Promise.all([
  fetch('boards.json', {cache: 'no-cache'}).then(r => r.json()),
  fetch('summary.json', {cache: 'no-cache'}).then(r => r.json()),
]).then(([boardData, summaryData]) => {
  boards = boardData.boards || [];
  drivers = boardData.drivers || {};
  meta = boardData.meta || {};
  const s = summaryData.summary;
  const share = s.mapping_enabled_share == null ? '\\u2014' : (s.mapping_enabled_share * 100).toFixed(1) + '%';
  document.querySelector('#kpis').innerHTML =
      `<div class="kpi"><b>${s.boards}</b><span>NXP boards</span></div>`
    + `<div class="kpi"><b>${s.board_ip_mappings}</b><span>Board-to-IP mappings</span></div>`
    + `<div class="kpi"><b>${share}</b><span>Fully enabled IP mappings</span></div>`
    + `<div class="kpi"><b>${s.mapping_status['Not enabled'] || 0}</b><span>IP mappings without Device PM</span></div>`;
  statusPicker.innerHTML = '<option value="">All PM states</option>'
    + Object.keys(s.mapping_status).sort().map(k => `<option>${escapeHtml(k)}</option>`).join('');
  document.querySelector('#footer').textContent =
    `Generated ${meta.generated_at} from Zephyr commit ${meta.zephyr_commit}. Scope: ${meta.scope}.`;
  render();
}).catch(() => {
  root.innerHTML = '<p class="error">Unable to load report data (boards.json / summary.json).</p>';
});

search.addEventListener('input', render);
statusPicker.addEventListener('change', render);
document.querySelector('#expand').addEventListener('click', event => {
  const collapse = event.target.textContent === 'Collapse all';
  document.querySelectorAll('details').forEach(d => { d.open = !collapse; });
  event.target.textContent = collapse ? 'Expand all' : 'Collapse all';
});
</script></body></html>"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def print_summary(summary: dict, boards: list[Board], board_filter: Optional[str]) -> None:
    if board_filter:
        matches = [board for board in boards if board_filter.lower() in board.name.lower()]
        if not matches:
            raise SystemExit(f"error: no board matching {board_filter!r}")
        for board in matches:
            print(f"\n{board.name}  ({', '.join(board.variants)})")
            print(f"  {board.enabled} of {board.applicable} IP mappings fully enabled")
            width = max((len(ip.compatible) for ip in board.ips), default=10)
            for ip in board.ips:
                actions = ", ".join(ip.actions) or "-"
                print(f"    {ip.compatible:<{width}}  {ip.status:<16}  {actions}")
        return

    print(f"\nBoards:               {summary['boards']}")
    print(f"Board-to-IP mappings: {summary['board_ip_mappings']}")
    for status, total in sorted(summary["mapping_status"].items(), key=lambda kv: -kv[1]):
        share = total / summary["board_ip_mappings"] * 100
        print(f"  {status:<18} {total:>6}  {share:>5.1f}%")
    print(f"\nDrivers analysed:     {summary['drivers']} across {summary['compatibles']} compatibles")
    for status, total in sorted(summary["driver_status"].items(), key=lambda kv: -kv[1]):
        print(f"  {status:<18} {total:>6}")
    print("\nDrivers implementing each PM action:")
    for action, total in summary["driver_action_counts"].items():
        print(f"  {action:<10} {total:>6}")
    print(f"\nBoards with every mapped IP enabled: {summary['boards_fully_enabled']}")
    print(f"Boards with no Device PM at all:     {summary['boards_without_any_pm']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NXP Zephyr Device PM coverage statistics.")
    parser.add_argument(
        "--repo",
        default=os.environ.get("ZEPHYR_PATH", os.getcwd()),
        help="Zephyr checkout to analyse (default: $ZEPHYR_PATH or cwd)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get(
            "DEVICE_PM_OUTPUT_DIR", str(Path(__file__).parents[2] / "reports" / "device-pm")
        ),
        help="Where the report and data files go (default: $DEVICE_PM_OUTPUT_DIR)",
    )
    parser.add_argument(
        "--show",
        nargs="?",
        const="",
        metavar="BOARD",
        help="Print statistics instead of writing files; optionally filter to one board",
    )
    args = parser.parse_args(argv)

    zephyr = Zephyr(Path(args.repo).resolve())
    print(f"Scanning {zephyr.root}...", file=sys.stderr)
    catalogue = driver_catalogue(zephyr)
    boards = collect_boards(zephyr, catalogue)

    if args.show is not None:
        print_summary(summarise(boards, catalogue), boards, args.show or None)
        return 0

    report_dir = Path(args.output_dir)
    summary = write_outputs(
        report_dir,
        zephyr,
        boards,
        catalogue,
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    print(f"Wrote index.html, boards.json, summary.json, boards.csv, drivers.csv to {report_dir}")
    print(
        f"  {summary['boards']} boards, {summary['board_ip_mappings']} board-to-IP mappings, "
        f"{summary['drivers']} drivers"
    )
    print(f"  statuses: {summary['mapping_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
