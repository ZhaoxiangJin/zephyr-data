#!/usr/bin/env python3
"""Tests for device_pm.py, run against a synthetic Zephyr tree.

The value in this tool is the static analysis of driver sources, and its rules
are subtle: an empty `case PM_DEVICE_ACTION_SUSPEND:` means the driver does not
suspend anything, but an empty case that falls through to the next one means it
does. A synthetic tree pins that behaviour down with sources written to hit each
rule exactly once.

Run: python tools/device-pm/test_device_pm.py
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

import device_pm as dpm


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + ("" if condition else f" -- {detail}"))
    if not condition:
        FAILURES.append(label)


def check_equal(label: str, actual, expected) -> None:
    check(label, actual == expected, f"expected {expected!r}, got {actual!r}")


# --------------------------------------------------------------------------
# Synthetic driver sources, one per analysis rule
# --------------------------------------------------------------------------

FULL_PM = """
#define DT_DRV_COMPAT nxp_full
static int full_pm_action(const struct device *dev, enum pm_device_action action)
{
    switch (action) {
    case PM_DEVICE_ACTION_TURN_ON:
        clock_on(dev);
        break;
    case PM_DEVICE_ACTION_TURN_OFF:
        clock_off(dev);
        break;
    case PM_DEVICE_ACTION_SUSPEND:
        save_state(dev);
        break;
    case PM_DEVICE_ACTION_RESUME:
        restore_state(dev);
        break;
    default:
        return -ENOTSUP;
    }
    return 0;
}
PM_DEVICE_DT_INST_DEFINE(0, full_pm_action);
DEVICE_DT_INST_DEFINE(0, init, PM_DEVICE_DT_INST_GET(0), NULL, NULL, POST_KERNEL, 50, &api);
"""

# SUSPEND does nothing observable; TURN_OFF only returns an error code. Neither
# is a working transition, so both must be reported as no-ops.
NOOP_BRANCHES = """
#define DT_DRV_COMPAT nxp_noop
static int noop_pm_action(const struct device *dev, enum pm_device_action action)
{
    switch (action) {
    case PM_DEVICE_ACTION_RESUME:
        restore_state(dev);
        break;
    case PM_DEVICE_ACTION_SUSPEND:
        /* nothing to do */
        break;
    case PM_DEVICE_ACTION_TURN_OFF:
        return -ENOSYS;
    default:
        return -ENOTSUP;
    }
    return 0;
}
PM_DEVICE_DT_INST_DEFINE(0, noop_pm_action);
DEVICE_DT_INST_DEFINE(0, init, PM_DEVICE_DT_INST_GET(0), NULL, NULL, POST_KERNEL, 50, &api);
"""

# TURN_OFF is empty but falls through to SUSPEND, so it does run real code.
FALLTHROUGH = """
#define DT_DRV_COMPAT nxp_fallthrough
static int ft_pm_action(const struct device *dev, enum pm_device_action action)
{
    switch (action) {
    case PM_DEVICE_ACTION_TURN_OFF:
    case PM_DEVICE_ACTION_SUSPEND:
        quiesce(dev);
        break;
    default:
        return -ENOTSUP;
    }
    return 0;
}
PM_DEVICE_DT_INST_DEFINE(0, ft_pm_action);
DEVICE_DT_INST_DEFINE(0, init, PM_DEVICE_DT_INST_GET(0), NULL, NULL, POST_KERNEL, 50, &api);
"""

IF_FORM = """
#define DT_DRV_COMPAT nxp_ifform
static int if_pm_action(const struct device *dev, enum pm_device_action action)
{
    if (action == PM_DEVICE_ACTION_RESUME) {
        power_up(dev);
        return 0;
    } else if (action == PM_DEVICE_ACTION_SUSPEND) {
        /* deliberately empty */
    }
    return -ENOTSUP;
}
PM_DEVICE_DT_INST_DEFINE(0, if_pm_action);
DEVICE_DT_INST_DEFINE(0, init, PM_DEVICE_DT_INST_GET(0), NULL, NULL, POST_KERNEL, 50, &api);
"""

RUNTIME_ONLY = """
#define DT_DRV_COMPAT nxp_runtime
static int init(const struct device *dev)
{
    pm_device_runtime_enable(dev);
    return 0;
}
DEVICE_DT_INST_DEFINE(0, init, NULL, NULL, NULL, POST_KERNEL, 50, &api);
"""

NO_PM = """
#define DT_DRV_COMPAT nxp_nopm
static int init(const struct device *dev) { return 0; }
DEVICE_DT_INST_DEFINE(0, init, NULL, NULL, NULL, POST_KERNEL, 50, &api);
"""

# Legacy pattern: the PM object is absent but pm_device_driver_init registers a
# callback, which this report counts as enabled.
LEGACY = """
#define DT_DRV_COMPAT nxp_legacy
static int legacy_pm_action(const struct device *dev, enum pm_device_action action)
{
    ARG_UNUSED(action);
    return 0;
}
static int init(const struct device *dev)
{
    return pm_device_driver_init(dev, legacy_pm_action);
}
DEVICE_DT_INST_DEFINE(0, init, NULL, NULL, NULL, POST_KERNEL, 50, &api);
"""

NO_REGISTRATION = """
#define DT_DRV_COMPAT nxp_helper
static int helper(void) { return 0; }
"""

# Commented-out PM code must not count: comments are stripped before analysis.
COMMENTED_OUT = """
#define DT_DRV_COMPAT nxp_commented
/*
 * case PM_DEVICE_ACTION_SUSPEND:
 *     save_state(dev);
 */
