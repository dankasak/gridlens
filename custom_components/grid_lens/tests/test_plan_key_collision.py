#!/usr/bin/env python3
"""Two plans sharing a display name must both survive the comparison.

`plan_costs` and `plan_details` are dicts keyed on "{retailer} - {plan_name}".
On 2026-08-28, bulk PRD authoring created sixteen pairs of plans sharing one
display name — retailers submit the same marketing name once per tariff type
(AGL's Single Rate and Time of Use "Residential Netflix Plan", Origin Basic,
Real Deal, Everyday Easy, ...) — so the second of each pair silently
overwrote the first and 16 plans vanished from the comparison with no error.

The plan data was disambiguated, so `_duplicate_plan_keys()` should be empty in
practice. These tests cover the structural guard behind it, because the data can
regress the moment another plan is authored.

Home Assistant is not importable here, so PlanCalculator is exercised through
the two pure helpers directly, bound to a stand-in with a `_get_plans()`.

Run:  python3 tests/test_plan_key_collision.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_helpers():
    """Pull the two helpers out of plan_calculator.py without importing it.

    The module imports homeassistant at top level, which does not exist in this
    environment; compiling just the two methods keeps the test honest (it runs
    the real source) without stubbing half of HA.
    """
    import ast, textwrap
    src = (ROOT / "plan_calculator.py").read_text()
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "PlanCalculator")
    wanted = {"_duplicate_plan_keys", "_plan_key"}
    funcs = [n for n in cls.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {f.name for f in funcs} == wanted, "helpers missing from PlanCalculator"
    mod = ast.Module(body=funcs, type_ignores=[])
    ns: dict = {}
    exec(compile(ast.fix_missing_locations(mod), "<helpers>", "exec"), ns)
    return ns["_duplicate_plan_keys"], ns["_plan_key"]


_dup_fn, _key_fn = _load_helpers()
_fails: list[str] = []


class FakePlan:
    def __init__(self, plan_id, retailer, plan_name):
        self.plan_id, self.retailer, self.plan_name = plan_id, retailer, plan_name


class FakeCalc:
    def __init__(self, plans):
        self._plans = plans

    def _get_plans(self):
        return self._plans

    _duplicate_plan_keys = _dup_fn
    _plan_key = _key_fn


def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond:
        _fails.append(label)


COLLIDING = [
    FakePlan("agl_netflix_single", "AGL", "Residential Netflix Plan"),
    FakePlan("agl_netflix_tou", "AGL", "Residential Netflix Plan"),
    FakePlan("globird_zerohero", "GloBird", "ZEROHERO"),
]


def test_colliding_plans_get_distinct_keys():
    print("test_colliding_plans_get_distinct_keys:")
    calc = FakeCalc(COLLIDING)
    dups = calc._duplicate_plan_keys()
    keys = [calc._plan_key(p, dups) for p in COLLIDING]
    check("the shared name is reported as duplicate",
          dups == {"AGL - Residential Netflix Plan"})
    check("every plan gets its own key", len(set(keys)) == len(COLLIDING))
    check("nothing is dropped from a keyed dict",
          len({k: 1 for k in keys}) == 3)


def test_uncolliding_plans_keep_a_clean_key():
    """The suffix must never appear on a plan that does not need it — these keys
    are user-visible sensor attribute names."""
    print("test_uncolliding_plans_keep_a_clean_key:")
    calc = FakeCalc(COLLIDING)
    dups = calc._duplicate_plan_keys()
    zero = [p for p in COLLIDING if p.plan_id == "globird_zerohero"][0]
    check("current plan key is untouched",
          calc._plan_key(zero, dups) == "GloBird - ZEROHERO")

    clean = [FakePlan("a", "AGL", "Solar Sharer"), FakePlan("b", "GloBird", "ZEROHERO")]
    c2 = FakeCalc(clean)
    check("no duplicates in a clean catalogue", c2._duplicate_plan_keys() == set())
    check("clean keys carry no suffix",
          [c2._plan_key(p, set()) for p in clean]
          == ["AGL - Solar Sharer", "GloBird - ZEROHERO"])


def test_key_is_order_independent():
    """Both call sites (the pricing loop and _detect_current_plan) derive the key
    separately; a key that depended on iteration order would make the user's own
    plan stop matching itself."""
    print("test_key_is_order_independent:")
    fwd = FakeCalc(COLLIDING)
    rev = FakeCalc(list(reversed(COLLIDING)))
    kf = {p.plan_id: fwd._plan_key(p, fwd._duplicate_plan_keys()) for p in COLLIDING}
    kr = {p.plan_id: rev._plan_key(p, rev._duplicate_plan_keys()) for p in COLLIDING}
    check("same key regardless of catalogue order", kf == kr)
    check("suffix identifies the variant",
          kf["agl_netflix_tou"].endswith("[agl_netflix_tou]"))


if __name__ == "__main__":
    test_colliding_plans_get_distinct_keys()
    test_uncolliding_plans_keep_a_clean_key()
    test_key_is_order_independent()
    if _fails:
        sys.exit(f"\nFAILED: {len(_fails)} check(s): {_fails}")
    print("\nOK — colliding display names cannot drop a plan.")
