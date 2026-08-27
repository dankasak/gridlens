"""Offline tests for the Grid Lens setup config flow.

Neither Home Assistant nor voluptuous is importable in this container, so both are
stubbed far enough to drive `GridLensConfigFlow` end to end and assert on what it
shows, in what order, and what it finally stores.

Covers the behaviours the 2026-08-21 setup-flow simplification depends on:
  * plan-data coverage is checked on the first submit, not discovered at the last step
  * an uncovered state aborts instead of leading the user into an empty plan dropdown
  * a state with exactly one covered network never shows the distributor screen
  * the API is asked for plans once, and the final step reuses that prefetch
  * energy-sensor unit validation applies to solar and export, not just import
  * every per-device list stored by device_power stays index-aligned
  * setup writes the battery/controlled-load defaults the rest of the code reads

Run: python3 tests/test_config_flow.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import types

# The flow logs a warning whenever Energy-dashboard discovery is unavailable, which is
# always here (homeassistant.components.energy isn't stubbed) — that's the documented
# fallback, not a failure, so keep it out of the test output.
logging.disable(logging.WARNING)

_COMPONENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------- voluptuous stub
class _Marker:
    """Stand-in for vol.Required / vol.Optional — carries the key and its default."""

    def __init__(self, key, default=None, description=None):
        self.key = key
        self.default = default
        self.description = description or {}

    def __str__(self):
        return str(self.key)

    def __hash__(self):
        return hash(str(self.key))

    def __eq__(self, other):
        return str(self) == str(other)


class _Schema:
    def __init__(self, d):
        self.schema = d

    def keys(self):
        return [str(k) for k in self.schema]

    def marker(self, name):
        for k in self.schema:
            if str(k) == name:
                return k
        raise KeyError(name)


def _install_stubs() -> None:
    def _mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    vol = _mod("voluptuous")
    vol.Schema = _Schema
    vol.Required = lambda key, default=None, description=None: _Marker(key, default, description)
    vol.Optional = lambda key, default=None, description=None: _Marker(key, default, description)

    aiohttp = _mod("aiohttp")
    aiohttp.ClientTimeout = lambda total=None: total
    aiohttp.ClientSession = object

    ha = _mod("homeassistant")

    ce = _mod("homeassistant.config_entries")

    class _ConfigFlow:
        def __init_subclass__(cls, **kw):
            pass

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_show_menu(self, **kw):
            return {"type": "menu", **kw}

        def async_create_entry(self, **kw):
            return {"type": "create_entry", **kw}

        def async_abort(self, **kw):
            return {"type": "abort", **kw}

    ce.ConfigFlow = _ConfigFlow
    ce.OptionsFlow = type("OptionsFlow", (), {})
    ce.ConfigEntry = type("ConfigEntry", (), {})
    ce.FlowResult = dict
    ha.config_entries = ce

    core = _mod("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn
    ha.core = core

    components = _mod("homeassistant.components")
    pn = _mod("homeassistant.components.persistent_notification")
    pn.created = []
    pn.async_create = lambda hass, msg, title=None, notification_id=None: pn.created.append(
        (title, notification_id)
    )
    components.persistent_notification = pn
    ha.components = components

    helpers = _mod("homeassistant.helpers")

    iid = _mod("homeassistant.helpers.instance_id")

    async def _get(hass):
        return "0f9d2c1e-1111-4222-8333-444455556666"

    iid.async_get = _get
    helpers.instance_id = iid

    sel = _mod("homeassistant.helpers.selector")
    for name in ("EntitySelector", "EntitySelectorConfig", "SelectSelector",
                 "SelectSelectorConfig", "TextSelector", "TextSelectorConfig",
                 "NumberSelector", "NumberSelectorConfig", "BooleanSelector"):
        setattr(sel, name, lambda *a, **k: None)
    sel.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown", LIST="list")
    sel.NumberSelectorMode = types.SimpleNamespace(BOX="box")
    sel.TextSelectorType = types.SimpleNamespace(EMAIL="email")
    helpers.selector = sel

    cv = _mod("homeassistant.helpers.config_validation")
    cv.string = str
    helpers.config_validation = cv

    ac = _mod("homeassistant.helpers.aiohttp_client")
    ac.async_get_clientsession = lambda hass: hass.session
    helpers.aiohttp_client = ac

    ha.helpers = helpers


def _load(path, fqname, package=None):
    spec = importlib.util.spec_from_file_location(fqname, path)
    module = importlib.util.module_from_spec(spec)
    if package is not None:
        module.__package__ = package
    sys.modules[fqname] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap():
    _install_stubs()
    pkg = types.ModuleType("gl")
    pkg.__path__ = []
    sys.modules["gl"] = pkg
    _load(os.path.join(_COMPONENT, "const.py"), "gl.const", package="gl")
    inv = types.ModuleType("gl.inverters")
    inv.__path__ = []
    inv.INVERTER_BRANDS = {"sigenergy": {"mqtt": "Sigenergy (MQTT)"}}
    inv.detect_inverter_brand = lambda hass: getattr(hass, "detected_inverter", None)
    sys.modules["gl.inverters"] = inv
    # Stubbed rather than loaded: credentials.py talks to homeassistant.helpers.storage,
    # and what these tests care about is whether the flow *consults* the mirror on 409
    # and *writes* it on success — not how Store serialises.
    creds = types.ModuleType("gl.credentials")
    creds.__path__ = []
    creds.saved = []
    creds.stored = None

    async def _save(hass, *, ha_uuid, api_key, email, api_url):
        creds.saved.append({"ha_installation_id": ha_uuid, "api_key": api_key,
                            "email": email, "api_url": api_url})

    async def _load_creds(hass, ha_uuid):
        return creds.stored

    creds.async_save_credentials = _save
    creds.async_load_credentials = _load_creds
    sys.modules["gl.credentials"] = creds
    return _load(os.path.join(_COMPONENT, "config_flow.py"), "gl.config_flow", package="gl")


cf = _bootstrap()
import gl.const as const  # noqa: E402


# ----------------------------------------------------------------- fakes
class FakeState:
    def __init__(self, attrs):
        self.attributes = attrs


class FakeStates:
    def __init__(self, d=None):
        self._d = d or {}

    def get(self, eid):
        return self._d.get(eid)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records every request; answers from a {(method, path): (status, payload)} map."""

    def __init__(self, routes, fail=()):
        self.routes = routes
        self.fail = set(fail)
        self.calls = []

    def _handle(self, method, url, params=None):
        path = url.split("api.gridlens.au")[-1] if "api.gridlens.au" in url else url
        self.calls.append((method, path, dict(params or {})))
        if path in self.fail:
            raise RuntimeError("boom")
        status, payload = self.routes.get(path, (404, None))
        if callable(payload):
            payload = payload(params or {})
        return FakeResponse(status, payload)

    def get(self, url, **kw):
        return self._handle("GET", url, kw.get("params"))

    def post(self, url, **kw):
        return self._handle("POST", url, kw.get("params"))


