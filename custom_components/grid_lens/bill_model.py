"""Reconstruct a half-hourly energy model from the summary figures on a paper bill.

Why this module exists
----------------------
Grid Lens's normal comparison reads interval data out of the HA recorder. That is
the accurate way and it is also why the product can say nothing at all about a
household that is not already running Grid Lens — including "my relative sent me
their quarterly bill, could they do better?".

A bill is a *sum*, not a *shape*, and most of what makes energy advice
interesting lives in the shape. This module takes the totals a retailer prints
(kWh per register, days, rates) plus a few answers only the household can give,
and reconstructs a plausible half-hourly model that reproduces those totals. The
result is fed to `PlanCalculator` through the `UsageSource` seam
(`bill_source.py`), so ranking, bill breakdown, charts and the What-If panel are
the same code that runs on real recorder data.

Deliberate constraints
----------------------
**No Home Assistant imports, no I/O, stdlib only.** Everything here is pure
arithmetic on dataclasses. That is what lets it be exercised from a plain
interpreter (`tools/bill_sandbox.py` in gridlens-api is the CLI harness, and
`tests/test_bill_model.py` runs with no HA at all), and it is the precondition
for lifting this into a shared `gridlens_core` package later —
see BILL_UPLOAD_COMPARISON_DESIGN.md §7.

**No fields for personal identifiers.** `BillFacts` has nowhere to put a name,
a supply address, an NMI, an account number or a payment reference, and
`BillFacts.from_dict` *rejects* unknown keys rather than ignoring them. This is
enforced in the schema on purpose: an over-eager PDF extractor then has nowhere
to put those values, instead of us filtering them out after the fact and hoping
we thought of every field. See the design doc §8.

Honesty about fidelity — read this before quoting any number
------------------------------------------------------------
PV system size is **not identifiable** from a bill. Fitting (PV total, daytime
load share) to the billed import and export registers converges to +0.00% on
both while *every* system from ~3.4 kW to ~13 kW fits equally well, each with a
compensating daytime share (measured on a real Origin/Endeavour bill,
2026-10-25). A 0.00% residual there means two free parameters absorbed the shape
error, not that we found the household — and downstream a 10 kWh battery
"saves" anywhere from $314 to $833/yr across that range.

So: pass the inverter nameplate in `IntakeAnswers.pv_kw` and the ambiguity
collapses (with PV fixed, the daytime share IS identified by the export
register). Leave it None and `calibrate` returns the midpoint of the admissible
valley with `identified=False` and a warning, and callers MUST present it as a
range — `admissible_pv_range` computes that range.

A household with no solar at all is the clean case: import register == their
consumption, no inverse problem, only the intraday shape unknown.
"""

from __future__ import annotations

import datetime as dt_mod
import math
from dataclasses import dataclass, field

SLOT_MIN = 30
SLOTS = 24 * 60 // SLOT_MIN

# Daylight window used to split a load archetype into its "someone is home in
# the day" and "nobody is" halves. Not astronomical daylight — it is the window
# over which the daytime-share parameter is defined, and it has to stay fixed
# for that parameter to mean the same thing across archetypes.
DAY_WINDOW = (7.0, 17.0)


def _slot_hour(slot: int) -> float:
    """Mid-point hour of a slot, so a profile is sampled at slot centres."""
    return (slot + 0.5) * SLOT_MIN / 60.0


def _in_day_window(slot: int) -> bool:
    lo, hi = DAY_WINDOW
    return lo <= _slot_hour(slot) < hi


# ---------------------------------------------------------------------------
# Bill facts — the parsed bill, and nothing else
# ---------------------------------------------------------------------------

@dataclass
class BillPeriod:
    """One rate period on the bill.

    Retailers split a bill whenever rates change mid-cycle (a 1 July price
    change, say), and each slice carries its own rates and its own day count.
    Keeping them separate is what lets `self_check()` reproduce the printed
    total to the cent.

    `printed_*` fields are what the bill actually shows for that line, used
    only by `self_check()`. They are optional: a parse that didn't capture a
    line's printed amount is checkable at the period or bill level instead.
    """

    start: str                       # ISO date, first day of this rate period
    days: int

    general_kwh: float = 0.0         # the general/anytime import register
    general_rate: float = 0.0        # $/kWh
    cl_kwh: float = 0.0              # controlled-load register, if any
    cl_rate: float = 0.0
    cl_register: str = "controlled_load_1"
    export_kwh: float = 0.0
    fit_rate: float = 0.0            # $/kWh feed-in
    supply_per_day: float = 0.0      # $/day
    cl_supply_per_day: float = 0.0   # $/day, the CL register's own supply charge

    # Only present on a bill from a household already on a demand tariff. A
    # non-demand bill never prints a peak kW, and it cannot be recovered from
    # kWh totals — the whole point of a demand charge is that it depends on a
    # maximum, which summing destroys. See bill_source.BillGates.
    demand_peak_kw: float | None = None
    demand_rate: float | None = None     # $/kW/day

    printed_general_amount: float | None = None
    printed_cl_amount: float | None = None
    printed_supply_amount: float | None = None
    printed_cl_supply_amount: float | None = None
    printed_export_amount: float | None = None
    printed_total: float | None = None

    @property
    def start_date(self) -> dt_mod.date:
        return dt_mod.date.fromisoformat(self.start)

    def reconstructed(self) -> dict:
        """Each line's amount, recomputed from kWh x rate."""
        return {
            "general": self.general_kwh * self.general_rate,
            "cl": self.cl_kwh * self.cl_rate,
            "supply": self.days * self.supply_per_day,
            "cl_supply": self.days * self.cl_supply_per_day,
            "export": -(self.export_kwh * self.fit_rate),
            "demand": (self.days * (self.demand_rate or 0.0) * (self.demand_peak_kw or 0.0)),
        }

    def total(self) -> float:
        return sum(self.reconstructed().values())


