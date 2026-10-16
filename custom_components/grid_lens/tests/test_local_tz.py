#!/usr/bin/env python3
"""retailer_plans.local_tz(): tariff windows, effective dates and hour-of-day
profiles follow Home Assistant's configured time zone, not a hardcoded Sydney.

A QLD install (Australia/Brisbane, no DST) used to have every TOU window read an
hour late for half the year; WA two to three hours. Home Assistant isn't
importable here, so a stub `homeassistant.util.dt` stands in for it.

Run:  python3 test_local_tz.py
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import types
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.dirname(_HERE)
sys.path.insert(0, _COMPONENT)

import retailer_plans  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _stub_ha(**attrs):
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dtm = types.ModuleType("homeassistant.util.dt")
    for k, v in attrs.items():
        setattr(dtm, k, v)
    util.dt = dtm
    ha.util = util
    sys.modules.update({"homeassistant": ha, "homeassistant.util": util,
                        "homeassistant.util.dt": dtm})


def _unstub_ha():
    for k in ("homeassistant", "homeassistant.util", "homeassistant.util.dt"):
        sys.modules.pop(k, None)


def test_falls_back_to_sydney_without_ha():
    _unstub_ha()
    check("no HA -> Australia/Sydney", str(retailer_plans.local_tz()) == "Australia/Sydney")


def test_follows_ha_configured_zone():
    _stub_ha(get_default_time_zone=lambda: ZoneInfo("Australia/Brisbane"))
    try:
        tz = retailer_plans.local_tz()
        check("HA zone is used", str(tz) == "Australia/Brisbane", tz)
        # 15:30 AEST in summer is 16:30 in Sydney — the hour a QLD TOU window
        # used to be misread at.
        t = dt.datetime(2026, 1, 15, 5, 30, tzinfo=dt.timezone.utc)
        check("summer QLD hour is read in QLD time",
              t.astimezone(tz).hour == 15 and t.astimezone(ZoneInfo("Australia/Sydney")).hour == 16)
    finally:
        _unstub_ha()


def test_older_ha_default_time_zone_attribute():
    _stub_ha(DEFAULT_TIME_ZONE=ZoneInfo("Australia/Perth"))
    try:
        check("DEFAULT_TIME_ZONE used when get_default_time_zone is absent",
              str(retailer_plans.local_tz()) == "Australia/Perth")
    finally:
        _unstub_ha()


def test_no_hardcoded_zone_left():
    hits = []
    for root, _dirs, files in os.walk(_COMPONENT):
        if os.sep + "tests" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(root, f)).read()
                if re.search(r'ZoneInfo\("Australia/', src):
                    hits.append(f)
    check("no ZoneInfo(\"Australia/...\") outside local_tz()", hits == [], hits)


def main():
    mod = sys.modules[__name__]
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        print(f"{name}:")
        getattr(mod, name)()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} - " + ", ".join(FAILURES))
        sys.exit(1)
    print("OK - local_tz follows Home Assistant's zone.")


if __name__ == "__main__":
    main()
