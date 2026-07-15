# GridLens Inverter HAL — Contract Specification

**Status:** design reference · **Purpose:** clean-room spec for a multi-vendor
battery/inverter control layer in GridLens.

> ## Legal basis (read first)
> This document is a **clean-room specification**. It was written by *observing the
> behaviour* of the PolyForm-Noncommercial-licensed `bolagnaise/PowerSync` project and
> recording **facts** (Modbus register addresses, scaling gains, vendor control
> sequences, protocol endpoints) together with **our own architectural synthesis**.
> Register maps and control sequences are facts about third-party hardware and are not
> copyrightable; most originate from manufacturer Modbus specifications and
> permissively-licensed upstream libraries (see per-brand "source of truth").
>
> **Rules for implementing against this spec:**
> 1. Do **not** copy, paste, or transliterate PowerSync source into GridLens.
> 2. Implement each driver from the vendor's Modbus/API doc or a permissive upstream
>    library (Apache/MIT), citing it in the driver header.
> 3. Where we already own the register map (our `custom_components/sigen`), use ours.
> 4. Keep this doc updated as the contract of record.

---

## 1. Why a HAL

GridLens' optimizer emits an abstract dispatch plan — a sequence of `(action, power)`
intervals. Every piece of vendor-specific knowledge must live **below** a uniform
interface so the optimizer, the executor, and the product UI never learn a brand name.
That boundary is the HAL.