@dataclass
class BillFacts:
    """Everything we extract from a bill — and deliberately nothing more.

    There is no field for name, supply address, NMI, account number, meter
    number or payment reference, and `from_dict` rejects keys it does not
    recognise. See this module's docstring.
    """

    state: str = "NSW"
    network: str = ""                # DNSP name as printed, e.g. "Endeavour Energy"
    postcode: str = ""
    latitude: float = -33.87         # for solar geometry; postcode-derived is fine
    retailer: str = ""
    plan_name: str = ""
    plan_id: str | None = None       # our slug for the plan they're on, if known
    periods: list = field(default_factory=list)
    printed_energy_total: float | None = None

    # Which register the controlled load sits on, when the bill shows one.
    cl_register: str = "controlled_load_1"

    _FLOAT = ("latitude", "printed_energy_total")
    _ALLOWED = (
        "state", "network", "postcode", "latitude", "retailer", "plan_name",
        "plan_id", "periods", "printed_energy_total", "cl_register",
    )

    # -- aggregates ---------------------------------------------------------

    @property
    def days(self) -> int:
        return sum(p.days for p in self.periods)

    @property
    def general_kwh(self) -> float:
        return sum(p.general_kwh for p in self.periods)

    @property
    def cl_kwh(self) -> float:
        return sum(p.cl_kwh for p in self.periods)

    @property
    def export_kwh(self) -> float:
        return sum(p.export_kwh for p in self.periods)

    @property
    def has_controlled_load(self) -> bool:
        return self.cl_kwh > 0.0

    @property
    def has_solar(self) -> bool:
        return self.export_kwh > 0.0

    @property
    def is_demand_bill(self) -> bool:
        return any(p.demand_peak_kw for p in self.periods)

    @property
    def demand_peak_kw(self) -> float | None:
        peaks = [p.demand_peak_kw for p in self.periods if p.demand_peak_kw]
        return max(peaks) if peaks else None

    @property
    def start_date(self) -> dt_mod.date:
        return min(p.start_date for p in self.periods)

    @property
    def end_date(self) -> dt_mod.date:
        """Exclusive — the day after the last billed day."""
        return max(p.start_date + dt_mod.timedelta(days=p.days) for p in self.periods)

    def billed_total(self) -> float:
        """What the retailer charged for energy over these periods.

        Energy lines only — no arrears, payment fees or dishonour reversals,
        which are account events rather than anything a tariff could change.
        """
        return sum(p.total() for p in self.periods)

    def cl_daily_kwh(self) -> float:
        return self.cl_kwh / self.days if self.days else 0.0

    def own_cl_rate(self) -> float | None:
        """The controlled-load rate this household is actually being charged.

        kWh-weighted across the bill's periods, so a mid-cycle price change is
        averaged by energy rather than by day count. Used as the suggested
        value for `IntakeAnswers.assume_cl_rate` — a rate the household can
        verify on their own bill, which is a better starting point than any
        number we could pick for them.
        """
        kwh = sum(p.cl_kwh for p in self.periods if p.cl_rate)
        if kwh <= 0:
            return None
        return sum(p.cl_kwh * p.cl_rate for p in self.periods if p.cl_rate) / kwh

    def own_cl_supply(self) -> float:
        """Day-weighted controlled-load daily supply charge from the bill."""
        if not self.days:
            return 0.0
        return sum(p.days * p.cl_supply_per_day for p in self.periods) / self.days

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self._ALLOWED if k != "periods"}
        out["periods"] = [
            {k: v for k, v in vars(p).items() if v is not None}
            for p in self.periods
        ]
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "BillFacts":
        """Build from JSON, rejecting any key the schema does not define.

        Strict on purpose: a tolerant parser is how a supply address ends up
        persisted in a Store "just in case it's useful later".
        """
        raw = dict(raw)
        unknown = set(raw) - set(cls._ALLOWED)
        if unknown:
            raise ValueError(
                "unrecognised bill field(s): %s — BillFacts has no field for "
                "personal identifiers and will not carry them"
                % ", ".join(sorted(unknown))
            )
        periods_raw = raw.pop("periods", None) or []
        if not periods_raw:
            raise ValueError("a bill needs at least one rate period")
        allowed_p = set(BillPeriod.__dataclass_fields__)
        periods = []
        for p in periods_raw:
            bad = set(p) - allowed_p
            if bad:
                raise ValueError(
                    "unrecognised bill period field(s): %s" % ", ".join(sorted(bad))
                )
            periods.append(BillPeriod(**p))
        return cls(periods=periods, **raw)

    # -- the free self-check -----------------------------------------------

    def self_check(self, tolerance: float = 0.10) -> "BillCheck":
        """Does the parse reproduce the amounts the bill prints?

        A bill carries its own proof: every line is kWh x rate, every period is
        the sum of its lines, and the bill is the sum of its periods. If the
        parse does not reproduce the printed figures to within a few cents, the
        parse is wrong — and this is a mechanical check, not an LLM confidence
        score, which is why it should gate acceptance rather than decorate it.

        `tolerance` is dollars per compared figure. Retailers round each line to
        the cent before summing, so a few cents of drift over a multi-line
        quarter is rounding, not error.
        """
        problems: list = []
        checked = 0
        for i, p in enumerate(self.periods, 1):
            recon = p.reconstructed()
            pairs = (
                ("general usage", recon["general"], p.printed_general_amount),
                ("controlled load", recon["cl"], p.printed_cl_amount),
                ("supply charge", recon["supply"], p.printed_supply_amount),
                ("CL supply charge", recon["cl_supply"], p.printed_cl_supply_amount),
                ("feed-in credit", recon["export"], None
                 if p.printed_export_amount is None else -abs(p.printed_export_amount)),
                ("period total", p.total(), p.printed_total),
            )
            for label, got, want in pairs:
                if want is None:
                    continue
                checked += 1
                if abs(got - want) > tolerance:
                    problems.append(
                        f"period {i} ({p.start}): {label} reconstructs to "
                        f"${got:,.2f} but the bill prints ${want:,.2f} "
                        f"(${got - want:+,.2f})"
                    )
        if self.printed_energy_total is not None:
            checked += 1
            got = self.billed_total()
            if abs(got - self.printed_energy_total) > max(tolerance, 0.02 * len(self.periods)):
                problems.append(
                    f"bill total reconstructs to ${got:,.2f} but the bill prints "
                    f"${self.printed_energy_total:,.2f} "
                    f"(${got - self.printed_energy_total:+,.2f})"
                )
        return BillCheck(ok=not problems, checked=checked, problems=problems)


@dataclass
class BillCheck:
    ok: bool
    checked: int                      # how many printed figures we could compare
    problems: list = field(default_factory=list)

    @property
    def verifiable(self) -> bool:
        """False when the parse captured no printed amounts to check against.

        `ok` is then vacuously True, which is exactly the state a caller must
        not mistake for "verified" — a parse with no printed totals has not
        been checked, it has merely not been contradicted.
        """
        return self.checked > 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "verifiable": self.verifiable,
                "checked": self.checked, "problems": list(self.problems)}


