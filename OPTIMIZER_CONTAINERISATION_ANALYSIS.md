# Optimizer logging & containerisation analysis

**Date:** 2026-09-03
**Status:** Analysis / decision record — no code changed
**Trigger:** "We're logging everything to the *default* Home Assistant log stream. Can we
log to our own instead? Other projects are selectable under Settings → System → Logs →
top-right drop-down."

---

## TL;DR

- That drop-down lists **add-ons and Supervisor components** — each a separate container
  with its own stdout stream. A **HA integration** (which is what `grid_lens` is) runs
  *inside* the HA Core process and always logs under "Home Assistant Core". It cannot get
  its own drop-down entry without being split into a separate add-on container.
- The noisy lines
  (`custom_components.grid_lens.battery_optimizer` — daily-target drop, export-floor hours,
  conditional credits, cap blocks) are **per-run diagnostics emitted at `WARNING`**. The
  proportionate fixes are (1) move them to `.debug()`, and/or (2) attach a dedicated
  `RotatingFileHandler` so grid_lens gets its own `grid_lens.log` file — no architecture
  change, works on every install type.
- Shipping the optimizer as its own container is a large, userbase-shrinking change. Keep
  it on the shelf for an *independent* reason (solver-stack install pain, independent
  scheduling), not for log routing.

---

## 1. Why the Logs drop-down can't help an integration

Settings → System → Logs, top-right selector, lists log **sources** that are separate
processes/containers:

- Home Assistant Core
- Supervisor, Host, Multicast, Audio, DNS, CLI
- **Each add-on**, by name (Claude Code, TimescaleDB, EMHASS, …)

Every one of those is its own container (or the host journal), with its own stdout that the
Supervisor tails.

`grid_lens` is a **HACS integration** — `manifest.json` has `integration_type: "service"`,
`single_config_entry: true`. It is Python imported into the HA Core process:
`logging.getLogger("custom_components.grid_lens.…")`. Its records propagate to HA's root
logger and land in **"Home Assistant Core"**. There is no mechanism for an in-core module
to appear as a distinct source in that selector.

### What you *can* do without a container

| Option | Effect | Cost |
|---|---|---|
| **A. Dedicated file handler** | Attach a `RotatingFileHandler` to the `custom_components.grid_lens` logger → writes `/homeassistant/grid_lens.log`. Set `logger.propagate = False` to *also* remove it from the Core log, or leave it on to have both. | ~15 lines in `__init__.py`; open the file via `hass.async_add_executor_job` (blocking I/O). Works on **every** install type. |
| **B. Fix the levels** | The chatty lines are routine per-solve diagnostics mislabelled `WARNING`. Move them to `_LOGGER.debug()` in `battery_optimizer.py`; document a `logger:` recipe in `grid-lens/DOCS.md` for users who want them back. | Small edit + doc. This is the real problem — the log is loud because the levels are wrong, not because routing is wrong. |

Recommended: **B, then A if a separate stream is still wanted.**

#### Option A sketch

```python
# custom_components/grid_lens/__init__.py
import logging
from logging.handlers import RotatingFileHandler

async def _setup_gridlens_logging(hass):
    logger = logging.getLogger("custom_components.grid_lens")
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return

    def _make_handler():
        h = RotatingFileHandler(
            hass.config.path("grid_lens.log"),
            maxBytes=2_000_000, backupCount=3,
        )
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
        return h

    logger.addHandler(await hass.async_add_executor_job(_make_handler))
    # logger.propagate = False   # uncomment to divert OUT of the Core log entirely
```

Call from `async_setup`. User then `tail -f /homeassistant/grid_lens.log`.

---

## 2. What "ship in our own container" actually means

> Note: the existing `grid-lens/` folder is **not** a starting point. Its `config.yaml` is a
> stale, half-finished attempt to package the **cloud API** as a local add-on — Stripe keys,
> `services: [mysql]`, `timescaledb_url`, `ghcr.io/yourusername/…` placeholders, no
> Dockerfile, no `rootfs/`. It has nothing to do with the in-core optimizer.

An HA integration is Python imported into the HA Core process. It cannot move to another
container. "Own container" therefore means **splitting Grid Lens in two**:

| Piece | Runs in | Contains | ~Lines today |
|---|---|---|---|
| **Integration** | HA Core (unchanged) | config flow; all entity platforms (`sensor`/`switch`/`number`/`select`); reading `hass.states` (battery SOC, price sensors, deferrable-load entities); writing the `*_planned_dispatch` sensor + `trajectory` attribute; dashboard seed (`_build_seed_views`); Lovelace card resource registration | ~12k of 14k |
| **Add-on** | new container | the LP model build + solve in `battery_optimizer.py`; its `scipy` / `pulp` / `highspy` / `numpy` deps; optionally the bill math in `plan_calculator.py` (3027 lines) | `battery_optimizer.py` = 1467 |