```
┌─────────────────────────────────────────────────────────────┐
│ Optimizer (LP/MPC)  — brand-agnostic; emits actions + power  │
├─────────────────────────────────────────────────────────────┤
│ ScheduleExecutor    — plan → actions; safety; deadman restore│
├─────────────────────────────────────────────────────────────┤
│ BatteryController   — uniform force_charge/discharge/idle/... │
│                       + guardrails (SOC floor, reserve trust) │
├─────────────────────────────────────────────────────────────┤
│ InverterController  — per brand/model driver (this spec)     │
│   ├ transport: native Modbus | REST | HA-entity proxy        │
│   └ vendor control sequence                                  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Layer 1 — `InverterController` abstract contract

Every driver inherits this. Constructor takes `(host, port=502, slave_id=1, model=None)`
for Modbus drivers; entity-proxy drivers take an `entity_prefix`/config instead.

### Required (abstract) methods

| Method | Semantics | Must be idempotent |
|---|---|---|
| `connect() -> bool` | Establish/verify the transport (open Modbus socket, validate REST auth, or confirm the required HA entities exist). Returns success. | yes |
| `disconnect() -> None` | Release the transport. No-op for stateless (UDP/entity) drivers. | yes |
| `curtail(home_load_w=None, rated_capacity_w=None) -> bool` | **Solar export suppression.** Stop/limit *production* so we don't export during negative prices. `home_load_w` enables load-following (limit to house load rather than hard zero). | yes |
| `restore() -> bool` | Undo `curtail` — return export/production limit to normal (100% or saved value). | yes |
| `get_status() -> InverterState` | Read a canonical snapshot (see §7). | yes (read-only) |

### Optional battery-dispatch methods (present only on storage-capable drivers)

| Method | Semantics |
|---|---|
| `force_charge(power_kw/power_w) -> bool` | Enter a forced-charge mode at ≈ the requested rate. |
| `force_discharge(power_kw/power_w) -> bool` | Enter a forced-discharge/export mode at ≈ the requested rate, **clamped to the DNSP export limit**. |
| `set_self_consumption_mode() -> bool` | Return battery to autonomous self-consumption (the "home" mode). |
| `set_standby_mode()` / `set_idle()` / `set_idle_mode()` | **Hold current SOC** — no charge, no discharge (see §6.1, the IDLE problem). |
| `restore_normal() -> bool` | Full handback: release forced dispatch **and** restore any export limit; hand control to native/VPP. Called on executor stop (deadman). |
| `set_charge_rate_limit(kw)` / `set_discharge_rate_limit(kw)` | Cap battery power without changing mode. |
| `set_export_limit(kw)` / `restore_export_limit()` | Grid-point export cap (DNSP compliance / zero-export). |

### Cross-cutting infrastructure the base provides

- **Per-endpoint request lock** keyed on `(host, port, slave_id)` — serialises all
  Modbus transactions to one physical endpoint. Two controllers pointed at the same
  gateway must share a lock. **Critical for us** (see §5.3, Modbus contention).
- `test_connection() -> (bool, str)` for the config flow.
- Signed/unsigned 16/32/64-bit register codec helpers.

---

## 3. Layer 2 — `BatteryController` (uniform wrapper + guardrails)

Wraps whichever `InverterController` is active and presents ONE interface to the
executor, regardless of brand. This is where **safety** lives.

Interface: `force_charge(duration_minutes, power_w)`, `force_discharge(...)`,
`restore_normal()`, `set_self_consumption_mode()`, `set_autonomous_mode()`,
`read_backup_reserve() -> ReserveReading`, `get_backup_reserve()`, `set_backup_reserve(pct)`.

### Guardrail responsibilities (enforce here, never trust the optimizer)

1. **SOC floor** — never discharge below the configured reserve, independent of the LP.
2. **Backup-reserve provenance/trust** — reads are tagged with a trust level
   (`LIVE` from a fresh readback vs `site_info_cache` stale) and a source string. A
   pending in-flight write wins over a cached read. Never round-trip a stale reserve
   value back into a write.
3. **Duration + auto-expiry** — forced modes are commanded with a duration
   (`interval + buffer`) so the inverter self-reverts if we crash (belt-and-braces with
   the deadman).
4. **Rate clamping** — clamp `power_w` to inverter rated + configured limits.

---

## 4. Layer 2.5 — `ScheduleExecutor` (plan → commands)

Translates the optimizer's per-interval decision into `BatteryController` calls.

- **Action enum:** `IDLE`, `CHARGE`, `DISCHARGE` (legacy generic), `CONSUME`
  (battery→home), `EXPORT` (battery→grid), `OFF_GRID` (contactor open).
- **Cost functions:** `cost` (minimise), `profit` (maximise), `self_consumption`.
- **Transition rule:** track the previous action; only issue a mode change when leaving
  a *forced* mode (`CHARGE`/`EXPORT`) back toward `CONSUME`/`IDLE` → call
  `set_self_consumption_mode()`. Avoids re-writing the same mode every tick.
- **Deadman:** `stop(restore_normal=True)` calls `restore_normal()` so that disabling
  the optimizer (or an unload) always returns the battery to native control. **Any
  watchdog timeout must do the same.**
- Timer is aligned to interval boundaries (`async_track_time_change`, minute-aligned).

---

## 5. Transport taxonomy (pick per brand — and per install)

### 5.1 Native local Modbus TCP (`pymodbus`)
Driver opens its own socket to the inverter/dongle. Full read+write control.
**Brands:** Sigenergy, FoxESS, Sungrow (SG string + SH hybrid), AlphaESS, Anker Solix,
GoodWe, Huawei, Fronius (string), Solax (string), SolarEdge (direct).

### 5.2 REST / HTTP
Driver talks the vendor's web API.
**Brands:** Enphase (local Envoy/IQ Gateway REST **+** Enlighten cloud JWT for fw 7.x+),
Zeversolar (local HTTP), SAJ H2 (web/TOU API).

### 5.3 HA-entity proxy ⭐ (drive an *already-installed* integration's entities)
Instead of opening its own connection, the driver calls
`number.set_value` / `select.select_option` / `switch.turn_on` on entities exposed by an
integration the user already runs, and reads that integration's sensors.
**Brands (proxy variants):** Fronius Reserva (GEN24 storage), GoodWe (`goodwe_entity`),
Neovolt, Solax battery (`solax_battery`), FoxESS (`foxess_entity`, drives the
`nathanmarlor/foxess_modbus` write service), SolarEdge battery (HA-entity fallback).

> **⭐ Decision for GridLens — this matters for our Sigenergy.**
> We already run `custom_components/sigen`, which holds an open Modbus master to the
> plant. A **second** Modbus master (a native HAL driver) risks transaction collisions
> and dongle lockups even with per-endpoint locking, because the locks live in different
> processes/integrations. **Prefer the HA-entity-proxy transport when a mature
> integration is already installed** — for Sigenergy, write to the existing
> `select.plant_remote_ems_control_mode`, `number.*` and read `sensor.sigen_*` rather
> than opening our own socket. Fall back to native Modbus only when no integration owns
> the device. Design the HAL so a brand can have *both* a native and a proxy driver,
> chosen at config time.

---

## 6. Cross-cutting hard problems (the expensive knowledge)

These are the parts that cost weeks on real hardware. Each brand solves them differently.

### 6.1 The IDLE-hold problem
"Hold SOC — neither charge nor discharge" is **not** a native mode on most inverters, and
naive approaches backfire (the firmware grid-charges to reach backup SOC, or
self-consumption drains the battery). Per-brand solution table:

| Brand | IDLE-hold mechanism |
|---|---|
| Sigenergy | Remote EMS **STANDBY (mode 1)** — stops charge/discharge without touching backup_reserve (prevents firmware grid-charging to reach backup SOC). |
| Sungrow SH | EMS **Forced + Stop** command (prevents self-consumption discharge). |
| Fronius Reserva | Zero the PV-charge limit **and** the discharge limit. |
| Neovolt | Raise the **discharge cut-off SOC to the current SOC** (best-effort lock). |
| SAJ H2 | TOU config that neither charges nor discharges; grid serves home. |
| AlphaESS | Best-effort battery lock at current SoC (dispatch power 0). |
| Tesla Powerwall | `set_autonomous_mode()` **+** backup_reserve — reserve alone is **not** sufficient. |

### 6.2 force_charge — mode selection matters
Charging "from grid" vs "PV-first" is a real behavioural fork:
- **Sigenergy:** use **CHARGE_PV (mode 4)**, *not* CHARGE_GRID (mode 3) — mode 3
  **suppresses solar generation entirely**. Set `ESS_MAX_CHARGE_LIMIT` *before*
  entering charge mode so we don't charge at rated capacity.
- **GoodWe:** ECO_CHARGE mode.
- **Sungrow SH:** EMS forced mode + charge command + power register; verify the write.
- General rule: **set the rate limit register first, then switch mode last** (mode write
  commits the command).

### 6.3 force_discharge — export clamping & PV suppression
- Always **clamp the target to the configured DNSP export limit** before writing.
- Discharge-mode choice is **site-dependent**: PV-first preserves solar but may not pull
  from the battery; ESS-first pulls from storage but can suppress PV. Pick the least
  invasive mode that can plausibly meet the target; read status to decide.
- **Sungrow SH:** disable any stale zero-export limit *before* forcing export, or the
  battery can't push to grid.

### 6.4 Restore / handback (deadman)
Every driver must have a `restore_normal()` that returns the device to native/VPP
self-use. Called on: executor stop, integration unload, **and any watchdog timeout**.
Sigenergy also exposes `disable_remote_ems()` to fully release control back to native/VPP.

### 6.5 Commit ordering & verification
Vendors that use a "mode + parameters" register set expect a specific write order (params
first, **mode register last** commits). Some (Sungrow SH) require a **read-back verify**
after writing. AlphaESS commits on the mode register (`0x0885`) written last.

---

## 7. Canonical status model (`InverterState`)

Every `get_status()` normalises to this shape (superset; None where unsupported):

| Field | Meaning / convention |
|---|---|
| `status` | `unknown` / `online` / `offline` / `curtailed` / `error` |
| `is_curtailed` | bool — export/production currently limited by us |
| `power_output_w` | AC output |
| `power_limit_percent` | current export/production limit (100 = unrestricted) |
| `attributes{}` | extended canonical fields below |

**Extended canonical fields** (normalise every brand into these): `soc_pct`,
`battery_power_w` (**vendors disagree** — Sigenergy/sigenergy2mqtt use `+`=charge/`−`
=discharge; AlphaESS uses `−`=charge/`+`=discharge; Sungrow/GoodWe/Huawei differ again),
`battery_capacity_wh`, `soh_pct`, `pv_power_w`, `grid_power_w`, `load_power_w`,
`backup_reserve_pct`.

> **Sign-convention normalisation is mandatory. GridLens canonical (locked, matches our
> Sigenergy hardware):**
> - `battery_power_w > 0 = **charging**`, `< 0 = discharging`
> - `grid_power_w    > 0 = **importing**`, `< 0 = exporting`
> - `pv_power_w`, `load_power_w` ≥ 0
>
> Convert in each driver; never leak the vendor convention upward. (Implemented in
> `custom_components/grid_lens/inverters/base.py`.)

---

## 8. Per-brand appendix

Capability legend: **C** = curtail/export-limit only (solar); **B** = full battery
dispatch. "Source of truth" = where to derive the driver *legally*.

### Sigenergy — [Native Modbus] **B** · slave 247 (plant), auto-switch to 1 (inverter)
- **We already own this register map** in `custom_components/sigen/modbusregisterdefinitions.py` — use ours.
- Control (holding regs): `REMOTE_EMS_ENABLE 40029`, `REMOTE_EMS_CONTROL_MODE 40031`
  (0=PCS-remote, 1=STANDBY, 2=self-consumption, 3=charge-grid, 4=charge-PV,
  5=discharge-PV, 6=discharge-ESS), `ACTIVE_POWER_FIXED_TARGET 40001` (S32, kW×1000),
  `ESS_MAX_CHARGE_LIMIT 40032` / `DISCHARGE 40034`, `GRID_EXPORT_LIMIT 40038`,
  `BACKUP_SOC 40046`, `CHARGE_CUTOFF_SOC 40047`, `DISCHARGE_CUTOFF_SOC 40048`.
- Telemetry (input regs): `ESS_SOC 30014` (%×10), `ESS_POWER 30037`, `ACTIVE_POWER 30031`,
  `GRID_SENSOR_POWER 30005`, `ESS_RATED_CAPACITY 30083`.
- Sequences: §6.1–6.4 above. **Recommended transport: HA-entity proxy over our `sigen`.**
- Source of truth: our own integration + Sigenergy Modbus Protocol PDF.

### FoxESS — [Native Modbus | HA-entity proxy] **B**
- Modbus remote-control write to command charge/discharge; **gain varies by model**
  (standard kW×1000; **H3-Pro kW×10000**). Self-Use = normal.
- Proxy variant drives the `nathanmarlor/foxess_modbus` integration's
  `write_registers` service. `restore_work_mode_from_idle` returns to Self Use.
- Source of truth: FoxESS Modbus spec / `nathanmarlor/foxess_modbus` (permissive).

### Sungrow SH (hybrid) — [Native Modbus] **B**
- `EMS_MODE 13049` (0=self-consumption, 2=forced, 3=external EMS),
  `CHARGE_CMD 13050`, `CHARGE_DISCHARGE_POWER 13051` (W), `MIN_SOC 13058`/`MAX_SOC 13057`
  (%×10), `BACKUP_RESERVE 13099`, `EXPORT_LIMIT_SETTING 13073`,
  `EXPORT_LIMIT_ENABLED 13086` (0xAA on / 0x55 off), `MAX_CHARGE_POWER 33046`.
- force = forced mode + cmd + power, **read-back verify**; IDLE = Forced+Stop;
  disable stale zero-export before force-discharge.
- Source of truth: Sungrow SH Modbus spec / `mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant`.

### Sungrow SG (string) — [Native Modbus] **C** — curtail/export-limit only.

### AlphaESS — [Native Modbus] **B** · slave 85 (0x55)
- Dispatch block: `DISPATCH_START 0x0880` (1=start/0=stop),
  `DISPATCH_ACTIVE_POWER 0x0881` (U32, **raw = 32000 + watts×direction**),
  `DISPATCH_MODE 0x0885` (**write LAST to commit**), `DISPATCH_SOC 0x0886` (**%×2.5**),
  `DISPATCH_TIME 0x0887` (seconds; inverter auto-stops on elapse — natural deadman).
  Export %: `MAX_FEED_INTO_GRID_PERCENT 0x0800` (0=zero-export,100=unlimited).
- SOC read `0x0102` (%×10); battery power `0x0126` (S16, **−=charge/+=discharge**).

### Anker Solix — [Native Modbus] **B**
- `OPERATING_MODE 10064`, `BATTERY_POWER_SETPOINT 10071`, `EMS_MODE_MASK 32774`,
  SOC `10014`, battery power `10008`, rated energy `10250`.

### GoodWe — [Native Modbus (TCP) | HA-entity] **B** (ET/EH/BT/BH/ES/EM hybrids)
- Battery dispatch via **ECO_CHARGE / ECO_DISCHARGE** work modes; GENERAL = normal.
- Export limit: `EXPORT_LIMIT_ENABLED 47549`, `EXPORT_LIMIT 47550` (W).
- **DT/D-NS string models do NOT support export limiting** — treat as **C** or unmanaged.
- SOC `37007`, battery power `35182` (S32), work mode `35200`.
- Source of truth: `marcelblijleven/goodwe` (MIT).

### Huawei SUN2000 — [Native Modbus via Smart Dongle] **C** (solar export control)
- `ACTIVE_POWER_CONTROL_MODE 47415` (0=unlimited,5=zero-export,6=kW-limit,7=%-limit),
  `MAX_FEED_GRID_POWER_KW 47416` (I32×1000), `..._PCT 47418` (I16×10).
- SOC `37004` (%×10), battery power `37001` (S32), grid `37113` (S32, +export/−import).
- Source of truth: `wlcrs/huawei-solar-lib` (Apache-2.0). Model families L1/M0/M1/M2.

### Fronius (string, SunSpec) — [Native Modbus] **C**
- `WMAXLIMPCT 40232` (0–10000 = 0–100%), `WMAXLIMPCT_RVRT 40234` (revert timeout s),
  `WMAXLIM_ENA 40236` (1 on/0 off). SunSpec model at `40000`. **Needs installer
  password** for 0 W export limit on some units.
- Source of truth: SunSpec spec + `pysunspec2`.

### Fronius Reserva (GEN24 storage) — [HA-entity proxy] **B**
- force_charge/force_discharge/`set_idle` (zero PV-charge + discharge limits) /
  `restore_normal` (automatic control), all via the Fronius integration's entities.

### SolarEdge — [Native Modbus for curtail | HA-entity for battery] **C+B**
- Curtail: `ACTIVE_POWER_LIMIT 0xF001` → 100% on restore. Battery force
  charge/discharge/idle via HA control entities (StorageAC control), saving/restoring
  prior control state. Source of truth: SolarEdge Modbus app note.

### Solax — [Native Modbus | HA-entity proxy] **C+B**
- Curtail: `EXPORT_CONTROL_USER_LIMIT 0x42` (W, writable); factory limit `0xB5` (RO).
- Battery proxy variant: force charge/discharge with a **grid-export duration** select
  and an **auto-restore timer** after the forced window expires.

### Enphase — [REST: local Envoy + Enlighten cloud JWT] **C** (microinverters)
- Curtail via DPEL (**requires installer access**); JWT session for firmware 7.x+.
  Gateway models: Envoy / Envoy-S / IQ Gateway (metered variants).
- Source of truth: `pyenphase` (MIT).

### SAJ H2/HS2 — [REST/HA-entity] **B**
- force_charge via **TOU charge slot 7**; force_discharge via TOU + AppMode=1 discharge
  slot 7; IDLE = TOU hold; restore = Self-Use.

### Neovolt — [HA-entity proxy] **B**, incl. **fleet** (multi-inverter) variant
- Dispatch-mode entities; IDLE = raise discharge cutoff to SOC; **captures stable
  baseline dispatch modes before taking over** and restores them on handback. Fleet
  variant aggregates status and fans commands across child controllers.

### Zeversolar — [REST] **C** — curtail/restore only.

### Tesla Powerwall — [`powerwall_local/` package: local TEDAPI + Fleet API] **B**
- Separate subsystem (protobuf transport `tedapi`/`tesla_local`), not a plain Modbus
  driver. Prefers a **local gateway IP** when available, else Fleet API.
- Control model: `operation_mode` (`autonomous` / `self_consumption`) + `backup_reserve`
  as a hard SOC floor. IDLE requires `set_autonomous_mode()` **and** reserve — reserve
  alone is insufficient. Reserve reads carry provenance/trust (§3).
- Source of truth: `jrester/tesla_powerwall` / `teslapy` / Tesla Fleet API docs.

---

## 9. Recommended build order for GridLens

1. **Define the canonical contract** (§2, §7) as GridLens' own ABC + `InverterState`.
   Lock the **sign conventions** now.
2. **Sigenergy via HA-entity proxy** over our existing `sigen` integration (§5.3) —
   advisory mode first (no writes), then control behind a master switch + guardrails.
3. **BatteryController guardrails + ScheduleExecutor deadman** (§3, §4) — get the
   safety envelope right on one brand before adding more.
4. **Second brand of a different transport class** to prove the abstraction — e.g. a
   GoodWe (native Modbus, permissive upstream) or a HA-entity brand the abstraction can
   reuse.
5. Expand per §8, each driver headed with its permissive source-of-truth citation.

Curtail-only (**C**) brands give GridLens negative-price **export protection** even
without battery dispatch — a cheap early win for solar-only customers.

---

## 10. Open questions to resolve on real hardware

- Sigenergy: does the existing `sigen` integration re-assert its own EMS state on its
  poll cycle, fighting our writes? (Proxy vs native contention — verify before trusting.)
- Per brand: exact behaviour on transport dropout mid-forced-mode (hold last command vs
  auto-revert). Auto-revert-on-timer brands (AlphaESS, Solax) are safest.
- DNSP export limits per NMI (already surfaced in GridLens demand-charge config).