# ---------------------------------------------------------------------------
# Load archetypes — the shape the bill cannot see
# ---------------------------------------------------------------------------
#
# The bill constrains totals. It barely constrains shape, and for a no-solar
# household it does not constrain shape AT ALL — the import total is identical
# whatever the intraday distribution. Since battery value is almost entirely a
# shape question, this is where the honesty lives: ship a small library of named
# profiles with the question that selects each one, run them all, and report the
# spread as the error bar.
#
# Each archetype supplies a raw 48-slot silhouette. `archetype_shape` splits it
# at DAY_WINDOW and renormalises the two halves separately, so the daytime share
# becomes a free parameter *on top of* the archetype's own character rather than
# replacing it. Passing day_share=None reproduces the archetype's natural share.


def _profile_evening_peak() -> list:
    """Nobody home on weekdays: deep daytime trough, large evening peak.

    The conventional Australian residential double hump — the default when the
    household says the house is empty during the day.
    """
    out = []
    for slot in range(SLOTS):
        h = _slot_hour(slot)
        base = 0.35
        morning = 0.55 * math.exp(-((h - 7.0) ** 2) / 2.0)
        daytime = 0.12 * math.exp(-((h - 12.0) ** 2) / 18.0)
        evening = 1.00 * math.exp(-((h - 19.0) ** 2) / 4.5)
        out.append(base + morning + daytime + evening)
    return out


def _profile_daytime_occupied() -> list:
    """Someone home during the day: flatter, substantial 09-16 load."""
    out = []
    for slot in range(SLOTS):
        h = _slot_hour(slot)
        base = 0.35
        morning = 0.45 * math.exp(-((h - 7.5) ** 2) / 3.0)
        daytime = 0.70 * math.exp(-((h - 12.5) ** 2) / 20.0)
        evening = 0.75 * math.exp(-((h - 19.0) ** 2) / 5.0)
        out.append(base + morning + daytime + evening)
    return out


def _profile_shift_worker() -> list:
    """Irregular hours: overnight-weighted, no clean evening peak."""
    out = []
    for slot in range(SLOTS):
        h = _slot_hour(slot)
        base = 0.45
        # Wraps midnight: weight 22:00-06:00.
        night_h = h if h < 12 else h - 24.0
        overnight = 0.80 * math.exp(-((night_h - 1.5) ** 2) / 12.0)
        evening = 0.40 * math.exp(-((h - 18.0) ** 2) / 6.0)
        out.append(base + overnight + evening)
    return out


def _profile_flat() -> list:
    """A control, not a household. Its job is to bound the archetype spread:
    if the recommendation survives even a perfectly flat load, the shape
    assumption is not what the answer rests on."""
    return [1.0] * SLOTS


ARCHETYPES: dict = {
    "evening_peak": {
        "label": "Out during the day",
        "question": "Nobody home on weekdays",
        "profile": _profile_evening_peak,
    },
    "daytime_occupied": {
        "label": "Home during the day",
        "question": "Someone home most weekdays",
        "profile": _profile_daytime_occupied,
    },
    "shift_worker": {
        "label": "Irregular / overnight hours",
        "question": "Shift work, or mostly awake at night",
        "profile": _profile_shift_worker,
    },
    "flat": {
        "label": "Flat (control)",
        "question": "Not a real household — bounds the shape uncertainty",
        "profile": _profile_flat,
    },
}

DEFAULT_ARCHETYPE = "evening_peak"


def natural_day_share(archetype: str) -> float:
    """The archetype's own daytime share, before any calibration moves it."""
    prof = ARCHETYPES[archetype]["profile"]()
    total = sum(prof) or 1.0
    return sum(v for s, v in enumerate(prof) if _in_day_window(s)) / total


def archetype_shape(archetype: str, day_share: float | None = None) -> list:
    """Per-slot weights for general consumption, summing to 1.0.

    `day_share` is the fraction of the day's general consumption falling inside
    DAY_WINDOW. It is the one parameter the billed import/export pair can
    actually pin down (given a known PV size), because it is exactly what
    decides how much PV is self-consumed versus exported. None keeps the
    archetype's natural share.
    """
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown archetype {archetype!r}")
    prof = ARCHETYPES[archetype]["profile"]()
    if day_share is None:
        total = sum(prof) or 1.0
        return [v / total for v in prof]
    day_share = max(0.02, min(0.95, day_share))

    day_prof = [v if _in_day_window(s) else 0.0 for s, v in enumerate(prof)]
    night_prof = [0.0 if _in_day_window(s) else v for s, v in enumerate(prof)]
    d_sum = sum(day_prof) or 1.0
    n_sum = sum(night_prof) or 1.0
    return [day_share * d / d_sum + (1.0 - day_share) * n / n_sum
            for d, n in zip(day_prof, night_prof)]


def window_shape(window: tuple) -> list:
    """Flat per-slot weights across an (start_hour, end_hour) window, wrapping
    midnight if start > end. Weights sum to 1.0.

    Used for controlled load and for the CL-to-main scenarios. Controlled load
    sits on its own register at its own flat rate, so WHEN it runs does not
    change its cost; the window matters for one thing only — whether that load
    can soak up daytime solar. The default overnight window says it cannot,
    which is the physically honest default for a distributor-switched circuit.
    """
    start, end = window
    weights = []
    for slot in range(SLOTS):
        h = _slot_hour(slot)
        inside = (h >= start or h < end) if start > end else (start <= h < end)
        weights.append(1.0 if inside else 0.0)
    total = sum(weights) or 1.0
    return [w / total for w in weights]


# ---------------------------------------------------------------------------
# Solar geometry — shape only; magnitude is calibrated, never assumed
# ---------------------------------------------------------------------------

def poa_profile(latitude: float, day: dt_mod.date,
                tilt: float = 20.0, azimuth: float = 0.0) -> list:
    """Clear-sky plane-of-array irradiance per slot, in absolute-ish kW/m².

    Solar declination and hour angle give elevation and azimuth, a Kasten-style
    air-mass term attenuates the beam, and the beam is projected onto a tilted
    plane with a flat diffuse addition.

    **Azimuth convention: 0 = north, positive = WEST, negative = east**, which
    is the usual Australian way of stating a roof's orientation. Internally the
    solar azimuth this formula produces is east-positive, so the panel azimuth
    is negated on the way in — do not "simplify" that away.

    Unlike `clear_sky_shape` this is NOT normalised, so the day-to-day
    magnitude is meaningful: a June day genuinely integrates to less than a
    December one. That is what lets a period's generation be distributed across
    its real days instead of smeared as one average day
    (see `_solar_basis`).
    """
    n = day.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0)))
    lat = math.radians(latitude)
    beta = math.radians(tilt)
    a_p = math.radians(-azimuth)

    out = []
    for slot in range(SLOTS):
        H = math.radians(15.0 * (_slot_hour(slot) - 12.0))

        sin_elev = (math.sin(lat) * math.sin(decl)
                    + math.cos(lat) * math.cos(decl) * math.cos(H))
        if sin_elev <= 0.01:
            out.append(0.0)
            continue
        elev = math.asin(min(1.0, sin_elev))

        cos_elev = math.cos(elev)
        sin_az = -math.cos(decl) * math.sin(H) / cos_elev if cos_elev > 1e-6 else 0.0
        cos_az = ((math.sin(decl) - sin_elev * math.sin(lat))
                  / (cos_elev * math.cos(lat))) if cos_elev > 1e-6 else 1.0
        az = math.atan2(max(-1.0, min(1.0, sin_az)), max(-1.0, min(1.0, cos_az)))

        air_mass = min(20.0, 1.0 / max(sin_elev, 0.05))
        dni = 1.353 * (0.7 ** (air_mass ** 0.678))
        cos_theta = (sin_elev * math.cos(beta)
                     + cos_elev * math.sin(beta) * math.cos(az - a_p))
        poa = max(0.0, dni * cos_theta) + 0.15 * dni * sin_elev
        out.append(max(0.0, poa))
    return out


