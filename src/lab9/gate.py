"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from numbers import Real
from pathlib import Path
from typing import Any

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []
    if not isinstance(pr, dict):
        return ["PR report is not an object"]
    if not isinstance(base, dict):
        return ["baseline report is not an object"]
    if not isinstance(spec, dict):
        return ["metric spec is not an object"]
    if base_spec is not None and not isinstance(base_spec, dict):
        return ["base metric spec is not an object"]

    raw_metrics = spec.get("metrics")
    raw_slices = spec.get("slices", [])
    if not isinstance(raw_metrics, list):
        problems.append("metric spec metrics must be a list")
        raw_metrics = []
    if not isinstance(raw_slices, list):
        problems.append("metric spec slices must be a list")
        raw_slices = []
    if not isinstance(spec.get("eval_set_version"), str) or not spec["eval_set_version"]:
        problems.append("metric spec eval_set_version missing or invalid")
    metric_ids = [m["id"] for m in raw_metrics if isinstance(m, dict) and isinstance(m.get("id"), str)]
    if len(metric_ids) != len(set(metric_ids)):
        problems.append("metric spec has duplicate metric ids")
    slice_ids = [(s["name"], s["metric"]) for s in raw_slices if isinstance(s, dict)
                 and isinstance(s.get("name"), str) and isinstance(s.get("metric"), str)]
    if len(slice_ids) != len(set(slice_ids)):
        problems.append("metric spec has duplicate slices")
    for index, metric in enumerate(raw_metrics):
        if not isinstance(metric, dict) or not isinstance(metric.get("id"), str) or not metric["id"]:
            problems.append(f"metric spec entry {index} has invalid id")
        elif not isinstance(metric.get("threshold", {}), dict):
            problems.append(f"metric {metric['id']} threshold must be an object")
        elif not isinstance(metric.get("blocking", False), bool):
            problems.append(f"metric {metric['id']} blocking must be boolean")
    for index, section in enumerate(raw_slices):
        if (not isinstance(section, dict) or not isinstance(section.get("name"), str)
                or not isinstance(section.get("metric"), str)):
            problems.append(f"slice spec entry {index} has invalid name/metric")
        elif not isinstance(section.get("blocking", False), bool):
            problems.append(f"slice {section['name']} blocking must be boolean")
        elif section["metric"] not in metric_ids:
            problems.append(f"slice {section['name']} references unknown metric {section['metric']}")
    bootstrap = spec.get("bootstrap", {})
    if not isinstance(bootstrap, dict):
        problems.append("metric spec bootstrap must be an object")
    else:
        resamples = bootstrap.get("n_resamples", 1000)
        rng = bootstrap.get("rng", 2027)
        confidence = bootstrap.get("confidence", 0.95)
        if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1:
            problems.append("bootstrap n_resamples must be a positive integer")
        if not isinstance(rng, int) or isinstance(rng, bool):
            problems.append("bootstrap rng must be an integer")
        try:
            valid_confidence = (isinstance(confidence, Real) and not isinstance(confidence, bool)
                                and math.isfinite(float(confidence)) and 0 < confidence < 1)
        except (OverflowError, ValueError):
            valid_confidence = False
        if not valid_confidence:
            problems.append("bootstrap confidence must be between 0 and 1")

    def valid_thresholds(entry: dict, label: str, *, slice_entry: bool = False) -> None:
        thresholds = entry if slice_entry else entry.get("threshold", {})
        if not isinstance(thresholds, dict):
            problems.append(f"{label} threshold must be an object")
            return
        for key in ("min", "max", "max_increase"):
            value = thresholds.get(key)
            if value is not None:
                try:
                    valid_number = (isinstance(value, Real) and not isinstance(value, bool)
                                    and math.isfinite(float(value)))
                except (OverflowError, ValueError):
                    valid_number = False
                if not valid_number:
                    problems.append(f"{label} threshold {key} is not a finite number")
        if "no_significant_drop" in thresholds and not isinstance(thresholds["no_significant_drop"], bool):
            problems.append(f"{label} no_significant_drop must be boolean")

    for metric in raw_metrics:
        if isinstance(metric, dict):
            valid_thresholds(metric, f"metric {metric.get('id')}")
    for section in raw_slices:
        if isinstance(section, dict):
            valid_thresholds(section, f"slice {section.get('name')}", slice_entry=True)

    for label, report in (("PR", pr), ("baseline", base)):
        if not isinstance(report.get("run_id"), str) or not report["run_id"]:
            problems.append(f"{label} run_id missing or invalid")
        manifest = report.get("manifest")
        if not isinstance(manifest, dict):
            problems.append(f"{label} manifest missing or not an object")
        elif not isinstance(manifest.get("eval_set_sha"), str) or not manifest["eval_set_sha"]:
            problems.append(f"{label} manifest eval_set_sha missing or invalid")
        else:
            seeds = manifest.get("seeds")
            items = manifest.get("items")
            if (not isinstance(seeds, list) or not seeds
                    or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds)
                    or len(set(seeds)) != len(seeds)
                    or not isinstance(items, int) or isinstance(items, bool) or items < 1):
                problems.append(f"{label} manifest must declare positive items and unique integer seeds")
        report_metrics = report.get("metrics")
        if not isinstance(report_metrics, dict):
            problems.append(f"{label} metrics missing or not an object")
        elif label == "baseline":
            for metric in raw_metrics:
                if isinstance(metric, dict) and metric.get("id") not in report_metrics:
                    problems.append(f"baseline metric {metric.get('id')} missing from report")

    def finite(value: Any) -> bool:
        if isinstance(value, Real):
            try:
                return math.isfinite(float(value))
            except (OverflowError, ValueError):
                return False
        if isinstance(value, dict):
            return all(finite(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return all(finite(v) for v in value)
        return True

    metrics = pr.get("metrics")
    if not isinstance(metrics, dict):
        problems.append("metrics missing or not an object")
    else:
        for metric in raw_metrics:
            if isinstance(metric, dict) and metric.get("id") not in metrics:
                problems.append(f"metric {metric.get('id')} missing from report")
        for metric, value in metrics.items():
            if value is None:
                problems.append(f"metric {metric} is missing")
            elif not isinstance(value, Real) or isinstance(value, bool):
                problems.append(f"metric {metric} is not numeric")
            elif not finite(value):
                problems.append(f"metric {metric} is non-finite")
    base_metrics = base.get("metrics")
    if isinstance(base_metrics, dict):
        for metric, value in base_metrics.items():
            if value is None or not isinstance(value, Real) or isinstance(value, bool) or not finite(value):
                problems.append(f"baseline metric {metric} is missing or non-finite")

    rows = pr.get("rows")
    base_rows = base.get("rows")
    manifest = pr.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    seeds = manifest.get("seeds")
    item_count = manifest.get("items")
    if not isinstance(rows, list):
        problems.append("rows missing or not a list")
        rows = []
    if not isinstance(base_rows, list):
        problems.append("baseline rows missing or not a list")
        base_rows = []
    valid_seeds = (isinstance(seeds, list) and bool(seeds)
                   and all(isinstance(seed, int) and not isinstance(seed, bool) for seed in seeds)
                   and len(set(seeds)) == len(seeds))
    if not valid_seeds or not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 1:
        problems.append("manifest must declare a positive item count and unique integer seeds")
    else:
        expected_rows = item_count * len(seeds)
        if len(rows) != expected_rows:
            problems.append(f"row count {len(rows)} != manifest items × seeds ({expected_rows})")

    def row_keys(report_rows: list[Any], label: str) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        for index, row in enumerate(report_rows):
            if not isinstance(row, dict):
                problems.append(f"{label} row {index} is not an object")
                continue
            item_id, seed = row.get("item_id"), row.get("seed")
            if not isinstance(item_id, str) or not item_id or not isinstance(seed, int) or isinstance(seed, bool):
                problems.append(f"{label} row {index} has invalid item_id/seed")
                continue
            key = (item_id, seed)
            if key in keys:
                problems.append(f"{label} has duplicate row {item_id}@{seed}")
            keys.add(key)
        return keys

    pr_keys = row_keys(rows, "PR")
    base_keys = row_keys(base_rows, "baseline")
    if pr_keys != base_keys:
        missing = sorted(base_keys - pr_keys)
        extra = sorted(pr_keys - base_keys)
        problems.append(f"(item_id, seed) set differs from baseline: missing={missing[:3]}, extra={extra[:3]}")
    if valid_seeds and {seed for _, seed in pr_keys} != set(seeds):
        problems.append("PR row seeds do not match manifest seeds")
    if isinstance(item_count, int) and not isinstance(item_count, bool) and item_count > 0:
        if len({item_id for item_id, _ in pr_keys}) != item_count:
            problems.append("PR row item count does not match manifest items")
    base_manifest = base.get("manifest")
    if isinstance(base_manifest, dict):
        base_items, base_seeds = base_manifest.get("items"), base_manifest.get("seeds")
        valid_base_seeds = (isinstance(base_seeds, list) and bool(base_seeds)
                            and all(isinstance(seed, int) and not isinstance(seed, bool) for seed in base_seeds)
                            and len(set(base_seeds)) == len(base_seeds))
        if (isinstance(base_items, int) and not isinstance(base_items, bool) and base_items > 0
                and valid_base_seeds):
            if len(base_rows) != base_items * len(base_seeds):
                problems.append("baseline row count does not match manifest items × seeds")
            if len({item_id for item_id, _ in base_keys}) != base_items:
                problems.append("baseline row item count does not match manifest items")
            if {seed for _, seed in base_keys} != set(base_seeds):
                problems.append("baseline row seeds do not match manifest seeds")

    failures = pr.get("evaluator_failures")
    if not isinstance(failures, list):
        problems.append("evaluator_failures missing or not a list")
    elif failures:
        problems.append(f"evaluator_failures is non-empty ({len(failures)} failure(s))")

    baseline_row_metrics = {key for row in base_rows if isinstance(row, dict) for key in row}
    aggregate_metrics = {"pass_5", "cost_per_success"}
    aggregate_metrics.update(m["id"] for m in raw_metrics if isinstance(m, dict)
                             and str(m.get("id", "")).startswith(("pass_at_", "pass_hat_")))
    graded_metrics = {m["id"] for m in raw_metrics if isinstance(m, dict)
                      and m.get("id") not in aggregate_metrics}
    for metric in sorted(graded_metrics - baseline_row_metrics):
        problems.append(f"baseline rows missing graded metric {metric}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for metric in graded_metrics:
            if metric not in row or row[metric] is None:
                problems.append(f"row {row.get('item_id')}@{row.get('seed')} has missing graded metric {metric}")
            elif not isinstance(row[metric], bool):
                problems.append(f"row {row.get('item_id')}@{row.get('seed')} has invalid graded metric {metric}")

    def slice_tags(report_rows: list[Any], label: str) -> dict[str, tuple[str, ...]]:
        tags_by_item: dict[str, tuple[str, ...]] = {}
        for row in report_rows:
            if not isinstance(row, dict) or not isinstance(row.get("item_id"), str):
                continue
            raw_tags = row.get("slices", [])
            if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
                problems.append(f"{label} row {row.get('item_id')} has invalid slice tags")
                continue
            tags = tuple(sorted(set(raw_tags)))
            previous = tags_by_item.setdefault(row["item_id"], tags)
            if previous != tags:
                problems.append(f"{label} item {row['item_id']} has inconsistent slice tags across seeds")
        return tags_by_item

    pr_tags = slice_tags(rows, "PR")
    base_tags = slice_tags(base_rows, "baseline")
    for item_id in sorted(pr_tags.keys() & base_tags.keys()):
        if pr_tags[item_id] != base_tags[item_id]:
            problems.append(f"item {item_id} slice tags differ from baseline")

    same_report_eval = (isinstance(pr.get("manifest"), dict) and isinstance(base.get("manifest"), dict)
                        and pr["manifest"].get("eval_set_sha") == base["manifest"].get("eval_set_sha"))
    if (base_spec is not None and same_report_eval
            and spec.get("eval_set_version") != base_spec.get("eval_set_version")):
        problems.append("ratchet cannot be skipped by changing eval_set_version without changing eval_set")

    ratchet_applicable = base_spec is not None and spec.get("eval_set_version") == base_spec.get("eval_set_version")
    if ratchet_applicable:
        if not isinstance(base_spec.get("metrics"), list):
            problems.append("base metric spec metrics must be a list for ratchet")
        if not isinstance(base_spec.get("slices", []), list):
            problems.append("base metric spec slices must be a list for ratchet")
        for previous in base_spec.get("metrics", []) if isinstance(base_spec.get("metrics"), list) else []:
            if (not isinstance(previous, dict) or not isinstance(previous.get("id"), str)
                    or not isinstance(previous.get("blocking", False), bool)):
                problems.append("base metric spec has an invalid metric entry")
            else:
                valid_thresholds(previous, f"base metric {previous.get('id')}")
        for previous in base_spec.get("slices", []) if isinstance(base_spec.get("slices", []), list) else []:
            if (not isinstance(previous, dict) or not isinstance(previous.get("name"), str)
                    or not isinstance(previous.get("metric"), str)
                    or not isinstance(previous.get("blocking", False), bool)):
                problems.append("base metric spec has an invalid slice entry")
            else:
                valid_thresholds(previous, f"base slice {previous.get('name')}", slice_entry=True)

    if (ratchet_applicable and isinstance(base_spec.get("metrics"), list)
            and isinstance(base_spec.get("slices", []), list)):
        def check_ratchet(current: dict, previous: dict, label: str, *, slice_entry: bool = False) -> None:
            current_threshold = current if slice_entry else current.get("threshold") or {}
            previous_threshold = previous if slice_entry else previous.get("threshold") or {}
            for key in ("min", "max", "max_increase"):
                old = previous_threshold.get(key)
                new = current_threshold.get(key)
                if old is None:
                    continue
                try:
                    valid_old = isinstance(old, Real) and not isinstance(old, bool) and math.isfinite(float(old))
                    valid_new = new is not None and isinstance(new, Real) and not isinstance(new, bool) and math.isfinite(float(new))
                except (OverflowError, ValueError):
                    valid_old, valid_new = False, False
                if not valid_old:
                    problems.append(f"base {label} threshold {key} is invalid")
                elif not valid_new:
                    problems.append(f"ratchet removed or invalid {label} threshold {key}")
                elif (key == "min" and new < old) or (key != "min" and new > old):
                    problems.append(f"ratchet loosened {label} threshold {key}: {old} -> {new}")
            if previous_threshold.get("no_significant_drop") is True and current_threshold.get("no_significant_drop") is not True:
                problems.append(f"ratchet dropped no_significant_drop for {label}")
            if previous.get("blocking", False) and not current.get("blocking", False):
                problems.append(f"ratchet made blocking {label} non-blocking")

        current_metrics = {m["id"]: m for m in raw_metrics if isinstance(m, dict) and isinstance(m.get("id"), str)}
        previous_metrics = {m["id"]: m for m in base_spec["metrics"] if isinstance(m, dict) and isinstance(m.get("id"), str)}
        for metric_id, previous in previous_metrics.items():
            if metric_id not in current_metrics:
                problems.append(f"ratchet removed metric {metric_id}")
            else:
                check_ratchet(current_metrics[metric_id], previous, f"metric {metric_id}")

        def slice_map(value: dict) -> dict[tuple[str, str], dict]:
            entries = value.get("slices", [])
            return {(s["name"], s["metric"]): s for s in entries
                    if isinstance(s, dict) and isinstance(s.get("name"), str)
                    and isinstance(s.get("metric"), str)}

        current_slices, previous_slices = slice_map(spec), slice_map(base_spec)
        for key, previous in previous_slices.items():
            if key not in current_slices:
                problems.append(f"ratchet removed slice {key[0]}/{key[1]}")
            else:
                check_ratchet(current_slices[key], previous, f"slice {key[0]}/{key[1]}", slice_entry=True)

    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked: list[str] = []
    lines: list[str] = []
    bs = spec.get("bootstrap", {})
    for section in spec.get("slices", []):
        name, metric = section["name"], section["metric"]
        slice_id = f"slice:{name}/{metric}"
        pr_values = per_item(pr, metric, slice_name=name)
        base_values = per_item(base, metric, slice_name=name)
        keys = sorted(set(pr_values) & set(base_values))
        blocking = section.get("blocking", False)
        verdict, note = "ok", ""
        value = sum(pr_values.values()) / len(pr_values) if pr_values else None
        if not keys:
            verdict = "missing"
            note = "no paired items in slice"
        else:
            if "no_significant_drop" in section:
                delta, low, high = stats.paired_bootstrap(
                    [pr_values[k] for k in keys], [base_values[k] for k in keys],
                    n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027),
                    confidence=bs.get("confidence", 0.95),
                )
                note = f"Δ {delta:+.3f} CI [{low:+.3f}, {high:+.3f}]"
                if section["no_significant_drop"] and high < 0:
                    verdict = "fail"
            floor = section.get("min")
            if floor is not None and value is not None and value < floor:
                verdict = "fail"
        if verdict != "ok" and blocking:
            blocked.append(slice_id)
        label = "ok" if verdict == "ok" else ("MISSING" if verdict == "missing" else "BLOCK" if blocking else "warn")
        lines.append(f"  {slice_id:<36} {value!s:<8} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and mid in base["metrics"]:
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    pr_id = pr.get("run_id", "<invalid>") if isinstance(pr, dict) else "<invalid>"
    base_id = base.get("run_id", "<invalid>") if isinstance(base, dict) else "<invalid>"
    manifest = pr.get("manifest") if isinstance(pr, dict) else None
    eval_sha = manifest.get("eval_set_sha", "<invalid>") if isinstance(manifest, dict) else "<invalid>"
    print(f"gate: PR {pr_id} vs main {base_id} · eval set {eval_sha}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