class FakeHass:
    def __init__(self, session, states=None):
        self.session = session
        self.states = FakeStates(states)
        self.data = {}
        self.config = types.SimpleNamespace(external_url=None, internal_url=None)
        self.detected_inverter = None


_PLANS = [
    {"id": "globird_zerohero", "retailer": "GloBird", "name": "ZeroHero"},
    {"id": "agl_battery_rewards", "retailer": "AGL", "name": "Battery Rewards"},
]


def _routes(ausgrid=_PLANS, endeavour=None, essential=None, register=(200, {"api_key": "gl_test"})):
    by_net = {"Ausgrid": ausgrid, "Endeavour Energy": endeavour, "Essential Energy": essential}
    return {
        "/plans/list": (200, lambda p: by_net.get(p.get("network"), []) or []),
        "/vpp-programs/list": (200, []),
        "/register": register,
        "/plans/meta": (200, {}),
    }


def _flow(session, states=None):
    f = cf.GridLensConfigFlow()
    f.hass = FakeHass(session, states)
    # Energy-dashboard discovery imports homeassistant.components.energy, which isn't
    # stubbed — the flow already treats a failure as "nothing discovered".
    return f


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


# ----------------------------------------------------------------- tests
def test_coverage_gate_aborts_for_uncovered_state():
    """A state with no plan data fails on screen 1, not at the final dropdown."""
    session = FakeSession(_routes(ausgrid=[]))
    f = _flow(session)
    res = run(f.async_step_user({const.CONF_STATE: "VIC", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    assert res["type"] == "abort", res
    assert res["reason"] == "state_not_supported", res
    assert res["description_placeholders"]["state"] == "VIC"
    # Every VIC network was probed, and nothing beyond coverage was requested.
    assert {c[1] for c in session.calls} == {"/plans/list"}
    assert len(session.calls) == len(const.DISTRIBUTORS["VIC"])
    print("  ✓ uncovered state aborts on the first screen")


def test_single_covered_network_skips_distributor():
    """NSW has three networks but only Ausgrid has plans — don't ask a one-answer question."""
    session = FakeSession(_routes())
    f = _flow(session)
    res = run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    assert res["type"] == "form", res
    assert res["step_id"] == "sensors", res["step_id"]
    assert f._distributor == "Ausgrid"
    assert f._api_plans == _PLANS
    print("  ✓ single covered network auto-selected, distributor screen skipped")


def test_multiple_covered_networks_ask():
    session = FakeSession(_routes(endeavour=_PLANS))
    f = _flow(session)
    res = run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    assert res["step_id"] == "distributor", res["step_id"]
    # Only the networks that actually returned plans are offered.
    assert sorted(f._plans_by_network) == ["Ausgrid", "Endeavour Energy"], f._plans_by_network
    res2 = run(f.async_step_distributor({const.CONF_DISTRIBUTOR: "Endeavour Energy"}))
    assert res2["step_id"] == "sensors"
    assert f._api_plans == _PLANS
    print("  ✓ two covered networks -> distributor screen, choice carries its prefetch")


def test_api_unreachable_fails_on_first_screen():
    """Total failure is 'cannot_connect' here, not an abort and not eight screens later."""
    session = FakeSession(_routes(), fail=["/plans/list"])
    f = _flow(session)
    res = run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    assert res["type"] == "form" and res["step_id"] == "user", res
    assert res["errors"] == {"base": "cannot_connect"}, res["errors"]
    print("  ✓ unreachable API re-shows screen 1 with cannot_connect")


def test_first_screen_asks_two_questions():
    session = FakeSession(_routes())
    f = _flow(session)
    res = run(f.async_step_user())
    assert res["data_schema"].keys() == [const.CONF_STATE, const.CONF_GRIDLENS_EMAIL], \
        res["data_schema"].keys()
    print("  ✓ first screen is state + email only (no postcode, no API URL)")


def test_energy_sensor_validation_covers_all_three():
    """Solar and export carry the same kWh/total requirement as import."""
    states = {
        "sensor.good": FakeState({"unit_of_measurement": "kWh", "state_class": "total_increasing"}),
        "sensor.watts": FakeState({"unit_of_measurement": "W", "state_class": "measurement"}),
        "sensor.no_class": FakeState({"unit_of_measurement": "kWh", "state_class": "measurement"}),
    }
    hass = FakeHass(FakeSession(_routes()), states)
    errors = cf._validate_energy_sensors(hass, {
        const.CONF_ENERGY_SENSOR: "sensor.good",
        const.CONF_SOLAR_SENSOR: "sensor.watts",
        const.CONF_GRID_EXPORT_SENSOR: "sensor.no_class",
    })
    assert errors == {
        const.CONF_SOLAR_SENSOR: "wrong_unit_power",
        const.CONF_GRID_EXPORT_SENSOR: "wrong_state_class",
    }, errors
    # A price sensor is not an energy counter and must not be validated as one.
    assert cf._validate_energy_sensors(hass, {const.CONF_IMPORT_PRICE_SENSOR: "sensor.watts"}) == {}
    print("  ✓ import, solar and export all validated; price sensors exempt")


def test_full_run_through_minimal_install():
    """No battery, no dashboard devices: state+email, sensors, battery, plan. Four screens."""
    session = FakeSession(_routes())
    states = {"sensor.imp": FakeState(
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"})}
    f = _flow(session, states)
    seen = []

    res = run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    seen.append(res["step_id"])
    res = run(f.async_step_sensors({const.CONF_ENERGY_SENSOR: "sensor.imp"}))
    seen.append(res["step_id"])
    res = run(f.async_step_battery({const.CONF_HAS_BATTERY: False}))
    seen.append(res["step_id"])
    res = run(f.async_step_current_plan({const.CONF_CURRENT_PLAN: "globird_zerohero"}))

    assert seen == ["sensors", "battery", "current_plan"], seen
    assert res["type"] == "create_entry", res
    data = res["data"]
    assert data[const.CONF_STATE] == "NSW"
    assert data[const.CONF_DISTRIBUTOR] == "Ausgrid"
    assert data[const.CONF_GRIDLENS_API_KEY] == "gl_test"
    assert data[const.CONF_CURRENT_PLAN] == "globird_zerohero"
    # Defaults the rest of the codebase reads directly must be present, not absent.
    assert data[const.CONF_HAS_CONTROLLED_LOAD_1] is False
    assert data[const.CONF_HAS_CONTROLLED_LOAD_2] is False
    assert data[const.CONF_BATTERY_EFFICIENCY] == 95.0
    assert data[const.CONF_BATTERY_MIN_SOC] == 10.0
    assert data[const.CONF_BATTERY_MAX_SOC] == 90.0
    # Coverage was prefetched once; the final step did not re-fetch plans.
    assert [c[1] for c in session.calls].count("/plans/list") == len(const.DISTRIBUTORS["NSW"])
    assert ("POST", "/register", {}) in session.calls
    print("  ✓ minimal install completes in 4 screens with defaults written")


def test_device_power_lists_stay_aligned():
    """Every per-device list must match len(selected) — downstream code zips them."""
    session = FakeSession(_routes())
    states = {
        "sensor.ev": FakeState({"friendly_name": "EV Charger"}),
        "sensor.pool": FakeState({"friendly_name": "Pool Pump"}),
    }
    f = _flow(session, states)
    f._sensor_data = {const.CONF_DEFERRABLE_LOAD_SENSORS: ["sensor.ev", "sensor.pool"]}

    form = run(f.async_step_device_power())
    assert form["data_schema"].keys() == ["max_kw_0", "switch_0", "max_kw_1", "switch_1"], \
        form["data_schema"].keys()
    assert form["description_placeholders"]["device_0_name"] == "EV Charger"

    run(f.async_step_device_power({"max_kw_0": 7.4, "switch_0": "switch.ev", "max_kw_1": 1.1}))
    d = f._sensor_data
    assert d[const.CONF_DEFERRABLE_LOAD_MAX_KW] == [7.4, 1.1]
    assert d[const.CONF_DEFERRABLE_LOAD_SWITCHES] == ["switch.ev", ""]
    for key in (const.CONF_DEFERRABLE_LOAD_CLIMATE_ON_MODE,
                const.CONF_DEFERRABLE_LOAD_SOC_SENSORS,
                const.CONF_DEFERRABLE_LOAD_CONTROLLED_LOAD,
                const.CONF_DEFERRABLE_LOAD_CL_IN_AGGREGATE,
                const.CONF_DEFERRABLE_LOAD_SETPOINT,
                const.CONF_DEFERRABLE_LOAD_SETPOINT_UNIT,
                const.CONF_DEFERRABLE_LOAD_PHASES,
                const.CONF_DEFERRABLE_LOAD_VOLTAGE,
                const.CONF_DEFERRABLE_LOAD_MIN_CURRENT,
                const.CONF_DEFERRABLE_LOAD_PLUG_SENSOR):
        assert len(d[key]) == 2, (key, d[key])
    print("  ✓ device_power shows 2 fields/device and keeps all 12 lists aligned")


def test_battery_step_records_detected_inverter():
    """The inverter screen is gone; a confident detection is recorded silently."""
    session = FakeSession(_routes())
    f = _flow(session)
    f.hass.detected_inverter = ("sigenergy", "mqtt")
    f._sensor_data = {}
    run(f.async_step_battery({const.CONF_HAS_BATTERY: True, const.CONF_BATTERY_CAPACITY: 10.0}))
    assert f._sensor_data[const.CONF_INVERTER_BRAND] == "sigenergy"
    assert f._sensor_data[const.CONF_INVERTER_TRANSPORT] == "mqtt"

    f2 = _flow(FakeSession(_routes()))
    f2._sensor_data = {}
    run(f2.async_step_battery({const.CONF_HAS_BATTERY: True, const.CONF_BATTERY_CAPACITY: 10.0}))
    assert const.CONF_INVERTER_BRAND not in f2._sensor_data
    print("  ✓ inverter auto-detected when possible, left unset otherwise")


def test_battery_capacity_validated():
    f = _flow(FakeSession(_routes()))
    f._sensor_data = {}
    res = run(f.async_step_battery({const.CONF_HAS_BATTERY: True, const.CONF_BATTERY_CAPACITY: 0}))
    assert res["errors"] == {const.CONF_BATTERY_CAPACITY: "invalid_capacity"}, res["errors"]
    print("  ✓ zero battery capacity still rejected")


def test_setup_never_shows_a_battery_advanced_field():
    schema = cf._battery_schema({}, basic=True)
    for advanced in (const.CONF_BATTERY_EFFICIENCY, const.CONF_BATTERY_MIN_SOC,
                     const.CONF_BATTERY_MAX_SOC):
        assert advanced not in schema.keys(), advanced
    full = cf._battery_schema({}, basic=False)
    for advanced in (const.CONF_BATTERY_EFFICIENCY, const.CONF_BATTERY_MIN_SOC,
                     const.CONF_BATTERY_MAX_SOC):
        assert advanced in full.keys(), advanced
    print("  ✓ battery guardrails hidden at setup, still present in options")


def test_reinstall_offers_manual_key():
    """A 409 from /register means this install already has a key — ask for it."""
    session = FakeSession(_routes(register=(409, {"detail": "already registered"})))
    f = _flow(session)
    run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
    f._sensor_data = {}
    res = run(f.async_step_current_plan({const.CONF_CURRENT_PLAN: "globird_zerohero"}))
    assert res["step_id"] == "manual_key", res["step_id"]
    assert res["description_placeholders"]["email"] == "a@b.com"

    ok = run(f.async_step_manual_key({const.CONF_GRIDLENS_API_KEY: "gl_existing"}))
    assert ok["type"] == "create_entry", ok
    assert ok["data"][const.CONF_GRIDLENS_API_KEY] == "gl_existing"
    assert ok["data"][const.CONF_CURRENT_PLAN] == "globird_zerohero"
    print("  ✓ 409 reinstall path reaches manual key entry and preserves the plan")


def test_reinstall_recovers_mirrored_key_without_asking():
    """The reinstall gap (FEATURES.md 12a): a 409 used to dead-end on manual_key, asking
    for a key the API only ever displays once. With a local mirror present and still
    valid, setup must complete silently instead."""
    import gl.credentials as creds

    creds.stored = {"ha_installation_id": "u-1", "api_key": "gl_mirrored",
                    "email": "a@b.com", "api_url": "https://api.gridlens.au"}
    creds.saved = []
    try:
        session = FakeSession(_routes(register=(409, {"detail": "already registered"})))
        f = _flow(session)
        run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
        f._sensor_data = {}
        res = run(f.async_step_current_plan({const.CONF_CURRENT_PLAN: "globird_zerohero"}))
        assert res["type"] == "create_entry", res
        assert res["data"][const.CONF_GRIDLENS_API_KEY] == "gl_mirrored", res["data"]
        assert res["data"][const.CONF_CURRENT_PLAN] == "globird_zerohero"
    finally:
        creds.stored = None
    print("  \u2713 409 with a valid local mirror recovers the key, no manual entry")


def test_recovery_rejects_a_mirrored_key_the_api_no_longer_accepts():
    """Fail safe: a stale mirrored key must fall back to asking, never be written into
    the entry — an install that looks configured and then 401s on every refresh is worse
    than one that asked a question."""
    import gl.credentials as creds

    creds.stored = {"ha_installation_id": "u-1", "api_key": "gl_revoked",
                    "email": "a@b.com", "api_url": "https://api.gridlens.au"}
    try:
        routes = _routes(register=(409, {"detail": "already registered"}))
        routes["/plans/meta"] = (401, {"detail": "invalid key"})
        session = FakeSession(routes)
        f = _flow(session)
        run(f.async_step_user({const.CONF_STATE: "NSW", const.CONF_GRIDLENS_EMAIL: "a@b.com"}))
        f._sensor_data = {}
        res = run(f.async_step_current_plan({const.CONF_CURRENT_PLAN: "globird_zerohero"}))
        assert res["step_id"] == "manual_key", res
    finally:
        creds.stored = None
    print("  \u2713 a revoked mirrored key falls back to manual entry, not a broken entry")


def test_upgrade_pitch_is_a_notification_not_a_redirect():
    sys.modules["homeassistant.components.persistent_notification"].created.clear()
    session = FakeSession(_routes())
    f = _flow(session)
    f._state = "NSW"
    f._sensor_data = {}
    res = run(f.async_step_finalize())
    assert res["type"] == "create_entry"
    created = sys.modules["homeassistant.components.persistent_notification"].created
    assert created and created[0][1] == f"{const.DOMAIN}_upgrade", created
    print("  ✓ setup ends on entry creation; upgrade pitch left as a notification")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} config-flow tests\n")
    for t in tests:
        t()
    print(f"\n✅ all {len(tests)} passed")
