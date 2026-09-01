# Digital Twin Reference

Single source of truth for the satellite digital twin used in the autonomous mission-ops hackathon project. This file is **cumulative** — every implementation step appends to it. Future agents and teammates should read this first before touching any twin code.

## Contents

1. [Problem context](#1-problem-context)
2. [Decisions log](#2-decisions-log)
3. [Data contract: what flows in and out of the twin](#3-data-contract)
4. [Subsystem architecture](#4-subsystem-architecture)
5. [Where the physics comes from](#5-where-the-physics-comes-from)
6. [What we inherit from CHESS and PASEOS](#6-what-we-inherit)
7. [What we build ourselves](#7-what-we-build-ourselves)
8. [Fault injection contract](#8-fault-injection-contract)
9. [Integration with other agents](#9-integration-with-other-agents)
10. [Build log](#10-build-log)

---

## 1. Problem context

We're building an agentic mission-ops layer for satellites. The detect/diagnose/propose/validate/approve/execute/verify loop needs a **digital twin that can:**

1. Simulate the satellite physics well enough that recovery actions can be validated **before** they're uplinked.
2. Produce telemetry streams in a format that the anomaly detector (NASA Telemanom, an LSTM autoencoder) can score.
3. Support reproducible **fault injection** so the demo is reliable and the audit trail is intact.
4. Be **fast** — the validation step runs the sim forward 4 hours per candidate action plan, and the recovery planner may try several plans.

The verification step (predicted outcome vs actual outcome) only works if the twin's predictions are within the right **order of magnitude and sign**. We do not need engineering-grade accuracy; we need physically defensible equations with parameters that can be tuned by sensitivity testing.

---

## 2. Decisions log

| # | Decision | Rationale | Date |
|---|---|---|---|
| D1 | **Hybrid approach: CHESS `digital_twin_CubeSat` as base + PASEOS's thermal equation + custom EPS/ADCS + custom fault injection** | CHESS gives us the simulation loop, mode logic, orbit propagator, and real CubeSat parameters; PASEOS gives us the one piece of real physics (thermal) that CHESS lacks. Writing everything from scratch would have been ~2× the code. | 2026-08-30 |
| D2 | **CHESS cloned at `digital_twin_CubeSat/`, PASEOS at `paseos/`, Telemanom at `telemanom/`** (all `--depth 1`) | We only need the code, not the full git history. Shallow clones keep the tree small. | 2026-08-30 |
| D3 | **Telemanom as the detection layer, not retrained on our data** | The NASA Telemanom was trained on SMAP/MSL telemetry. We mirror their channel structure and prefix convention so the pretrained model can score our simulated anomalies without retraining. Retraining on synthetic data is a 6+ hour GPU job we cannot afford in a hackathon. | 2026-08-30 |
| D4 | **Output format: per-channel `.npy` files matching SMAP/MSL shape `(n_timesteps, n_features)`** | Native format for Telemanom. No translation layer in the detection pipeline. | 2026-08-30 |
| D5 | **CHESS's EPS is bookkeeping, not physics — we replace it** | CHESS's `Eps.update_batteries()` is `battery_level -= power_consumed * dt; battery_level += power_generated * dt`. No voltage, no internal resistance, no thermal coupling. We need a terminal-voltage model with `internal_r` as the fault-injection handle. | 2026-08-30 |
| D6 | **CHESS's ADCS is a stub — we replace it** | The source literally says "Not implemented yet for this subsystem" in the safe-flag handling. No pointing error, no wheel speeds, no controller. We add a 2-state dynamic model. | 2026-08-30 |
| D7 | **CHESS has no thermal — we add multi-node lumped RC** | Single-node lumped model (PASEOS's equation) is sufficient for one temperature, but our cascade demo needs battery/payload/electronics to diverge. 4-node RC network with inter-node conductances. | 2026-08-30 |
| D8 | **Fault injection is its own module with an audit trail** | Both CHESS and PASEOS lack fault injection. The runbook agent needs to know which faults were applied, when, and with what parameters. `FaultScheduler.history` is the audit log. | 2026-08-30 |
| D9 | **We do not use Basilisk, GMAT, STK, or any high-fidelity commercial simulator** | Integration overhead is too high for a 48h build. Lumped models tuned by sensitivity testing are accurate enough for our verification step. | 2026-08-30 |
| D10 | **Hybrid license: CHESS is open (permissive-ish, single-developer EPFL), PASEOS is GPL** | We use CHESS's loop and parameters (no code copy beyond what we're already doing), and PASEOS's *equation* (math, not copyrightable). The new code we write is our own and we choose the license. The hybrid itself is not distributed as a single work, so GPL contamination is not a concern for a hackathon submission. **If this project ever ships to production, revisit D10 with a real IP review.** | 2026-08-30 |

---

## 3. Data contract

### 3.1 Output to the agent system (per simulation run)

The twin produces a `SimulationOutput` object. Arrays whose name is marked **(CHESS-native)** come from CHESS's existing loop with no changes; the rest are added by our new modules.

```
SimulationOutput
├── per_step_arrays: dict[str, np.ndarray]    # shape (n_timesteps+1,) unless noted
│   ├── timestamp_s               # sim time in seconds from epoch
│   │
│   # --- EPS ---
│   ├── battery_energy_ws           # (CHESS-native) watt-seconds; SoC = this / 274752.0
│   ├── battery_voltage_v           # NEW — derived from V_oc(SoC) - internal_r(T) * load
│   ├── battery_soc                 # NEW — normalized battery_energy_ws / capacity_ws
│   ├── battery_internal_r_ohm      # NEW — temperature-dependent
│   ├── battery_temp_c              # NEW — from thermal node "battery"
│   ├── solar_panel_current_a       # NEW
│   ├── solar_panel_power_w         # (CHESS-native as power_generation, repackaged)
│   │
│   # --- Thermal ---
│   ├── payload_temp_c              # NEW
│   ├── electronics_temp_c          # NEW
│   ├── radiator_temp_c             # NEW
│   ├── heater_duty_battery         # NEW — 0..1, actionable by recovery planner
│   ├── heater_duty_payload
│   ├── heater_duty_electronics
│   │
│   # --- ADCS ---
│   ├── pointing_error_deg          # NEW
│   ├── pointing_rate_deg_s         # NEW
│   ├── wheel_speed_x_rpm           # NEW
│   ├── wheel_speed_y_rpm
│   ├── wheel_speed_z_rpm
│   ├── attitude_mode               # (CHESS-native, same int)
│   │
│   # --- Orbit / Comms ---
│   ├── in_eclipse                  # (CHESS-native as eclipse_windows)
│   ├── link_margin_db              # NEW — derived
│   ├── elevation_to_gs_deg         # NEW — derived
│   ├── comm_window                 # (CHESS-native as vis_windows flattened)
│   │
│   # --- Mode ---
│   ├── operating_mode              # (CHESS-native as modes) int 0..5
│
├── channels: dict[str, np.ndarray]          # Telemanom-shaped, shape (n_timesteps, n_features)
│   ├── "P-1": battery voltage      # cols: [voltage, soc, internal_r, eclipse]
│   ├── "P-2": solar current        # cols: [current, sun_angle, eclipse, mode]
│   ├── "B-1": battery temp         # cols: [temp, heater_duty, soc, mode]
│   ├── "T-1": payload temp         # cols: [temp, heater_duty, mode, eclipse]
│   ├── "T-2": electronics temp     # cols: [temp, heater_duty, mode, eclipse]
│   ├── "A-1": pointing error       # cols: [err, rate, mode, eclipse]
│   ├── "G-1": wheel speed          # cols: [wx, wy, wz, mode]
│   ├── "D-1": link margin          # cols: [margin, elev, err, comm_window]
│   └── ... (extend as needed)
│
├── fault_log: list[FaultRecord]              # every fault applied, with timestamps
│
├── run_metadata: dict
│   ├── duration_s
│   ├── n_timesteps
│   ├── dt_s
│   ├── config_hash
│   └── physics_versions: {"thermal": "...", "adcs": "...", "eps": "..."}
│
└── validation_result: Optional[ValidationResult]   # populated when validate() is called
    ├── feasible: bool
    ├── violations: list[ConstraintViolation]
    ├── risk_score: float
    ├── predicted_trajectory: np.ndarray    # shape (n_pred_steps, n_channels)
    ├── baseline_trajectory: np.ndarray
    └── summary: str
```

### 3.2 Input to the twin (configuration)

A single JSON or dict:

```
{
  "spacecraft": { ... },         # mass, drag coeff, init mode (CHESS template)
  "orbit": { ... },              # TLE or classical elements (CHESS template)
  "ground_stations": [ ... ],    # CHESS template
  "simulation": { ... },         # dt, duration, atmosphere model
  "thermal": {
    "nodes": {
      "battery":  { "mass_kg": 1.2, "thermal_capacity_j_kg_k": 1000, "sun_facing_area_m2": 0.005, "emissive_area_m2": 0.02, "infrared_absorptance": 0.3, "init_temp_k": 293 },
      "payload":  { ... },
      "electronics": { ... },
      "radiator": { ... }
    },
    "conductances_w_k": {
      "battery-payload": 0.5,
      "payload-electronics": 0.4,
      "electronics-radiator": 0.8,
      "battery-radiator": 0.1
    },
    "heaters_w": { "battery": 5.0, "payload": 0, "electronics": 3.0 }
  },
  "eps": {
    "capacity_wh": 76.32,
    "internal_r_nominal_ohm": 0.05,
    "internal_r_temp_coeff": 0.01,         # d(ln r)/dT
    "solar_cells": 28,
    "cell_area_m2": 0.0028355,
    "cell_efficiency": 0.25,
    "consumption_w": { "0": 0.24, "1": 0.24, ... }
  },
  "adcs": {
    "inertia_kg_m2": 0.05,
    "controller_kp": 0.1,
    "controller_kd": 0.5,
    "disturbance_torque_nm": 1e-5,
    "wheel_saturation_rpm": 6000,
    "wheel_inertia_kg_m2": 1e-5
  },
  "fault_schedule": [
    { "type": "eps_internal_r_ramp", "start_t": 3600, "magnitude": 0.4, "duration_s": 1800 },
    { "type": "thermal_heater_stuck_off", "start_t": 7200, "node": "battery" }
  ]
}
```

### 3.3 Channel format (Telemanom input)

Each channel is a `.npy` file with shape `(n_timesteps, n_features)`:

- **Column 0**: the channel's own value (the thing being predicted)
- **Columns 1..N**: same-channel context (eclipse flag, mode, other correlated channels)
- **Pre-scaled to `(-1, 1)`** by min/max of the test set (Telemanom requirement)
- **Channel IDs** use SMAP/MSL prefix convention: P- (power), B- (battery), T- (thermal), A- (actuator/attitude), G- (gyro), D- (data/downlink), E- (electronic)

Files written to `data/train/<channel>.npy` and `data/test/<channel>.npy` for Telemanom's `Channel.load_data()`.

---

## 4. Subsystem architecture

```
                   ┌──────────────────────────┐
                   │  Orbit Propagator (CHESS)│
                   │  - Position/velocity     │
                   │  - Eclipse flag          │
                   │  - Atmosphere density    │
                   │  - Visibility windows    │
                   └──────────┬───────────────┘
                              │ rv, eclipse, vis
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                       SIMULATION LOOP                          │
│                                                                │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────┐  │
│  │ EPS        │←→│ Thermal    │←→│ ADCS       │←→│ Telecom  │  │
│  │ (custom)   │  │ (4-node RC)│  │ (custom)   │  │ (CHESS)  │  │
│  │            │  │            │  │            │  │          │  │
│  │ internal_r │  │ battery T  │  │ pointing_  │  │ link     │  │
│  │ ↑↓ T       │  │ payload T  │  │ error,     │  │ margin   │  │
│  │ voltage    │  │ elect T    │  │ wheel rpm  │  │ windows  │  │
│  │ SoC        │  │ radiator T │  │ mode       │  │          │  │
│  └────────────┘  └────────────┘  └────────────┘  └──────────┘  │
│        ↑               ↑              ↑              ↑         │
│        └───────────────┴──────────────┴──────────────┘         │
│                              │                                  │
│                              ▼                                  │
│                   ┌──────────────────────────┐                 │
│                   │  Fault Scheduler         │                 │
│                   │  - injects at scheduled t│                 │
│                   │  - logs to history        │                 │
│                   └──────────┬───────────────┘                 │
│                              │                                  │
└──────────────────────────────┼──────────────────────────────────┘
                               ▼
                   ┌──────────────────────────┐
                   │  Channel Shaper          │
                   │  - per-channel .npy     │
                   │  - Telemanom shape       │
                   │  - (-1, 1) scaling       │
                   └──────────┬───────────────┘
                              │
                              ▼
                   ┌──────────────────────────┐
                   │  SimulationOutput        │
                   │  - per_step_arrays       │
                   │  - channels              │
                   │  - fault_log             │
                   │  - validation_result     │
                   └──────────────────────────┘
```

### Coupling graph (causal)

```
solar_input ─→ battery_charge_current
                  │
                  ▼
              battery_soc ─→ battery_voltage
                  │                │
                  │                ▼
                  │         battery_power_delivered
                  │                │
                  ▼                ▼
         battery_temp ──→ internal_r(T)  ←── heater_duty_battery
              (node)             │              (action)
                                  ▼
                          voltage_droop
                                  │
                                  ▼
                          all_subsystem_power ←── pointing_error → solar_input
                                  │                                  (cos θ penalty)
                                  ▼
                                  ├──→ payload_temp
                                  └──→ electronics_temp
                                            │
                                            ▼
                                       radiator_temp
                                            │
                                            ▼
                                       (radiated to space)
```

---

## 5. Where the physics comes from

### 5.1 EPS — terminal voltage + SoC bookkeeping

**Coulomb counting (SoC):**

```
SoC(t+dt) = SoC(t) - (load_current - charge_current) * dt / (capacity_ah * 3600)
```

**Terminal voltage (Thevenin equivalent, simplified):**

```
V_oc(SoC) = V_nominal + V_slope * (SoC - 0.5)
V_terminal = V_oc(SoC) - internal_r(T) * load_current
```

**Internal resistance temperature dependence:**

```
internal_r(T) = r_nominal * (1 + alpha * (T - T_ref))
```

Where `alpha ≈ 0.01 /K` is a typical Li-ion coefficient. This is the **fault-injection handle**: ramping `alpha` or adding a constant to `internal_r` simulates cell degradation.

**Solar panel current:**

```
P_solar = n_cells * cell_area * solar_flux * efficiency * cos(θ_incident)
I_solar = P_solar / V_bus  (clipped to charge rate limit)
```

The `cos(θ_incident)` term couples ADCS to EPS — pointing off-sun reduces charging. This is real CHESS logic (see `SolarPanel.get_effective_surface()` in their code).

### 5.2 Thermal — multi-node lumped RC (PASEOS equation, extended)

**PASEOS's single-node equation (per node, our extension):**

```
Q_total = Q_solar + Q_albedo + Q_IR - Q_emission + heater_power + sum_neighbors(Q_nm)

where:
  Q_solar = α_sun * A_sun * solar_irradiance * (1 - eclipse)
  Q_albedo = α_sun * A_body * body_reflectance * solar_irradiance * 0.5 * (1 - eclipse)
  Q_IR = α_IR * ε_body * A_body * σ * T_body^4 * (1 / (h/R_body)^2)
  Q_emission = α_IR * A_emit * σ * T^4
  Q_nm = G_nm * (T_m - T_n)     # RC coupling between nodes

T(t+dt) = T(t) + (dt * Q_total) / (mass * thermal_capacity)
```

Constants (PASEOS defaults, our inherited):
- `solar_irradiance = 1360 W/m²`
- `body_surface_temp = 288 K`
- `body_emissivity = 0.6`
- `body_reflectance = 0.3`
- `σ = 5.670374419e-8 W/m²/K⁴` (Stefan-Boltzmann)

**Multi-node extension (our addition):** 4 nodes (battery, payload, electronics, radiator), each as a separate instance of PASEOS's equation with shared conductances `G_nm` between adjacent nodes. The radiator node is the primary heat sink to space.

### 5.3 ADCS — 2nd-order damped pointing + wheel dynamics

**Pointing error (2nd-order ODE):**

```
I * θ_ddot = τ_control - τ_disturbance
τ_control = k_p * (θ_target - θ) - k_d * θ_dot
τ_disturbance = constant (gravity gradient + aerodynamic drag)
```

**Reaction wheel dynamics:**

```
ω_wheel(t+dt) = ω_wheel(t) + dt * τ_control / I_wheel
ω_wheel = clip(ω_wheel, -ω_sat, +ω_sat)   # saturation at 6000 rpm
```

This is the simplest physically meaningful model. No quaternions (single-axis scalar pointing error), no momentum exchange between wheels, no magnetorquers. Enough to show: star tracker fault → error drifts → wheels saturate.

### 5.4 Orbit / environment — CHESS propagator (untouched)

- `OrbitPropagator` from CHESS with NRLMSISE-00 / JB2008 atmosphere models
- Outputs: `r_earth_sat`, `r_earth_sun`, eclipse status, ground-station visibility, atmospheric density
- `propagate(dt, C_D, A_over_m)` advances state
- `calculate_eclipse_status()` returns `(bool, r_earth_sun)`
- `calculate_vis_window(ground_stations)` returns visibility per station

### 5.5 Comms — derived (not simulated)

```
link_margin_db = base_margin - path_loss(elevation) - pointing_penalty(pointing_error)

where:
  base_margin = 10 dB (configurable)
  path_loss at 90° elev ≈ 165 dB, at 5° elev ≈ 175 dB (UHF, 400 MHz, simple model)
  pointing_penalty = 20 * log10(1 + (pointing_error / beamwidth))
```

No need to import anything; ~20 lines of code.

---

## 6. What we inherit from CHESS

| Module | Use it? | Notes |
|---|---|---|
| `orbit_propagator/` | **Yes, unchanged** | Real NRLMSISE-00 / JB2008 atmosphere, eclipse calc, visibility windows |
| `simulation.py` (main loop) | **Yes, modify in place** | Keep loop structure, add calls to our thermal/ADCS/EPS/fault modules |
| `spacecraft/spacecraft.py` | **Yes, modify in place** | Add thermal_state and adcs_state; call our modules in `update_subsystems()` |
| `spacecraft/eps/eps.py` | **No, replace** | Bookkeeping only; we add physical model |
| `spacecraft/adcs/adcs.py` | **No, replace** | Stub; we add dynamic model |
| `spacecraft/telecom/telecom.py` | **Yes, mostly unchanged** | Keep for visibility-window logic; comms are derived |
| `spacecraft/payload/payload.py` | **Yes, mostly unchanged** | Bookkeeping only; not our focus |
| `spacecraft/obc/obc.py` | **Yes, unchanged** | Data storage and housekeeping |
| `mode_switch.py` | **Yes, mostly unchanged** | Mode logic is reasonable; add a few triggers for our faults |
| `report.py`, `plotting.py` | **Yes, extend** | Add per-channel plots, fault timeline |
| `data/spacecraft/spacecraft_template.json` | **Yes, extend** | Add thermal/ADCS/EPS-physics sections |
| `data/orbit/`, `data/ground_station/`, `data/simulation/`, `data/mission_design/` | **Yes, unchanged** | Standard CHESS inputs |
| `digital_twin_env.yml` | **Yes, extend** | Add our dependencies (numpy already there, scipy is in CHESS) |

## 7. What we build ourselves

Six new files, total ~600–800 lines:

| File | Lines | Purpose |
|---|---|---|
| `twin/thermal_node.py` | ~120 | Multi-node lumped RC, ported from PASEOS equation |
| `twin/adcs_dynamics.py` | ~80 | 2nd-order damped pointing + wheel dynamics |
| `twin/eps_physical.py` | ~100 | Terminal voltage + SoC + internal_r(T) |
| `twin/fault_injection.py` | ~100 | FaultScheduler with audit trail |
| `twin/channel_shaper.py` | ~80 | Per-channel .npy in Telemanom format |
| `twin/run_sim.py` | ~50 | Orchestrator wrapping the modified CHESS loop |

Plus modifications to existing CHESS files:
- `simulation.py`: call our modules per step, build the channel shaper at the end
- `spacecraft/spacecraft.py`: add thermal_state and adcs_state attributes; route to our modules
- `data/spacecraft/spacecraft_template.json`: add thermal/ADCS/EPS sections

---

## 8. Fault injection contract

### 8.1 Fault types

| Fault ID | Subsystem | Effect | Parameters |
|---|---|---|---|
| `eps_internal_r_ramp` | EPS | Linearly increases `internal_r` over duration | `start_t`, `magnitude_ohm`, `duration_s` |
| `eps_load_step` | EPS | Adds constant load current | `start_t`, `extra_load_a`, `duration_s` (or None = permanent) |
| `thermal_heater_stuck_off` | Thermal | Sets heater duty to 0 regardless of action | `start_t`, `node ∈ {battery, payload, electronics}` |
| `thermal_heater_stuck_on` | Thermal | Sets heater duty to 1.0 | `start_t`, `node` |
| `adcs_star_tracker_lost` | ADCS | Pointing error drifts as if estimator failed | `start_t`, `drift_rate_deg_s`, `duration_s` |
| `solar_degradation` | EPS | Multiplies solar power by factor | `start_t`, `factor` (e.g., 0.5), `duration_s` |
| `comm_ground_station_lost` | Comms | Removes a ground station from the visibility table | `start_t`, `gs_id`, `duration_s` |

### 8.2 API

```python
scheduler = FaultScheduler()

# Schedule a fault
fault_id = scheduler.inject(
    fault_type="eps_internal_r_ramp",
    start_t=3600,             # sim time in seconds
    magnitude_ohm=0.4,
    duration_s=1800,
)

# Inside the simulation loop:
def step(t, state):
    # Apply active faults before physics step
    active_faults = scheduler.active_at(t)
    for f in active_faults:
        f.mutate(state.thermal, state.adcs, state.eps)
    # ... normal physics step

# At the end:
audit_log = scheduler.history  # list of FaultRecord
```

### 8.3 FaultRecord (audit trail)

```python
@dataclass
class FaultRecord:
    fault_id: str            # e.g., "F-001"
    fault_type: str          # e.g., "eps_internal_r_ramp"
    start_t: float
    end_t: Optional[float]
    parameters: dict
    applied_at: float        # actual sim time when first applied
    resolved_at: Optional[float]
```

### 8.4 Cause enum (diagnostic agent output)

The `Cause` enum in `twin/procedures.py` is the vocabulary the diagnostic agent
is allowed to emit. Each cause name is exactly a `fault_type` from the table above
so the runbook can verify "we suspected X and we were right." See `twin/procedures.py`
for the full list. The enum is the single source of truth; do not add new causes
without also adding a corresponding fault_type in the FaultScheduler.

---

## 9. Integration with other agents

| Agent layer | Reads from twin | Writes to twin |
|---|---|---|
| **Detector (Telemanom)** | `channels/*.npy` (per-channel streams) | nothing |
| **Diagnostic (LLM)** | `channels` + `per_step_arrays` (current snapshot) | `Cause` enum (one of `twin.procedures.Cause`) |
| **Recovery planner (first layer)** | `Cause` + `Procedure` registry from `twin.procedures` | ranked list of `{procedure, params, confidence}` |
| **Validation** | current sim state + ActionPlan | `ValidationResult` with predicted trajectory |
| **Approval (human)** | ValidationResult (read-only in UI) | approved ActionPlan |
| **Execution** | approved ActionPlan (via `apply_procedure`) | mutates sim state to apply the plan |
| **Verification** | `per_step_arrays` (post-action) + ValidationResult.predicted_trajectory | bool (verified/failed) + metrics |
| **Runbook** | full audit trail: fault_log + actions + ValidationResult + verification result | markdown runbook artifact |

### 9.1 Shared procedure vocabulary (the contract)

`twin/procedures.py` is the single source of truth for:
- `Cause` enum — what the diagnostic agent can diagnose (13 values)
- `Procedure` enum — what the recovery planner can recommend (9 values)
- `PROCEDURE_REGISTRY` — per-procedure params_schema, preconditions, postconditions, apply_fn
- `apply_procedure(state, procedure, params)` — the ONE place that mutates state

Diagnostic agent outputs must use `Cause` values exactly. The recovery planner
emits one of the `Procedure` values with validated params. Both import from
`twin.procedures` so they cannot drift.

### 9.2 Message schema (action vocabulary)

The recovery planner emits `ActionPlan` objects. The twin consumes them via `apply_procedure()`:

```python
from twin.procedures import Procedure, apply_procedure

# Apply a recommended procedure to the current state
apply_procedure(state, Procedure.EPS_SHED_LOAD, {
    "load_reduction_a": 1.0,
    "duration_s": 3600,
})
```

`apply_procedure()` validates the procedure is in the registry, validates the
params against the schema, checks preconditions, and only then mutates state.
Raises `ValueError` on any failure (which the caller should treat as "this
procedure is not applicable" and rank it lower).

---

---

## 10. Build log

Each implementation step appends here. Format: `### Step N: <title> (date) — <status>`.

### Step 0: Repository survey (2026-08-30) — done
- Cloned `digital_twin_CubeSat`, `paseos`, `telemanom` (all `--depth 1`)
- Read EPS, ADCS, simulation loop in CHESS
- Read thermal_model.py in PASEOS
- Read channel.py, modeling.py, detector.py in Telemanom
- Wrote decisions D1–D10 above
- Wrote this reference document

### Step 1: Get CHESS base running (2026-08-30) — done

**What we did:**
- Used `uv` (no conda available) to create a Python 3.10 venv at `digital_twin_CubeSat/.venv`
- Installed CHESS's runtime deps. Hit four real-world version pinning issues — recorded below so future agents don't waste time
- Created smoke-test configs (5s timestep, 600s duration) so a full run takes ~1s
- Ran end-to-end and inspected the per-step arrays

**Real-world pin issues encountered (must reproduce exactly):**

1. `poliastro>=0.18` removed `Earth.J2`. Pin to `poliastro==0.17.0` (which pulls `astropy==5.3.4`, `numpy==1.26.4`).
2. `pyatmos` (a CHESS dep) imports `pkg_resources`, removed from `setuptools>=81`. Pin `setuptools<80`.
3. `kaleido==1.x` is **incompatible with plotly 5.24** (it changed the engine API). Pin `kaleido==0.2.1` to match plotly.
4. CHESS's `get_astropy_unit_time()` accepts only `{"second", "hour", "day", "year"}` — **not** `"minute"`. Use `second` and pass `duration_sim` in seconds, or open `src/digital_twin/utils.py:18` and add `"minute": u.min`. We chose the latter (see step 1.5 below).

**Concrete one-liner to install all deps:**
```bash
cd digital_twin_CubeSat
uv venv --python 3.10 .venv
source .venv/bin/activate
uv pip install "poliastro==0.17.0" "kaleido==0.2.1" "setuptools<80" \
    numpy scipy astropy==5.3.4 astroquery plotly==5.24.1 \
    pandas pyyaml numba matplotlib pyatmos wget
```

**Confirmed output structure (from per-step tap of the main loop):**

| Array | Shape | Dtype | Source in CHESS |
|---|---|---|---|
| `battery_energies` | `(n+1,)` | float64 Ws | `simulation.py:227` — `Eps.get_battery_energy().value` |
| `power_consumption` | `(n+1,)` | float64 W | `simulation.py:229` — `Eps.get_power_consumption().value` |
| `power_generation` | `(n+1,)` | float64 W | `simulation.py:230` — `Eps.get_power_generation().value` |
| `eclipse_windows` | `(n+1,)` | int | `simulation.py:226` — `int(eclipse_status)` |
| `vis_windows` | `(n+1, n_ground_stations)` | int | `simulation.py:225` |
| `modes` | `(n+1,)` | int (0..5) | `simulation.py:210` |
| `eph` | `(n+1, 6)` | float64 km, km/s | `simulation.py:166` — `[x, y, z, vx, vy, vz]` |

**Smoke test (600s @ 5s step) output sanity check:**

- Battery: starts at 274,752 Ws (full), ends at 271,002 Ws (98.6%). Consumption 6.25 W constant, generation 0 W because orbit epoch 2028-05-01 lands the satellite in eclipse for first ~150s and the solar panel is producing nothing even when lit (likely because mode 0 = IDLE thomson-spin with random incidence factor ≤0.3).
- Eclipse: 89 in-eclipse, 32 in-sunlit across 120 steps — matches typical SSO ~35 min/orbit ratio.
- Mode: stuck at 0 (IDLE) for entire 600s — no triggers fire.
- No ground station visibility (Lausanne at +47.5N, sat at 97.4° inclination SSO doesn't pass over Lausanne in this 10-min window).
- Per-step runtime: ~8 ms (1s total for 120 steps). 1 day at 10s = 8640 steps → ~70s sim time. Acceptable for a hackathon.

**Surprises worth knowing:**

1. **Solar generation is 0 W even when not in eclipse.** This is because attitude 0 (IDLE) uses a random incidence factor in `SolarPanel.get_effective_surface()`. The cell_area × n_cells × efficiency × 1360 W/m² × cos(θ) calculation, with attitude=0, comes out to ≈0 W in this particular random seed. **For the demo we will force a non-zero baseline (probably by initializing in attitude=1 sun-spin, which sets incidence to 0°).** This is one of the parameters we'll control.
2. **Battery is in watt-seconds (Ws) internally, with `1 Ws = 1 J`.** So 274,752 Ws = 76.32 Wh, matching the spacecraft template's `total_energy: 76.32` parameter. Convenient: SoC can be expressed as `battery_Ws / 274752.0` directly.
3. **CHESS's `Eps.update_batteries()` does no clamping against internal_r or temperature.** Adding a thermal-coupled voltage drop is a true addition, not a modification.
4. **The report generator dumps state JSON and PNG figures to `results/`, but the per-step arrays are **only in memory** during the run.** To use them in our agent pipeline, we need to capture them — either by modifying `Simulation.run()` to return `data_results` (small change, see step 1.5) or by calling the loop manually (the pattern in the smoke test above).

### Step 1.5: Small CHESS cleanups (2026-08-30) — done (folded into Step 1)

- Added `"minute": u.min` to `get_astropy_unit_time()` in `src/digital_twin/utils.py:18`
- Modified `Simulation.run()` to return `data_results` (the per-step arrays dict) so callers don't have to monkey-patch `produce_report`
- Added `per_step_callback: Optional[Callable[[int, dict], None]]` parameter to `Simulation.run()` that fires per-step with a snapshot dict (`t_s`, `rv`, `eclipse`, `com_window`, `mode`, `gs_coords`, `r_earth_sun`). Used by our fault injection + thermal/ADCS hooks.

### Step 1.7: Shared cause/procedure registry (2026-08-30) — done

See `twin/procedures.py` for the full registry. 13 causes, 9 procedures, 9 self-tests passing.

### Step 2: TwinState dataclass (2026-08-30) — done

`twin/state.py`. 40-field state dict with explicit make_default_state(), build_per_step_arrays() that converts to 1D numpy arrays for the per_step_arrays contract, attitude_mode→int code mapping, and snapshot_for_callback() to seed state from CHESS's loop.

### Step 3: EPS module (2026-08-30) — done

`twin/eps.py`. Coulomb counting for SoC (now the source of truth, derived from soc not energy_ws to avoid desync), terminal voltage from V_oc(SoC) - internal_r(T) * load, internal_r as temperature-dependent, heater load budget coupling.

Self-test: 5 tests passing (rises with T, eclipse blocks solar, step produces sane outputs, discharges in eclipse, voltage sags under load).

### Step 4: Thermal module (2026-08-30) — done

`twin/thermal.py`. 4 nodes (battery, payload, electronics, radiator), ported from PASEOS thermal_model.py equation (Martínez EQ 20). Inter-node conductances, Stefan-Boltzmann emission per node, heater inputs, electronics power dissipation, sun + albedo + Earth IR inputs.

Self-test: 4 tests passing. Physics sanity: in eclipse, interior cools, radiator stays warm due to conduction from electronics (correct).

### Step 5: ADCS module (2026-08-30) — done

`twin/adcs.py`. Damped 2nd-order pointing (PD controller), reaction wheel speeds with saturation, attitude-mode-aware controller (nadir/sun/ground active, safe_hold detumble, thomson spin). Star tracker fault injects drift. **Sub-steps internally at max 5s per substep for numerical stability at any caller-provided dt_s.**

Self-test: 4 tests passing.

### Step 6: Fault injection (2026-08-30) — done

`twin/fault_injection.py`. 9 fault types: RampFault, StepFault, StuckFault, MaskFault. Each cause name matches a `twin.procedures.Cause` value. FaultScheduler.inject() with user_param mapping (e.g., magnitude_ohm → peak_value), validation that required params are present, FaultRecord audit trail.

Self-test: 6 tests passing.

### Step 7: Channel shaper (2026-08-30) — done

`twin/channel_shaper.py`. 8 Telemanom channels (P-1, P-2, B-1, T-1, T-2, A-1, G-1, D-1) with SMAP/MSL prefix convention. Each .npy is (n_timesteps, n_features) where n_features = 1 value + 3 context, scaled to (-1, 1) per column.

Self-test: passing.

### Step 8: Run simulator orchestrator (2026-08-30) — done

`twin/run_sim.py`. Wires CHESS's per_step_callback to our EPS, thermal, ADCS modules. Per-step state history, fault injection, channel shaper. Single entry point: `run_sim(config, fault_schedule) -> SimulationOutput`.

### Step 9: Validation function (2026-08-30) — done

`twin/validate.py`. `validate_procedure(state, procedure, params, horizon_s)` projects the twin forward with the procedure applied once at t=0, releases at procedure end, returns predicted + baseline trajectories, constraint violations, risk score, and postcondition check.

**Important fix during implementation:** procedures are applied ONCE at their start_t (not every step). The original code was applying every step, which stacked state mutations (load_a went to -57A).

Self-test: passing for EPS_SHED_LOAD with realistic healthy state.

### Step 10: End-to-end smoke test (2026-08-30) — done

`examples/smoke_test.py`. Runs the full detect→diagnose→propose→validate loop with a single injected eps_internal_r_ramp fault. The diagnostic is a stub (looks at the post-fault state and picks the obvious cause) — your first layer replaces this.

Smoke test output:
- 120 sim steps, 8 channels written, 1 fault applied
- Post-fault state: SoC=0.938, V=29.06V, r=0.33Ω (degraded)
- Diagnosed cause: eps_internal_r_ramp (correct)
- 3 candidate procedures from registry
- Validation: mode_change_to_safe is the only feasible one (SoC recovers to 0.191 vs baseline 0.000)
- Predicted vs baseline shows clear procedure benefit