`battery_optimizer.py` is where the log noise originates and where the CPU cost is
(`scipy.optimize.linprog` / `milp`, `pulp`) — so it is the natural thing to move.

### Communication

Internal Docker network — Core reaches an add-on at `http://<slug>:<port>`:

1. Integration gathers inputs from `hass.states` → `POST http://grid_lens:8000/optimize`
   with a JSON payload (prices, SOC, deferrable-load windows, plan data).
2. Add-on solves, returns trajectory JSON.
3. Integration writes it onto its sensors.
4. **Add-on logs solve detail to its own stdout → its own entry in Settings → System →
   Logs.** ← the only way to get the drop-down entry.

### Build work

- Real `Dockerfile` — `FROM ghcr.io/home-assistant/{arch}-base-python:3.12`, `pip install`
  the solver stack, copy app.
- Entrypoint (`startup: application` + a `run` script, or s6 `rootfs/etc/services.d/…`).
- `config.yaml` rewritten from scratch — drop the Stripe/mysql/timescale cruft; add
  `homeassistant_api` / `hassio_api` / `map` only as needed; declare `ports` or rely on the
  internal network.
- `icon.png` / `logo.png`, `CHANGELOG.md`, `DOCS.md`.
- Small HTTP server in the add-on (aiohttp / FastAPI): `/optimize` + `/health`; `/health`
  carries a **payload-schema version** for negotiation.
- Refactor the solve path so it takes a plain dict / dataclass **in** and returns one
  **out**, with no `hass` references inside — audit what it touches today.
- Integration gains an HTTP client path **plus a fallback** for when the add-on is not
  installed/reachable (either keep the solver in-process too — now maintained twice — or
  degrade gracefully).
- Multi-arch image builds in CI (amd64 / aarch64 / armv7) → GHCR; real `image:` line in
  `config.yaml`.

### Distribution / UX costs

- **Add-ons only exist on HA OS and Supervised installs.** HA Container (plain Docker) and
  HA Core (venv) installs cannot install add-ons *at all*. The integration alone works
  everywhere; splitting strands those users unless the in-process path is kept.
- Onboarding goes from "one HACS click" to "HACS integration **+** add a second add-on repo
  **+** install the add-on **+** keep both versions in lockstep."
- **Two release trains** (HACS + add-on store) that must agree on the wire schema. A user
  updating one and not the other is a new failure mode → hence the `/health` schema
  version.

### Ongoing costs

- Serialising 72h trajectories + plan tables over HTTP every solve; round-trip latency.
- Anything in the moved code that reads `hass` must have that data passed in explicitly, or
  call back via the Core API with a token.
- Debugging now spans two containers.

---

## 3. Recommendation

For the stated goal — get the optimizer chatter out of the Core log — the container split
is disproportionate and shrinks the addressable userbase.

1. **Fix the levels.** Move the per-run diagnostic lines in `battery_optimizer.py`
   (daily-target drop, `min export price floor`, `conditional credits`, `cap block
   import/export`) from `_LOGGER.warning()` to `_LOGGER.debug()`. Add a `logger:` recipe to
   `grid-lens/DOCS.md`.
2. **Optionally add the dedicated file handler** (§1 Option A) → `grid_lens.log`,
   `propagate=False`, for users who want a separate stream to tail.

Revisit the add-on split only when there is an **independent** driver:

- `scipy` / `highspy` becoming painful to install in the Core venv on some architecture
  (this has bitten other integrations with native deps).
- Wanting the optimizer to run on its own schedule, decoupled from HA restarts / reloads.
- The solve growing heavy enough that running it in the Core event loop's executor pool is
  a problem for the rest of HA.

None of those is the case today.

---

## Appendix — repo facts referenced

- `custom_components/grid_lens/manifest.json`:
  `requirements: ["requests>=2.31.0", "pulp>=2.7.0", "highspy>=1.7.0", "scipy>=1.9.0"]`,
  `integration_type: "service"`, `single_config_entry: true`, `dependencies: ["recorder"]`.
- `battery_optimizer.py`: 1467 lines; imports `numpy`, `scipy.optimize.linprog` /
  `scipy.optimize.milp`, `scipy.sparse`, `pulp`.
- `plan_calculator.py`: 3027 lines (bill math). `__init__.py`: 2127. `config_flow.py`: 1963.
- `grid-lens/` add-on skeleton: `config.yaml` + `build.yaml` + `DOCS.md` only; targets the
  cloud API, not the optimizer; unfinished (placeholder URLs, no Dockerfile).