def clear_sky_shape(latitude: float, day: dt_mod.date,
                    tilt: float = 20.0, azimuth: float = 0.0,
                    cloud_flatten: float = 0.85) -> list:
    """One day's generation shape, peak-normalised to 1.0.

    Geometry (`poa_profile`) rather than a symmetric bell curve, for three
    reasons that matter to a solar+battery decision:
      - Day length and peak elevation swing hard with season, and a bell fitted
        to a winter quarter is wrong by hours of width in summer.
      - Azimuth is a buyer's lever ("should the new array face west?"), and a
        symmetric bell cannot represent a west array at all.
      - `cloud_flatten` < 1 pulls the curve toward flat, because a season's
        AVERAGE day is broader and blunter than a clear one, and a clear-sky
        peak overstates the midday surplus a battery has to work with. It is a
        shape-only knob — it changes how a total is spread across the day,
        never the total.

    Absolute magnitude is meaningless here by design; `calibrate` sets the
    scale from the bill. Use `poa_profile` when relative day-to-day energy is
    what matters.
    """
    raw = poa_profile(latitude, day, tilt=tilt, azimuth=azimuth)
    peak = max(raw) or 1.0
    shaped = [(v / peak) ** cloud_flatten for v in raw]
    peak2 = max(shaped) or 1.0
    return [v / peak2 for v in shaped]


def seasonal_yield(latitude: float, day: dt_mod.date) -> float:
    """Rough specific yield (kWh per kWp per day) for a date and latitude.

    A coarse sinusoid about an annual mean, enough to turn a calibrated daily
    generation figure into an inverter-nameplate sanity check and back. Not a
    replacement for PVWatts or Solcast, and only ever used for that conversion.
    """
    annual_mean = 4.2 - (abs(latitude) - 34.0) * 0.045
    n = day.timetuple().tm_yday
    # Peak late December (n~355) in the southern hemisphere.
    seasonal = math.cos(2 * math.pi * (n - 355) / 365.25)
    return max(1.2, annual_mean * (1.0 + 0.33 * seasonal))


# ---------------------------------------------------------------------------
# Intake answers — the parameters the bill cannot identify
# ---------------------------------------------------------------------------

CL_CONTROLLED = "controlled"
CL_MAIN_SOLAR = "main_solar"
CL_MAIN_UNCHANGED = "main_unchanged"


@dataclass
class IntakeAnswers:
    """What the household tells us, because the bill cannot.

    Three questions beat any amount of inference (design doc §5):
      1. Do you have solar, and what is the inverter's kW rating? (plus roof
         direction and rough tilt) — collapses the PV ambiguity entirely.
      2. Is anyone home during the day? — selects the archetype, which is the
         other free parameter and the only handle at all on a no-solar bill.
      3. Which of electric hot water / ducted aircon / pool / EV do you have? —
         identifies the deferrable devices and explains the CL register.
    """

    pv_kw: float | None = None           # inverter nameplate; None = unknown
    pv_azimuth: float = 0.0              # degrees from north, + toward west
    pv_tilt: float = 20.0
    archetype: str = DEFAULT_ARCHETYPE
    cloud_flatten: float = 0.85

    # Controlled load placement — the scenario axis (design doc §6).
    cl_placement: str = CL_CONTROLLED
    cl_window: tuple = (22.0, 7.0)       # the DNSP's overnight switching window
    cl_main_window: tuple = (10.0, 15.0)  # where it would run on the main circuit
    cl_max_kw: float | None = None       # element rating, caps absorption rate
    cl_rewire_cost: float = 0.0          # one-off; user-entered, never guessed

    # Devices the household declared (hot_water, aircon, pool, ev). Captured so
    # a later build can overlay them as declared deferrable loads; the base
    # archetype carries their energy today.
    devices: list = field(default_factory=list)

    # An ANALYSIS setting rather than a household fact, kept here because it
    # travels with the bill and is the user's own call.
    #
    # Only 24 of 588 NSW plans carry controlled-load rates, and 4 of 178 on
    # Endeavour, so a CL household's comparison is unanswerable for almost
    # every plan — the household's own plan included. Setting this carries one
    # assumed CL rate onto every plan that has none, which unblocks the
    # comparison at the cost of an assumption about each retailer's CL pricing.
    #
    # OFF by default, deliberately. Controlled-load rates are substantially
    # network pass-throughs and vary far less between retailers than general
    # usage rates do, which is why offering it is reasonable at all — but a
    # household with most of its usage on that register is exactly the
    # household for which being wrong matters most. Every figure that uses it
    # is tagged, and it is never presented as a retailer-published rate.
    assume_cl_rate: float | None = None       # $/kWh
    assume_cl_supply: float = 0.0             # $/day

    _ALLOWED = (
        "pv_kw", "pv_azimuth", "pv_tilt", "archetype", "cloud_flatten",
        "cl_placement", "cl_window", "cl_main_window", "cl_max_kw",
        "cl_rewire_cost", "devices", "assume_cl_rate", "assume_cl_supply",
    )

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self._ALLOWED}
        out["cl_window"] = list(self.cl_window)
        out["cl_main_window"] = list(self.cl_main_window)
        return out

    @classmethod
    def from_dict(cls, raw: dict | None) -> "IntakeAnswers":
        raw = dict(raw or {})
        unknown = set(raw) - set(cls._ALLOWED)
        if unknown:
            raise ValueError("unrecognised intake field(s): %s"
                             % ", ".join(sorted(unknown)))
        for key in ("cl_window", "cl_main_window"):
            if key in raw and raw[key] is not None:
                raw[key] = tuple(float(x) for x in raw[key])
        if raw.get("archetype") and raw["archetype"] not in ARCHETYPES:
            raise ValueError(f"unknown archetype {raw['archetype']!r}")
        if raw.get("cl_placement") not in (None, CL_CONTROLLED, CL_MAIN_SOLAR,
                                           CL_MAIN_UNCHANGED):
            raise ValueError(f"unknown cl_placement {raw['cl_placement']!r}")
        return cls(**raw)


