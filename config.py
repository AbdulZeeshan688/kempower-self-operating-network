"""All thresholds and assumptions in one place. See docs/assumptions.md for the reasoning."""
import pandas as pd

TZ = "+02:00"
DATA_DIR = "data"
OUT_DIR = "outputs"

# Simulation window. Warm-up from Wed noon so Wednesday-evening arrivals are charged by our policy.
SIM_START = pd.Timestamp("2026-11-11T12:00:00+02:00")
SIM_END = pd.Timestamp("2026-11-14T00:00:00+02:00")
DAY_START = pd.Timestamp("2026-11-13T00:00:00+02:00")   # KPI day (scenario_day)
DAY_END = pd.Timestamp("2026-11-14T00:00:00+02:00")
STEP = pd.Timedelta("15min")
SLOT_H = 0.25

DEPOT_SITES = ["S1", "S2", "S5"]
PUBLIC_SITES = ["S3", "S4"]
ALL_SITES = ["S1", "S2", "S3", "S4", "S5"]

# ---- Health model (our estimates, not manufacturer values) ----
HEALTH = {
    "fan_window_h": 6,            # trend window for fan rpm
    "fan_min_data_h": 3,          # need at least this much data to fit a trend
    "fan_nominal_rpm": 1800,
    "fan_watch_drop_pct": 10,     # drop over the window -> watch
    "fan_critical_rpm": 700,      # below this, cooling is inadequate -> critical
    "fan_stop_rpm": 500,          # cabinet trips at about this level (used for failure ETA)
    "fan_eta_degrading_h": 24,    # projected to hit critical within 24 h -> degrading
    "fan_eta_critical_h": 4,      # projected to hit critical within 4 h -> critical
    "err_window_h": 2,            # rolling window for error events per dispenser
    "err_watch_window_h": 6,
    "err_watch": 2,
    "err_degrading": 3,
    "err_critical": 6,
    "temp_rise_watch_c": 4.0,     # cabinet temp vs sibling cabinets, compared with previous days
}
# Power factor the planner applies to a cabinet in each state
HEALTH_FACTOR = {"healthy": 1.0, "watch": 1.0, "degrading": 0.5, "critical": 0.0, "faulted": 0.0, "repair": 0.0}
# A weak fan is not a safety risk, and in the data it declines with time, not with load: derating early
# only wastes capacity. So a cabinet with a projected fan trip keeps full power and is emptied on purpose
# 30 min before the projected trip (no new vehicle goes there unless it can finish before then).
# A connector/cable problem is a safety risk: degrading = 50 %, critical = locked out.
FAN_DEGRADING_FACTOR = 1.0
FAN_CRITICAL_FACTOR = 1.0
TRIP_MARGIN = pd.Timedelta("30min")
HEALTH_SCORE = {"healthy": 100, "watch": 75, "degrading": 45, "critical": 15, "faulted": 0, "repair": 0}

# ---- Planner ----
TAPER_START_SOC = 80.0            # above this, accepted power falls linearly ...
TAPER_END_FRACTION = 0.30         # ... to 30 % of max at 100 % SoC
SHORTFALL_PENALTY_EUR_KWH = 100.0 # missing energy at departure: must outweigh any peak-fee saving
COMFORT_VALUE_EUR_KWH = 1.00      # cold-weather margin: worth more than the priciest hour, far less than a missed departure
EARLY_EPS_EUR_KWH = 1e-5          # tiny preference for earlier charging when prices tie
COLD_TEMP_C = -10.0               # below this, add a consumption margin
COLD_EXTRA_CONSUMPTION = 0.10     # +10 % consumption on the next route
PRICE_DAY_AHEAD_PUBLISH_HOUR = 14  # Finnish time: next-day prices known from 14:00 (13:00 CET)
PLANNING_HORIZON_H = 30
SCHEDULE_LOOKAHEAD_H = 12         # fleet schedule known this far ahead (assumption: published by the operator)

# ---- Bays ----
SWAP_POWER_MARGIN = 1.10          # projected charge time x this (taper, uncertainty)
DRIVER_SWAP_WINDOW_MIN = 15       # a swap within this many minutes of the arriving vehicle = done by its driver

# ---- Repairs ----
SHIFT_START_H = 8
SHIFT_END_H = 16
SHIFT_DAYS = {0, 1, 2, 3, 4}      # Mon-Fri
PART_FOR_PROBLEM = {"fan": "P_FAN", "connector": "P_CABLE_CCS2", "power_module": "P_POWER_MODULE"}
POWER_LIMITED_RATIO = 0.70        # max output in recent window < 70 % of earlier max -> power module, else cable

# ---- Public hub queue model (calibrated against the baseline run, see docs/assumptions.md) ----
PUBLIC_VEHICLE_MAX_KW = {"car": 150, "van": 110, "taxi": 130, "truck": 350}
PUBLIC_QUEUE_MAX = 2              # an arriving driver drives on if this many are already waiting

# ---- Commercial placeholders (team decides final numbers) ----
CREDIT_PER_MISSED_DEPARTURE_EUR = 150   # matches C1's current penalty_eur_per_late_departure
STAFF_COST_EUR_H = 45                   # operations_staff in equipment_costs.csv
