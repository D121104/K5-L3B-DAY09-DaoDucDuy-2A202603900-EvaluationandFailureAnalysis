"""M5-M7 self-check (Advanced/Expert). Run: python -m pytest -q -m student -o addopts= -k advanced
Public vectors only; the instructor grades with hidden ones."""
import copy
import math

import pytest

pytestmark = pytest.mark.student


@pytest.fixture(scope="module")
def stats():
    from lab9 import stats

    return stats


@pytest.mark.parametrize("n,c,k,want", [(5, 5, 5, 1.0), (5, 0, 1, 0.0), (5, 2, 1, 0.4), (10, 3, 3, 1 - 35 / 120)])
def test_pass_at_k(stats, n, c, k, want):
    assert math.isclose(stats.pass_at_k(n, c, k), want, abs_tol=1e-9)


@pytest.mark.parametrize("n,c,k,want", [(5, 5, 5, 1.0), (5, 4, 5, 0.0), (5, 4, 3, 0.4), (10, 8, 2, 28 / 45)])
def test_pass_hat_k(stats, n, c, k, want):
    assert math.isclose(stats.pass_hat_k(n, c, k), want, abs_tol=1e-9)


def test_pass_k_rejects_bad_args(stats):
    for args in [(5, 6, 1), (5, 2, 0), (3, 1, 4), (0, 0, 1), (5, -1, 1), (5, 2.0, 1)]:
        with pytest.raises(ValueError):
            stats.pass_at_k(*args)
        with pytest.raises(ValueError):
            stats.pass_hat_k(*args)


def _report(rows, metrics, items=4, seeds=(1, 2)):
    return {"run_id": "test", "manifest": {"eval_set_sha": "x", "items": items, "seeds": list(seeds)},
            "rows": rows, "metrics": metrics, "evaluator_failures": []}


ROWS = [{"item_id": f"E-{i:03d}", "seed": s, "pass_rate": True, "slices": ["denial"] if i < 2 else []}
        for i in range(4) for s in (1, 2)]
SPEC = {"eval_set_version": "v2", "metrics": [{"id": "pass_rate", "kind": "capability", "blocking": True,
                                               "threshold": {"min": 0.8}}]}


def test_integrity_flags_nan_and_dropped_rows():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    assert gate.integrity_check(base, base, SPEC) == []
    nan = _report(copy.deepcopy(ROWS), {"pass_rate": float("nan")})
    assert gate.integrity_check(nan, base, SPEC)
    dropped = _report(copy.deepcopy(ROWS[:-1]), {"pass_rate": 1.0})
    assert gate.integrity_check(dropped, base, SPEC)

    bad_manifest = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    bad_manifest["manifest"] = "not-an-object"
    assert any("manifest" in p for p in gate.integrity_check(bad_manifest, base, SPEC))

    duplicate_seed = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    duplicate_seed["manifest"]["seeds"] = [1, 1]
    assert any("unique integer seeds" in p for p in gate.integrity_check(duplicate_seed, base, SPEC))


def test_integrity_flags_loosened_spec():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    loose = copy.deepcopy(SPEC)
    loose["metrics"][0]["threshold"]["min"] = 0.5
    assert gate.integrity_check(base, base, loose, SPEC)
    assert gate.integrity_check(base, base, SPEC, SPEC) == []


def test_integrity_flags_missing_grader_value_and_duplicate_row():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    missing = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    del missing["rows"][0]["pass_rate"]
    assert any("missing graded metric pass_rate" in p for p in gate.integrity_check(missing, base, SPEC))

    duplicate = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    duplicate["rows"][-1] = copy.deepcopy(duplicate["rows"][0])
    assert any("duplicate row" in p for p in gate.integrity_check(duplicate, base, SPEC))

    missing_aggregate = _report(copy.deepcopy(ROWS), {})
    assert any("metric pass_rate missing from report" in p
               for p in gate.integrity_check(missing_aggregate, base, SPEC))


def test_integrity_flags_slice_relabel_and_evaluator_failure():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    relabeled = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    relabeled["rows"][0]["slices"] = []
    assert any("slice tags differ" in p for p in gate.integrity_check(relabeled, base, SPEC))

    failed = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    failed["evaluator_failures"] = ["E-001@1:pass_rate:RuntimeError"]
    assert any("evaluator_failures" in p for p in gate.integrity_check(failed, base, SPEC))


def test_integrity_ratchets_slice_and_max_thresholds():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    old = copy.deepcopy(SPEC)
    old["metrics"][0]["threshold"] = {"max": 0.1}
    old["slices"] = [{"name": "denial", "metric": "pass_rate", "no_significant_drop": True,
                      "blocking": True}]
    current = copy.deepcopy(old)
    current["metrics"][0]["threshold"]["max"] = 0.2
    current["slices"][0].pop("no_significant_drop")
    problems = gate.integrity_check(base, base, current, old)
    assert any("threshold max" in p for p in problems)
    assert any("no_significant_drop" in p for p in problems)


def test_slice_check_blocks_a_slice_below_floor():
    from lab9 import gate

    base = _report(copy.deepcopy(ROWS), {"pass_rate": 1.0})
    rows = copy.deepcopy(ROWS)
    for r in rows:
        if "denial" in r["slices"] and r["seed"] == 1:
            r["pass_rate"] = False
    spec = dict(SPEC, slices=[{"name": "denial", "metric": "pass_rate", "min": 0.9, "blocking": True}])
    blocked, _ = gate.slice_check(_report(rows, {"pass_rate": 0.75}), base, spec)
    assert blocked == ["slice:denial/pass_rate"]


def test_slice_check_does_not_block_ci_that_touches_zero():
    from lab9 import gate

    base_rows = [{"item_id": "E-001", "seed": s, "pass_rate": True, "slices": ["money_movement"]}
                 for s in (1, 2)]
    base = _report(base_rows, {"pass_rate": 1.0}, items=1, seeds=(1, 2))
    spec = dict(SPEC, bootstrap={"n_resamples": 1000, "rng": 2027},
                slices=[{"name": "money_movement", "metric": "pass_rate",
                         "no_significant_drop": True, "blocking": True}])
    blocked, lines = gate.slice_check(copy.deepcopy(base), base, spec)
    assert blocked == []
    assert "CI [+0.000, +0.000]" in lines[0]