@dataclass
class Scenario:
    """One priced position on the controlled-load axis.

    Battery size and extra solar are NOT scenario fields: the comparison page
    already carries those as `battery_kwh` / `solar_pct` what-if parameters, and
    they stay there so a bill-sourced run gets the same What-If panel a live run
    does.
    """

    id: str
    label: str
    cl_placement: str
    note: str = ""


def scenarios_for(facts: BillFacts) -> list:
    """The scenario axis for this bill — only CL positions that mean something.

    `main_unchanged` exists as a control: it is strictly worse than the status
    quo, and its job is to show how much of any benefit is the *re-timing*
    rather than the *re-wiring*.
    """
    base = [Scenario(id="status_quo", label="As billed today",
                     cl_placement=CL_CONTROLLED)]
    if not facts.has_controlled_load:
        return base
    base[0].label = "Hot water on controlled load (as billed)"
    base.append(Scenario(
        id="cl_to_main_solar",
        label="Hot water moved to the main circuit, run midday",
        cl_placement=CL_MAIN_SOLAR,
        note="Loses the cheap controlled-load rate and its separate supply "
             "charge; gains the ability to soak up solar. Needs an electrician "
             "and a meter reconfiguration.",
    ))
    base.append(Scenario(
        id="cl_to_main_overnight",
        label="Hot water moved to the main circuit, still overnight (control)",
        cl_placement=CL_MAIN_UNCHANGED,
        note="Strictly worse than today — included so the midday figure can be "
             "split into re-wiring versus re-timing.",
    ))
    return base


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class EnergyModel:
    """A half-hourly energy model consistent with the billed totals.

    `solar` / `general` are per-day lists of SLOTS kWh values, one entry per day
    of the bill period, each carrying its real date. Real dates matter: many NSW
    TOU plans price the same window differently either side of a seasonal
    boundary (agl_residential_smart_saver_tou_endeavour charges weekdays
    16:00-20:00 as Peak 42.424c from 04-01 to 10-31 and Shoulder 41.914c from
    11-01 to 03-31), and `PlanFromData._match_rate_def` resolves that from the
    datetime's date. Synthesising the whole period rather than a representative
    day is what makes a bill spanning a seasonal boundary price correctly.
    """

    dates: list                       # dt_mod.date, one per day, in order
    solar: list                       # list[list[float]] kWh per slot per day
    general: list                     # list[list[float]] kWh per slot per day
    cl_daily_kwh: float               # controlled-load register, per day
    cl_shape: list                    # SLOTS weights for where CL energy sits

    pv_total_kwh: float
    pv_kw_implied: float
    day_share: float
    archetype: str

    fit_import_kwh: float             # what the model reproduces...
    fit_export_kwh: float
    target_import_kwh: float          # ...versus what the bill says
    target_export_kwh: float

    identified: bool = True           # False when PV size was not pinned down
    pv_range_kw: tuple | None = None  # (lo, hi) when not identified
    warnings: list = field(default_factory=list)

    @property
    def import_err_pct(self) -> float:
        return pct_err(self.fit_import_kwh, self.target_import_kwh)

    @property
    def export_err_pct(self) -> float:
        return pct_err(self.fit_export_kwh, self.target_export_kwh)

    @property
    def general_daily_kwh(self) -> float:
        n = len(self.general) or 1
        return sum(sum(d) for d in self.general) / n

    def summary(self) -> dict:
        """Everything a confidence banner needs, JSON-ready."""
        self_cons = self.pv_total_kwh - self.fit_export_kwh
        return {
            "archetype": self.archetype,
            "archetype_label": ARCHETYPES[self.archetype]["label"],
            "day_share_pct": round(self.day_share * 100, 1),
            "pv_total_kwh": round(self.pv_total_kwh, 1),
            "pv_kw_implied": round(self.pv_kw_implied, 2),
            "pv_identified": self.identified,
            "pv_range_kw": (None if not self.pv_range_kw
                            else [round(self.pv_range_kw[0], 1),
                                  round(self.pv_range_kw[1], 1)]),
            "self_consumed_kwh": round(self_cons, 1),
            "self_consumed_pct": (round(self_cons / self.pv_total_kwh * 100, 1)
                                  if self.pv_total_kwh > 0 else None),
            "general_daily_kwh": round(self.general_daily_kwh, 2),
            "cl_daily_kwh": round(self.cl_daily_kwh, 2),
            "fit_import_kwh": round(self.fit_import_kwh, 1),
            "fit_export_kwh": round(self.fit_export_kwh, 1),
            "target_import_kwh": round(self.target_import_kwh, 1),
            "target_export_kwh": round(self.target_export_kwh, 1),
            "import_err_pct": round(self.import_err_pct, 2),
            "export_err_pct": round(self.export_err_pct, 2),
            "warnings": list(self.warnings),
        }


def pct_err(got: float, want: float) -> float:
    return 0.0 if want <= 0 else (got - want) / want * 100.0


def _day_list(facts: BillFacts) -> list:
    start, days = facts.start_date, facts.days
    return [start + dt_mod.timedelta(days=i) for i in range(days)]


@dataclass
class _SolarBasis:
    """Per-day, per-slot solar weights for the whole bill period, summing to 1.

    Scaling by a period PV total gives the kWh grid. Built once per (latitude,
    tilt, azimuth, cloud_flatten, date range) and reused across every PV size
    the calibration tries, because none of those inputs depend on PV size.

    `slot_sorted` / `slot_prefix` are the reason calibration is fast enough to
    run on every page load. The general load profile is identical on every day,
    so for a given load `g` in slot s:

        import_s = sum_d max(0, g - pv*A[d])  = k*g - pv*S[k]
        export_s = sum_d max(0, pv*A[d] - g)  = pv*(S[D]-S[k]) - (D-k)*g

    where A is that slot's weights sorted ascending, S its prefix sums, and
    k = bisect(A, g/pv). That turns an O(days x slots) sweep into O(slots)
    with a binary search, which matters because `admissible_pv_range` runs
    thousands of evaluations.
    """

    dates: list
    weights: list                 # list[list[float]] per day, per slot
    slot_sorted: list             # list[list[float]] per slot, sorted asc
    slot_prefix: list             # list[list[float]] per slot, len days+1

    @property
    def n_days(self) -> int:
        return len(self.dates)

    def grid(self, pv_total: float) -> list:
        return [[pv_total * w for w in day] for day in self.weights]