static int init(const struct device *dev) { return 0; }
DEVICE_DT_INST_DEFINE(0, init, NULL, NULL, NULL, POST_KERNEL, 50, &api);
"""

DRIVERS = {
    "drivers/adc/adc_nxp_full.c": FULL_PM,
    "drivers/i2c/i2c_nxp_noop.c": NOOP_BRANCHES,
    "drivers/spi/spi_nxp_fallthrough.c": FALLTHROUGH,
    "drivers/pwm/pwm_nxp_ifform.c": IF_FORM,
    "drivers/gpio/gpio_nxp_runtime.c": RUNTIME_ONLY,
    "drivers/serial/uart_nxp_nopm.c": NO_PM,
    "drivers/counter/counter_nxp_legacy.c": LEGACY,
    "drivers/misc/misc_nxp_helper.c": NO_REGISTRATION,
    "drivers/dma/dma_nxp_commented.c": COMMENTED_OUT,
    # Out of scope by path, even though they match the name pattern.
    "drivers/wifi/wifi_nxp_thing.c": FULL_PM.replace("nxp_full", "nxp_wifi"),
    "drivers/bluetooth/bt_nxp_thing.c": FULL_PM.replace("nxp_full", "nxp_bt"),
    "drivers/can/can_nxp_s32_thing.c": FULL_PM.replace("nxp_full", "nxp_s32"),
    # Out of scope because the scope regex does not match it at all.
    "drivers/adc/adc_other_vendor.c": FULL_PM.replace("nxp_full", "other_thing"),
}

MAINTAINERS = """\
Some Other Area:
  status: maintained
  files:
    - drivers/other/

NXP Platform Drivers:
  status: maintained
  maintainers:
    - somebody
  files-regex:
    - drivers/.*/.*_nxp_.*
    - drivers/.*/.*nxp.*
  labels:
    - platform: NXP

Trailing Area:
  status: maintained
