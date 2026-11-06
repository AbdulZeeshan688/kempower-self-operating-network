"""Read and clean the dataset. One function per file; timestamps stay timezone-aware."""
import os
from functools import lru_cache

import pandas as pd

import config

TS_COLS = {
    "fleet_schedule": ["arrive_ts", "depart_ts"],
    "prices": ["ts"],
    "grid_events": ["notified_ts", "start_ts", "end_ts"],
    "connectivity_outages": ["start_ts", "end_ts"],
    "telemetry": ["ts"],
    "telemetry_1min": ["ts"],
    "faults": ["start_ts", "cleared_ts"],
    "service_tickets": ["opened_ts", "dispatched_ts", "arrived_ts", "closed_ts"],
    "sessions_baseline": ["start_ts", "end_ts"],
    "departures_baseline": ["depart_ts"],
    "site_power_baseline": ["ts"],
    "weather": ["ts"],
    "public_arrivals": ["ts"],
    "pv_production": ["ts"],
}


def _read(name: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(config.DATA_DIR, f"{name}.csv"))
    for col in TS_COLS.get(name, []):
        df[col] = pd.to_datetime(df[col], utc=False, format="ISO8601")
    return df


def sites(): return _read("sites")
def power_cabinets(): return _read("power_cabinets")
def chargers(): return _read("chargers")
def vehicles(): return _read("vehicles")
def fleet_schedule(): return _read("fleet_schedule")
def prices(): return _read("prices")
def tariffs(): return _read("tariffs")
def grid_events(): return _read("grid_events")
def connectivity_outages(): return _read("connectivity_outages")
def telemetry(): return _read("telemetry")
def telemetry_1min(): return _read("telemetry_1min")
def faults(): return _read("faults")
def service_tickets(): return _read("service_tickets")
def spare_parts(): return _read("spare_parts")
def technicians(): return _read("technicians")
def travel_times(): return _read("travel_times")
def site_config(): return _read("site_config")
def departures_baseline(): return _read("departures_baseline")
def sessions_baseline(): return _read("sessions_baseline")
def site_power_baseline(): return _read("site_power_baseline")
def baseline_kpis(): return _read("baseline_kpis")
def operations_log(): return _read("operations_log")
def weather(): return _read("weather")
def public_arrivals(): return _read("public_arrivals")
def pv_production(): return _read("pv_production")
def contracts(): return _read("contracts")


@lru_cache(maxsize=1)
def load_all() -> dict:
    names = ["sites", "power_cabinets", "chargers", "vehicles", "fleet_schedule", "prices", "tariffs",
             "grid_events", "connectivity_outages", "telemetry", "telemetry_1min", "faults",
             "service_tickets", "spare_parts", "technicians", "travel_times", "site_config",
             "departures_baseline", "sessions_baseline", "site_power_baseline", "baseline_kpis",
             "operations_log", "weather", "public_arrivals", "pv_production", "contracts"]
    data = {n: globals()[n]() for n in names}
    data["weather"] = data["weather"][data["weather"]["location"] == "Close-up town"].reset_index(drop=True)
    return data


def in_window(df: pd.DataFrame, col: str, start=None, end=None) -> pd.DataFrame:
    start = config.SIM_START if start is None else start
    end = config.SIM_END if end is None else end
    return df[(df[col] >= start) & (df[col] < end)]


def price_eur_kwh(data: dict) -> pd.DataFrame:
    """Energy price per tariff per 15-min slot, €/kWh = (spot + grid tariff energy part) / 1000."""
    p = data["prices"].copy()
    p["T_DEPOT"] = (p["spot_eur_mwh"] + p["grid_tariff_depot_eur_mwh"]) / 1000.0
    p["T_PUBLIC"] = (p["spot_eur_mwh"] + p["grid_tariff_public_eur_mwh"]) / 1000.0
    return p.set_index("ts")[["spot_eur_mwh", "T_DEPOT", "T_PUBLIC"]]


def friday_events(data: dict) -> list[str]:
    """Human-readable list of what happens around the scenario day, computed from the data."""
    out = []
    f = data["faults"]
    for _, r in f[(f.start_ts >= "2026-11-10") & (f.start_ts < "2026-11-14")].iterrows():
        out.append(f"{r.start_ts:%a %d.%m %H:%M} {r.site_id} {r.cabinet_id} {r.charger_id if isinstance(r.charger_id, str) else '(cabinet)'}: "
                   f"{r.fault_id} {r.fault_category}/{r.severity} - {r.symptom} (cleared by {r.cleared_by})")
    p = data["prices"]
    fri = p[(p.ts >= config.DAY_START) & (p.ts < config.DAY_END)]
    mx = fri.loc[fri.spot_eur_mwh.idxmax()]
    exp = fri[fri.spot_eur_mwh > 200]
    out.append(f"Price: max {mx.spot_eur_mwh:.0f} €/MWh at {mx.ts:%H:%M}; >200 €/MWh from {exp.ts.min():%H:%M} "
               f"to {(exp.ts.max() + config.STEP):%H:%M}; Friday mean {fri.spot_eur_mwh.mean():.0f}, "
               f"other days mean {p[(p.ts < config.DAY_START)].spot_eur_mwh.mean():.0f} €/MWh")
    for _, g in data["grid_events"].iterrows():
        out.append(f"Grid {g.event_id}: {g.site_id} {g.type} {g.limit_kw} kW {g.start_ts:%a %d.%m %H:%M}-{g.end_ts:%H:%M}, notified {g.notified_ts:%a %H:%M}")
    co = data["connectivity_outages"]
    for _, c in co[(co.end_ts - co.start_ts) >= pd.Timedelta("1h")].iterrows():
        out.append(f"Cloud outage: {c.site_id} {c.start_ts:%a %d.%m %H:%M}-{c.end_ts:%H:%M}")
    w = data["weather"]
    cold = w[(w.ts >= "2026-11-12") & (w.ts < config.DAY_END)]
    out.append(f"Weather: min {cold.temp_c.min():.1f} °C Thu-Fri (Close-up town)")
    t1 = data["telemetry_1min"]
    fan = t1[t1.charger_id == "D1-9"].dropna(subset=["fan_rpm"])
    out.append(f"S1 C1-3 fan: {fan.fan_rpm.iloc[:30].mean():.0f} rpm at {fan.ts.min():%a %H:%M} -> "
               f"{fan.fan_rpm.iloc[-30:].mean():.0f} rpm at {fan.ts.max():%a %H:%M}")
    return out


if __name__ == "__main__":
    d = load_all()
    for k, v in d.items():
        print(f"{k:24s} {len(v):7d} rows")
    print()
    for line in friday_events(d):
        print(line)