def _solar_basis(facts: BillFacts, answers: IntakeAnswers) -> _SolarBasis:
    """Distribute a unit of generation across the period's real days and slots.

    Weighted by each day's own clear-sky integral (`poa_profile`, deliberately
    un-normalised), so a Jun-Sep bill puts materially more generation in
    September than in June rather than smearing one average day across the
    quarter. Within a day, `cloud_flatten` blunts the shape.
    """
    from bisect import bisect_right  # noqa: F401  (documented in _SolarBasis)

    dates = _day_list(facts)
    weights = []
    for d in dates:
        raw = poa_profile(facts.latitude, d, tilt=answers.pv_tilt,
                          azimuth=answers.pv_azimuth)
        energy = sum(raw)
        peak = max(raw) or 1.0
        shaped = [(v / peak) ** answers.cloud_flatten for v in raw]
        s_sum = sum(shaped) or 1.0
        weights.append([energy * v / s_sum for v in shaped])

    grand = sum(sum(day) for day in weights) or 1.0
    weights = [[v / grand for v in day] for day in weights]

    slot_sorted, slot_prefix = [], []
    for slot in range(SLOTS):
        col = sorted(day[slot] for day in weights)
        pref = [0.0]
        for v in col:
            pref.append(pref[-1] + v)
        slot_sorted.append(col)
        slot_prefix.append(pref)
    return _SolarBasis(dates=dates, weights=weights, slot_sorted=slot_sorted,
                       slot_prefix=slot_prefix)


def _flows(basis: _SolarBasis, pv_total: float, load_shape: list,
           gen_daily: float) -> tuple:
    """Period import and export for a given load profile and PV total."""
    from bisect import bisect_right

    n = basis.n_days
    imp = exp = 0.0
    for slot in range(SLOTS):
        g = gen_daily * load_shape[slot]
        col = basis.slot_sorted[slot]
        pref = basis.slot_prefix[slot]
        if pv_total <= 0.0:
            imp += n * g
            continue
        k = bisect_right(col, g / pv_total)
        imp += k * g - pv_total * pref[k]
        exp += pv_total * (pref[n] - pref[k]) - (n - k) * g
    return max(0.0, imp), max(0.0, exp)


def _evaluate(basis: _SolarBasis, pv_total: float, load_shape: list,
              target_imp: float, target_exp: float) -> tuple:
    """Import, export and the daily general total implied by a load shape.

    General consumption is the billed import plus whatever PV the house
    self-consumed, and self-consumption is itself an output of the shapes — so
    iterate to a fixed point rather than assuming it.
    """
    n_days = basis.n_days or 1
    gen_daily = (target_imp + max(0.0, pv_total - target_exp)) / n_days
    imp = exp = 0.0
    for _ in range(80):
        imp, exp = _flows(basis, pv_total, load_shape, gen_daily)
        new_gen = (target_imp + (pv_total - exp)) / n_days
        if abs(new_gen - gen_daily) < 1e-7:
            gen_daily = new_gen
            break
        gen_daily = new_gen
    return imp, exp, gen_daily


def _period_slice(basis: _SolarBasis, first: int, count: int) -> _SolarBasis:
    """A basis covering only days [first, first+count) of the period.

    Its weights still sum to whatever share of the whole period those days
    carried, so scaling it by a PV total gives that slice's own generation —
    which is why `_fit_periods` renormalises before using it.
    """
    days = basis.weights[first:first + count]
    total = sum(sum(d) for d in days) or 1.0
    weights = [[v / total for v in d] for d in days]
    slot_sorted, slot_prefix = [], []
    for slot in range(SLOTS):
        col = sorted(d[slot] for d in weights)
        pref = [0.0]
        for v in col:
            pref.append(pref[-1] + v)
        slot_sorted.append(col)
        slot_prefix.append(pref)
    return _SolarBasis(dates=basis.dates[first:first + count], weights=weights,
                       slot_sorted=slot_sorted, slot_prefix=slot_prefix)


def _fit_period(basis: _SolarBasis, load_shape: list, target_imp: float,
                target_exp: float) -> tuple:
    """Solve (generation, consumption) so ONE period hits both its registers.

    The bill prints a register pair per rate period, and a period's own figures
    are what its own rates are applied to — so a model that only matches the
    whole-bill totals mis-prices a bill whose rates changed mid-cycle. Fitting
    each period separately also has a physical reading: consumption genuinely
    varies between a June slice and an August one, and so does generation
    (weather, not a different array), while the intraday *shape* is a property
    of the household and stays shared.

    Nested bisection, which the monotonicity supports: import rises with
    consumption at fixed generation, and export rises with generation once
    import is pinned. Returns (pv_total, gen_daily, import, export).
    """
    n = basis.n_days or 1

    def imp_exp(pv: float, gen_daily: float) -> tuple:
        return _flows(basis, pv, load_shape, gen_daily)

    def gen_for_import(pv: float) -> float:
        """Daily consumption that reproduces this period's import register."""
        lo, hi = 0.0, max(1.0, (target_imp + pv) / n * 4.0)
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if imp_exp(pv, mid)[0] < target_imp:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    if target_exp <= 0.0:
        # No export on this period: import IS consumption, exactly.
        return 0.0, target_imp / n, target_imp, 0.0

    lo_pv, hi_pv = target_exp, target_exp + 60.0 * n
    gen = target_imp / n
    for _ in range(60):
        pv = (lo_pv + hi_pv) / 2.0
        gen = gen_for_import(pv)
        _, exp = imp_exp(pv, gen)
        if exp < target_exp:
            lo_pv = pv
        else:
            hi_pv = pv
    pv = (lo_pv + hi_pv) / 2.0
    gen = gen_for_import(pv)
    imp, exp = imp_exp(pv, gen)
    return pv, gen, imp, exp


def _fit_periods(facts: BillFacts, basis: _SolarBasis, load_shape: list) -> tuple:
    """Per-period fit across the whole bill. Returns (solar_grid, general_grid).

    Each period gets its own generation and consumption level against its own
    printed registers; the load shape is shared. The grids come back indexed by
    day across the whole bill, ready to hand to `EnergyModel`.
    """
    solar_grid, general_grid = [], []
    first = 0
    for p in facts.periods:
        sub = _period_slice(basis, first, p.days)
        pv, gen, _, _ = _fit_period(sub, load_shape, p.general_kwh, p.export_kwh)
        for day in sub.weights:
            solar_grid.append([pv * w for w in day])
            general_grid.append([gen * w for w in load_shape])
        first += p.days
    return solar_grid, general_grid