"""

# One SoC include shared by two boards, so include-following is exercised.
SOC_DTSI = """
/ {
    soc {
        adc0: adc@1000 { compatible = "nxp,full"; };
        i2c0: i2c@2000 { compatible = "nxp,noop"; };
        wifi0: wifi@9000 { compatible = "nxp,wifi"; };
    };
};
"""

BOARD_A_DTS = """
#include "soc_common.dtsi"
/ {
    model = "Board A";
    chosen { };
    spi0: spi@3000 { compatible = "nxp,fallthrough"; };
    uart0: uart@4000 { compatible = "nxp,nopm"; };
};
"""

BOARD_B_DTS = """
#include "soc_common.dtsi"
/ {
    model = "Board B";
    gpio0: gpio@5000 { compatible = "nxp,runtime"; };
    helper0: helper@6000 { compatible = "nxp,helper"; };
    unknown0: unknown@7000 { compatible = "vendor,not-a-driver"; };
};
"""

# Two sources claim this compatible with different statuses, so the board-level
# roll-up must report Partial.
PARTIAL_A = FULL_PM.replace("nxp_full", "nxp_shared").replace("full_pm_action", "shared_a_action")
PARTIAL_B = NO_PM.replace("nxp_nopm", "nxp_shared")
BOARD_C_DTS = """
/ {
    model = "Board C";
    shared0: shared@8000 { compatible = "nxp,shared"; };
};
"""


def run(repo: Path, args: list[str]) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8")


def build_tree(root: Path) -> Path:
    repo = root / "zephyr"
    (repo / "boards" / "nxp").mkdir(parents=True)
    (repo / "MAINTAINERS.yml").write_text(MAINTAINERS, encoding="utf-8")

    sources = dict(DRIVERS)
    sources["drivers/adc/adc_nxp_shared_a.c"] = PARTIAL_A
    sources["drivers/adc/adc_nxp_shared_b.c"] = PARTIAL_B
    for path, text in sources.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    (repo / "dts").mkdir(exist_ok=True)
    for board, dts in [("board_a", BOARD_A_DTS), ("board_b", BOARD_B_DTS), ("board_c", BOARD_C_DTS)]:
        directory = repo / "boards" / "nxp" / board
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{board}.dts").write_text(dts, encoding="utf-8")
        if "soc_common.dtsi" in dts:
            (directory / "soc_common.dtsi").write_text(SOC_DTSI, encoding="utf-8")
    # A variant of board_a, and a common/ directory that must be skipped.
    (repo / "boards" / "nxp" / "board_a" / "board_a_variant.dts").write_text(
        BOARD_A_DTS, encoding="utf-8"
    )
    common = repo / "boards" / "nxp" / "common"
    common.mkdir(exist_ok=True)
    (common / "common.dts").write_text(BOARD_A_DTS, encoding="utf-8")

    run(repo, ["init", "-q", "-b", "main"])
    run(repo, ["config", "user.email", "t@example.com"])
    run(repo, ["config", "user.name", "T"])
    run(repo, ["add", "-A"])
    run(repo, ["commit", "-q", "--no-verify", "-m", "synthetic tree"])
    return repo


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_low_level_helpers() -> None:
    print("\nbranch classification")
    for code, expected in [
        ("", True),
        ("break;", True),
        ("{ }", True),
        ("return 0;", True),
        ("return -ENOSYS;", True),
        ("return -ENOTSUP;", True),
        ("/* nothing to do */ break;", True),
        ("ARG_UNUSED(dev); break;", True),
        ("(void)dev; break;", True),
        ("#if CONFIG_X\n#endif\nbreak;", True),
        ("save_state(dev); break;", False),
        ("if (x) { y(); } break;", False),
    ]:
        check_equal(f"is_noop_branch {code.strip()[:34]!r}", dpm.is_noop_branch(code), expected)

    print("\ncompatible normalisation")
    check_equal("dts form", dpm.normalise_compatible("nxp,lpc-lpadc"), "nxp_lpc_lpadc")
    check_equal("c form", dpm.normalise_compatible("nxp_lpc_lpadc"), "nxp_lpc_lpadc")
    check_equal("case folded", dpm.normalise_compatible("NXP,LPC-LPADC"), "nxp_lpc_lpadc")

    print("\nstatus roll-up")
    check_equal("all enabled", dpm.roll_up_status({"Enabled"}), "Enabled")
    check_equal("mixed is partial", dpm.roll_up_status({"Enabled", "Not enabled"}), "Partial")
    check_equal("runtime only", dpm.roll_up_status({"Runtime PM only", "Not enabled"}), "Runtime PM only")
    check_equal("none enabled", dpm.roll_up_status({"Not enabled"}), "Not enabled")
    check_equal("nothing applicable", dpm.roll_up_status({"Not applicable"}), "Not applicable")


def test_driver_analysis() -> None:
    print("\ndriver analysis")
    cases = {
        "drivers/adc/adc_nxp_full.c": FULL_PM,
        "drivers/i2c/i2c_nxp_noop.c": NOOP_BRANCHES,
        "drivers/spi/spi_nxp_fallthrough.c": FALLTHROUGH,
        "drivers/pwm/pwm_nxp_ifform.c": IF_FORM,
        "drivers/gpio/gpio_nxp_runtime.c": RUNTIME_ONLY,
        "drivers/serial/uart_nxp_nopm.c": NO_PM,
        "drivers/counter/counter_nxp_legacy.c": LEGACY,
        "drivers/misc/misc_nxp_helper.c": NO_REGISTRATION,
        "drivers/dma/dma_nxp_commented.c": COMMENTED_OUT,
    }
    analysed = {path: dpm.analyse_driver(path, text) for path, text in cases.items()}

    full = analysed["drivers/adc/adc_nxp_full.c"]
    check_equal("full: status", full.status, "Enabled")
    check_equal("full: all four actions", full.actions, ["RESUME", "SUSPEND", "TURN_OFF", "TURN_ON"])
    check_equal("full: no no-ops", full.noop_actions, [])
    check_equal("full: subsystem from path", full.subsystem, "ADC")

    noop = analysed["drivers/i2c/i2c_nxp_noop.c"]
    check_equal("noop: only RESUME works", noop.actions, ["RESUME"])
    check_equal("noop: empty and error branches flagged", noop.noop_actions, ["SUSPEND", "TURN_OFF"])
    check_equal("noop: still Enabled (PM object present)", noop.status, "Enabled")
    check("noop: note names the no-op actions", "SUSPEND, TURN_OFF" in noop.note, noop.note)

    ft = analysed["drivers/spi/spi_nxp_fallthrough.c"]
    check_equal("fallthrough: empty case inherits the shared body",
                ft.actions, ["SUSPEND", "TURN_OFF"])
    check_equal("fallthrough: nothing flagged as no-op", ft.noop_actions, [])

    iff = analysed["drivers/pwm/pwm_nxp_ifform.c"]
    check_equal("if-form: RESUME detected", iff.actions, ["RESUME"])
    check_equal("if-form: empty SUSPEND flagged", iff.noop_actions, ["SUSPEND"])

    runtime = analysed["drivers/gpio/gpio_nxp_runtime.c"]
    check_equal("runtime: status", runtime.status, "Runtime PM only")
    check_equal("runtime: flag set", runtime.runtime_pm, True)

    nopm = analysed["drivers/serial/uart_nxp_nopm.c"]
    check_equal("no pm: status", nopm.status, "Not enabled")
    check_equal("no pm: no actions", nopm.actions, [])
    check("no pm: note explains", "No PM device object" in nopm.note, nopm.note)

    legacy = analysed["drivers/counter/counter_nxp_legacy.c"]
    check_equal("legacy: pm_device_driver_init counts as Enabled", legacy.status, "Enabled")
    check("legacy: note mentions the legacy pattern",
          "Legacy Device PM pattern" in legacy.note or "stub callback" in legacy.note, legacy.note)

    helper = analysed["drivers/misc/misc_nxp_helper.c"]
    check_equal("no registration: status", helper.status, "Not enabled")
    check("no registration: note explains the policy",
          "No DEVICE*DEFINE registration" in helper.note, helper.note)

    commented = analysed["drivers/dma/dma_nxp_commented.c"]
    check_equal("commented-out PM does not count", commented.actions, [])
    check_equal("commented-out PM stays Not enabled", commented.status, "Not enabled")


def test_scope_and_boards(repo: Path) -> None:
    print("\nscope and board mapping")
    zephyr = dpm.Zephyr(repo)
    patterns = zephyr.scope_patterns()
    check_equal("files-regex entries parsed", len(patterns), 2)

    catalogue = dpm.driver_catalogue(zephyr)
    sources = {driver.source for group in catalogue.values() for driver in group}
    for excluded in [
        "drivers/wifi/wifi_nxp_thing.c",
        "drivers/bluetooth/bt_nxp_thing.c",
        "drivers/can/can_nxp_s32_thing.c",
        "drivers/adc/adc_other_vendor.c",
    ]:
        check(f"excluded from scope: {excluded}", excluded not in sources, str(sorted(sources)))
    check("in-scope driver present", "drivers/adc/adc_nxp_full.c" in sources)

    boards = {board.name: board for board in dpm.collect_boards(zephyr, catalogue)}
    check_equal("boards discovered", sorted(boards), ["board_a", "board_b", "board_c"])
    check("boards/nxp/common is skipped", "common" not in boards, str(sorted(boards)))
    check_equal("variants collected", boards["board_a"].variants, ["board_a", "board_a_variant"])

    a = {ip.compatible: ip for ip in boards["board_a"].ips}
    check_equal("board_a IPs include the included dtsi",
                sorted(a), ["nxp,fallthrough", "nxp,full", "nxp,noop", "nxp,nopm"])
    check("wifi compatible in the dtsi maps to no in-scope driver", "nxp,wifi" not in a, str(sorted(a)))
    check_equal("board_a enabled count", boards["board_a"].enabled, 3)
    check_equal("board_a applicable", boards["board_a"].applicable, 4)
    check_equal("board_a coverage", round(boards["board_a"].coverage, 3), 0.75)

    b = {ip.compatible: ip for ip in boards["board_b"].ips}
    check("unmatched vendor compatible dropped", "vendor,not-a-driver" not in b, str(sorted(b)))
    check_equal("runtime-only IP status", b["nxp,runtime"].status, "Runtime PM only")
    check_equal("helper IP status", b["nxp,helper"].status, "Not enabled")

    c = {ip.compatible: ip for ip in boards["board_c"].ips}
    check_equal("two drivers, one enabled -> Partial", c["nxp,shared"].status, "Partial")
    check_equal("partial IP lists both drivers", len(c["nxp,shared"].drivers), 2)

    check("worst status sorts first",
          boards["board_a"].ips[0].status == "Not enabled", boards["board_a"].ips[0].status)


def test_outputs(repo: Path, output: Path) -> None:
    print("\ngenerated outputs")
    zephyr = dpm.Zephyr(repo)
    catalogue = dpm.driver_catalogue(zephyr)
    boards = dpm.collect_boards(zephyr, catalogue)
    summary = dpm.write_outputs(output, zephyr, boards, catalogue, "2026-01-01 00:00 UTC")

    for name in ["index.html", "boards.json", "summary.json", "boards.csv", "drivers.csv"]:
        check(f"{name} written", (output / name).is_file())
    check("no Excel workbook is produced",
          not list(output.glob("*.xlsx")), str(list(output.glob("*.xlsx"))))

    payload = json.loads((output / "boards.json").read_text(encoding="utf-8"))
    check_equal("boards.json board count", len(payload["boards"]), 3)
    check("boards.json carries the driver map", bool(payload["drivers"]))
    check_equal("driver map is deduplicated by source",
                len(payload["drivers"]), len({d.source for g in catalogue.values() for d in g}))

    # Every source referenced by a board must exist in the driver map, or the
    # dashboard renders "undefined" for its subsystem and note.
    referenced = {s for board in payload["boards"] for ip in board["ips"] for s in ip["sources"]}
    check("every referenced source resolves in the driver map",
          referenced <= set(payload["drivers"]), str(sorted(referenced - set(payload["drivers"]))))

    check_equal("summary counts mappings",
                summary["board_ip_mappings"], sum(len(b.ips) for b in boards))
    check_equal("summary action counts cover all four actions",
                sorted(summary["driver_action_counts"]), ["RESUME", "SUSPEND", "TURN_OFF", "TURN_ON"])
    check_equal("mapping status totals reconcile",
                sum(summary["mapping_status"].values()), summary["board_ip_mappings"])

    rows = list(csv_rows(output / "boards.csv"))
    check_equal("boards.csv row count", len(rows), summary["board_ip_mappings"])
    check_equal("boards.csv header",
                rows[0].keys() and list(rows[0]),
                ["board", "variants", "compatible", "status", "TURN_ON", "TURN_OFF",
                 "SUSPEND", "RESUME", "drivers", "subsystems"])
    full_row = next(r for r in rows if r["compatible"] == "nxp,full")
    check_equal("boards.csv marks implemented actions",
                [full_row[a] for a in ("TURN_ON", "TURN_OFF", "SUSPEND", "RESUME")],
                ["Yes", "Yes", "Yes", "Yes"])
    noop_row = next(r for r in rows if r["compatible"] == "nxp,noop")
    check_equal("boards.csv reports no-op branches as No",
                [noop_row[a] for a in ("TURN_OFF", "SUSPEND", "RESUME")], ["No", "No", "Yes"])

    driver_rows = list(csv_rows(output / "drivers.csv"))
    check_equal("drivers.csv row count", len(driver_rows), len(payload["drivers"]))
    noop_driver = next(r for r in driver_rows if r["source"].endswith("i2c_nxp_noop.c"))
    check_equal("drivers.csv records no-op actions",
                noop_driver["noop_actions"], "SUSPEND TURN_OFF")


def csv_rows(path: Path):
    import csv as _csv

    with path.open(encoding="utf-8", newline="") as handle:
        yield from _csv.DictReader(handle)


def test_missing_section_is_reported(root: Path) -> None:
    print("\nerror handling")
    broken = root / "broken"
    broken.mkdir()
    (broken / "MAINTAINERS.yml").write_text("Other Area:\n  status: maintained\n", encoding="utf-8")
    try:
        dpm.Zephyr(broken).scope_patterns()
    except SystemExit as error:
        check("renamed maintainer block is reported clearly",
              "NXP Platform Drivers" in str(error), str(error))
    else:
        check("renamed maintainer block is reported clearly", False, "no error raised")

    empty = root / "notzephyr"
    empty.mkdir()
    try:
        dpm.Zephyr(empty)
    except SystemExit as error:
        check("non-Zephyr directory is rejected", "MAINTAINERS.yml" in str(error), str(error))
    else:
        check("non-Zephyr directory is rejected", False, "no error raised")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="device-pm-test-"))
    try:
        print(f"building synthetic Zephyr tree in {root}")
        repo = build_tree(root)
        test_low_level_helpers()
        test_driver_analysis()
        test_scope_and_boards(repo)
        test_outputs(repo, root / "out")
        test_missing_section_is_reported(root)
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