def _best_day_share(basis: _SolarBasis, archetype: str, target_imp: float,
                    target_exp: float, pv_total: float) -> tuple:
    """Daytime share minimising the residual on both registers, at fixed PV.

    Coarse-to-fine rather than a derivative method: the residual surface has
    flat stretches where a slot's load crosses its solar value, so a gradient
    step is unreliable — and the parameter is one-dimensional and cheap.
    """
    lo, hi = 0.02, 0.95
    best = None
    for _ in range(4):
        for i in range(25):
            ds = lo + (hi - lo) * i / 24.0
            shape = archetype_shape(archetype, ds)
            imp, exp, gen_daily = _evaluate(basis, pv_total, shape,
                                            target_imp, target_exp)
            err = math.hypot(pct_err(imp, target_imp), pct_err(exp, target_exp))
            if best is None or err < best[0]:
                best = (err, ds, imp, exp, gen_daily, shape)
        span = (hi - lo) / 6.0
        lo, hi = max(0.02, best[1] - span), min(0.95, best[1] + span)
    return best


def calibrate(facts: BillFacts, answers: IntakeAnswers | None = None,
              pv_total_kwh: float | None = None) -> EnergyModel:
    """Build an energy model that reproduces the bill's registers.

    Two unknowns — total PV generation and the daytime share of general load —
    against two observations:

        import = sum over slots of max(0, general_load - solar)  == general register
        export = sum over slots of max(0, solar - general_load)  == export register

    CRITICAL: that pair does NOT identify both unknowns. There is a wide flat
    valley of solutions; a big array feeding a big daytime load and a small
    array feeding a small one leave the same footprint on a meter that only
    counts net flow in each direction. A solver returning one point and a 0.00%
    residual is not telling you it found the household — it is telling you two
    free parameters absorbed the shape error, which they always can.

    So `answers.pv_kw` (the inverter nameplate) fixes PV and leaves only the
    daytime share, which IS then identified. Without it this returns the
    midpoint of the admissible valley, sets `identified=False`, records the
    range in `pv_range_kw`, and callers must present it as a range.

    Controlled load is held out of the solve entirely: on the overnight
    switching window it never overlaps solar, so it cannot affect either
    residual, and the register is billed separately anyway.
    """
    answers = answers or IntakeAnswers()
    if not facts.periods:
        raise ValueError("a bill needs at least one rate period")
    days = facts.days
    mid = facts.start_date + dt_mod.timedelta(days=days // 2)
    yield_kwp = seasonal_yield(facts.latitude, mid)
    target_imp = facts.general_kwh
    target_exp = facts.export_kwh

    warnings: list = []
    identified = True
    pv_range_kw = None

    if not facts.has_solar:
        # The clean case. Import IS consumption; there is no inverse problem and
        # nothing to calibrate. The archetype's own share stands, unmoved, and
        # is reported as an assumption rather than a finding.
        dates = _day_list(facts)
        shape = archetype_shape(answers.archetype)
        # Per rate period, so a mid-cycle price change is priced on the kWh the
        # retailer billed for each slice rather than a period average.
        general = []
        for p in facts.periods:
            gen_daily = p.general_kwh / p.days if p.days else 0.0
            general.extend([[gen_daily * w for w in shape] for _ in range(p.days)])
        warnings.append(
            "No solar on this bill, so the intraday shape is completely "
            "unconstrained by it: the import total is the same whatever hour "
            "the energy was used. Every time-of-use and battery figure rests "
            "on the selected load profile alone — compare the archetypes "
            "rather than reading one of them as the answer.")
        return EnergyModel(
            dates=dates, solar=[[0.0] * SLOTS for _ in dates], general=general,
            cl_daily_kwh=facts.cl_daily_kwh(),
            cl_shape=window_shape(answers.cl_window),
            pv_total_kwh=0.0, pv_kw_implied=0.0,
            day_share=natural_day_share(answers.archetype),
            archetype=answers.archetype,
            fit_import_kwh=target_imp, fit_export_kwh=0.0,
            target_import_kwh=target_imp, target_export_kwh=0.0,
            identified=True, warnings=warnings,
        )

    basis = _solar_basis(facts, answers)

    if pv_total_kwh is None and answers.pv_kw:
        pv_total_kwh = answers.pv_kw * yield_kwp * days
    if pv_total_kwh is None:
        lo, hi = admissible_pv_range(facts, answers, _basis=basis)
        identified = False
        pv_total_kwh = (lo + hi) / 2.0
        pv_range_kw = (lo / days / yield_kwp, hi / days / yield_kwp)
        warnings.append(
            f"PV SIZE NOT IDENTIFIED: every system from {pv_range_kw[0]:.1f} kW "
            f"to {pv_range_kw[1]:.1f} kW reproduces this bill's import AND "
            f"export to within 0.5%, each with a compensating daytime load "
            f"share. This model uses the midpoint, which is a choice and not a "
            f"measurement. Enter the inverter's kW rating — it is the single "
            f"input that collapses this ambiguity.")

    err, day_share, imp, exp, gen_daily, shape = _best_day_share(
        basis, answers.archetype, target_imp, target_exp, pv_total_kwh)

    # The global fit above sets the one shared shape parameter (the daytime
    # share). Each rate period is then fitted to its OWN printed registers, so
    # a bill whose rates changed mid-cycle prices each slice on the kWh the
    # retailer actually billed for it rather than on a period-average smear.
    solar_grid, general = _fit_periods(facts, basis, shape)
    imp = exp = 0.0
    for day_g, day_s in zip(general, solar_grid):
        for g, s in zip(day_g, day_s):
            imp += max(0.0, g - s)
            exp += max(0.0, s - g)
    pv_total_kwh = sum(sum(d) for d in solar_grid)

    model = EnergyModel(
        dates=_day_list(facts), solar=solar_grid, general=general,
        cl_daily_kwh=facts.cl_daily_kwh(),
        cl_shape=window_shape(answers.cl_window),
        pv_total_kwh=pv_total_kwh,
        pv_kw_implied=pv_total_kwh / days / yield_kwp if yield_kwp else 0.0,
        day_share=day_share, archetype=answers.archetype,
        fit_import_kwh=imp, fit_export_kwh=exp,
        target_import_kwh=target_imp, target_export_kwh=target_exp,
        identified=identified, pv_range_kw=pv_range_kw, warnings=warnings,
    )

    if abs(model.import_err_pct) > 3.0 or abs(model.export_err_pct) > 3.0:
        model.warnings.append(
            "The model cannot reproduce the billed registers to within 3% at "
            "this PV size. If the nameplate was entered, it disagrees with the "
            "bill — shading, clipping, a part-period outage or a mis-parsed "
            "register. Treat every modelled figure as unusable until that is "
            "resolved.")

    natural = natural_day_share(answers.archetype)
    if identified and abs(day_share - natural) > 0.25:
        model.warnings.append(
            f"To match the export register, the daytime share of general load "
            f"had to move from this profile's natural {natural * 100:.0f}% to "
            f"{day_share * 100:.0f}%. That is a large correction: either the "
            f"declared occupancy pattern or the entered PV size is off, and "
            f"the other archetypes are worth comparing before acting.")
    return model


def admissible_pv_range(facts: BillFacts, answers: IntakeAnswers | None = None,
                        tolerance_pct: float = 0.5, samples: int = 60,
                        _basis: "_SolarBasis | None" = None) -> tuple:
    """Range of period PV generation that reproduces both billed registers.

    Scans PV size, and at each one takes the best achievable daytime share,
    keeping every size whose residual stays under `tolerance_pct` on both
    registers. The width of what comes back IS the honest uncertainty in every
    battery and extra-solar figure derived from this bill, so it is reported
    rather than hidden behind a point estimate.

    The returned bounds are in kWh over the whole bill period, not kW — divide
    by days and `seasonal_yield` for a nameplate figure.
    """
    answers = answers or IntakeAnswers()
    basis = _basis or _solar_basis(facts, answers)
    days = facts.days
    ti, te = facts.general_kwh, facts.export_kwh
    floor = te                     # cannot export more than was generated
    ceiling = floor + 60.0 * days  # 60 kWh/day is past any residential array

    ok = []
    for i in range(samples):
        pv_total = floor + (ceiling - floor) * i / (samples - 1)
        best = _best_day_share(basis, answers.archetype, ti, te, pv_total)
        if best and best[0] <= tolerance_pct:
            ok.append(pv_total)
    if not ok:
        return (floor, floor)
    return (min(ok), max(ok))


# ---------------------------------------------------------------------------
# Scenario synthesis — the series PlanCalculator actually consumes
# ---------------------------------------------------------------------------

@dataclass
class SynthesisedProfiles:
    """Per-day, per-slot kWh for each role the calculator asks about.

    `cl_daily_kwh` > 0 means the controlled load is still on its own register
    and must be priced as a declared CL device, NOT folded into `grid_import`.
    Zero means this scenario moved it onto the main circuit, where it is already
    inside `general` (and so inside import/export) and must not be billed twice.
    """

    dates: list
    grid_import: list     # list[list[float]]
    grid_export: list
    solar: list
    general: list
    cl_daily_kwh: float
    cl_on_own_register: bool
    cl_supply_charged: bool

    def totals(self) -> dict:
        def s(grid):
            return sum(sum(d) for d in grid)
        return {"import_kwh": s(self.grid_import), "export_kwh": s(self.grid_export),
                "solar_kwh": s(self.solar), "general_kwh": s(self.general),
                "cl_kwh": self.cl_daily_kwh * len(self.dates)}


def synthesise(model: EnergyModel, answers: IntakeAnswers,
               scenario: Scenario) -> SynthesisedProfiles:
    """Turn a calibrated model plus a CL placement into the four role series.

    The controlled-load placement is the whole scenario axis (design doc §6),
    and everything that moves when the load moves has to move here or the
    scenario flatters itself:

      - `controlled` (status quo): CL energy stays off the main meter entirely.
        It is priced by `_compute_bill_items` at the plan's own CL rate plus the
        CL register's daily supply charge, as a separate bill line, exactly as a
        real bill itemises it.
      - `main_solar`: CL energy is added to the general load inside
        `cl_main_window`, so it competes with PV for the same kWh — it reduces
        export and raises import by less than 1:1, which is the upside. It is
        then charged at the plan's *general* rate for those hours, loses the
        cheap CL rate, and drops the CL daily supply charge.
      - `main_unchanged`: same re-wiring, same loss of the CL rate, but still
        run overnight where no solar exists. The control.

    The element's kW rating caps how fast that energy can be absorbed, so a
    window too short to deliver the day's kWh is widened and a warning is
    attached to the model rather than silently producing a physically impossible
    profile.
    """
    dates = model.dates
    n = len(dates)
    cl_daily = model.cl_daily_kwh

    if scenario.cl_placement == CL_CONTROLLED or cl_daily <= 0:
        general = model.general
        cl_on_own = cl_daily > 0
        cl_supply = cl_daily > 0
    else:
        window = (answers.cl_main_window if scenario.cl_placement == CL_MAIN_SOLAR
                  else answers.cl_window)
        window = _widen_for_rate(window, cl_daily, answers.cl_max_kw, model)
        shape = window_shape(window)
        general = [[g + cl_daily * w for g, w in zip(day, shape)]
                   for day in model.general]
        cl_on_own = False
        cl_supply = False

    grid_import, grid_export = [], []
    for day_general, day_solar in zip(general, model.solar):
        imp, exp = [], []
        for g, s in zip(day_general, day_solar):
            imp.append(max(0.0, g - s))
            exp.append(max(0.0, s - g))
        grid_import.append(imp)
        grid_export.append(exp)

    return SynthesisedProfiles(
        dates=dates, grid_import=grid_import, grid_export=grid_export,
        solar=model.solar, general=general,
        cl_daily_kwh=cl_daily if cl_on_own else 0.0,
        cl_on_own_register=cl_on_own, cl_supply_charged=cl_supply,
    )


def _widen_for_rate(window: tuple, daily_kwh: float, max_kw: float | None,
                    model: EnergyModel) -> tuple:
    """Widen a window that physically cannot deliver `daily_kwh` at `max_kw`.

    A 3.6 kW element cannot put 16.7 kWh into a tank inside a 4-hour window. The
    LP's `hour_mask` enforces this for a real declared load; here the profile is
    synthesised directly, so without this check the model would quietly assume
    an element twice the size it has.
    """
    if not max_kw or max_kw <= 0 or daily_kwh <= 0:
        return window
    start, end = window
    hours = (end - start) if end > start else (24.0 - start + end)
    needed = daily_kwh / max_kw
    if needed <= hours + 1e-9:
        return window
    extra = needed - hours
    model.warnings.append(
        f"A {max_kw:.1f} kW element needs {needed:.1f} h/day to deliver "
        f"{daily_kwh:.1f} kWh, but the chosen window is only {hours:.1f} h. "
        f"The window was extended by {extra:.1f} h past its end — some of that "
        f"energy therefore runs outside the solar peak, which this scenario's "
        f"figures already account for.")
    return (start, (end + extra) % 24.0)
