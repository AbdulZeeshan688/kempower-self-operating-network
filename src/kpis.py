"""KPIs with exactly the definitions of baseline_kpis.csv (verified by tests/test_kpis.py).

Inputs are frames with the same shape as the baseline files, so the same function scores the
baseline data and our simulation output.
  site_power : ts, site_id, power_kw              (15-min grid power)
  departures : vehicle_id, site_id, depart_ts, depart_soc_pct, required_soc_pct, below_required
  status     : ts, charger_id, site_id, status    (15-min dispenser status)
  public     : site_id, ts, outcome               (one row per public arrival)
"""
import pandas as pd

import config
from src import load

KPI_COLS = ["departures", "departures_below_required_soc", "public_arrivals", "public_abandoned",
            "public_abandon_pct", "energy_mwh", "energy_cost_eur", "peak_kw", "charger_availability_pct"]
ABANDON = {"left_full", "left_queue", "left_after_interruption"}


def _day(df, col, start, end):
    return df[(df[col] >= start) & (df[col] < end)]


def site_cost(site_power: pd.DataFrame, data: dict) -> pd.DataFrame:
    """Adds energy_kwh and cost_eur columns to a 15-min site power frame."""
    price = load.price_eur_kwh(data)
    tariff = data["sites"].set_index("site_id")["tariff_id"]
    sp = site_power.copy()
    sp["energy_kwh"] = sp["power_kw"] * config.SLOT_H
    p = price.reindex(sp["ts"]).reset_index(drop=True)
    sp = sp.reset_index(drop=True)
    sp["eur_kwh"] = [p.at[i, tariff[s]] for i, s in enumerate(sp["site_id"])]
    sp["cost_eur"] = sp["energy_kwh"] * sp["eur_kwh"]
    return sp


def compute_kpis(data, site_power, departures, status, public, start=None, end=None) -> pd.DataFrame:
    start = config.DAY_START if start is None else start
    end = config.DAY_END if end is None else end
    sp = site_cost(_day(site_power, "ts", start, end), data)
    dep = _day(departures, "depart_ts", start, end)
    st = _day(status, "ts", start, end)
    pub = _day(public, "ts", start, end) if public is not None and len(public) else pd.DataFrame(columns=["site_id", "outcome"])
    rows = []
    for s in config.ALL_SITES:
        d = dep[dep.site_id == s]
        p = pub[pub.site_id == s]
        e = sp[sp.site_id == s]
        t = st[st.site_id == s]
        n_pub = len(p)
        n_ab = int(p["outcome"].isin(ABANDON).sum())
        rows.append({
            "site_id": s,
            "departures": len(d),
            "departures_below_required_soc": int(d["below_required"].sum()),
            "public_arrivals": n_pub,
            "public_abandoned": n_ab,
            "public_abandon_pct": round(100 * n_ab / n_pub, 1) if n_pub else None,
            "energy_mwh": round(e["energy_kwh"].sum() / 1000, 2),
            "energy_cost_eur": round(e["cost_eur"].sum(), 0),
            "peak_kw": round(e["power_kw"].max(), 1) if len(e) else 0.0,
            "charger_availability_pct": round(100 * (1 - t["status"].isin(["faulted", "offline"]).mean()), 1),
        })
    return pd.DataFrame(rows)


def baseline_from_data(data, start=None, end=None) -> pd.DataFrame:
    return compute_kpis(data, data["site_power_baseline"], data["departures_baseline"],
                        data["telemetry"], data["public_arrivals"], start, end)


def window_cost(data, site_power, h_from: int, h_to: int, start=None, end=None) -> pd.Series:
    """Energy cost per site for hours [h_from, h_to) on the KPI day (e.g. the 17-19 price spike)."""
    start = config.DAY_START if start is None else start
    end = config.DAY_END if end is None else end
    sp = site_cost(_day(site_power, "ts", start, end), data)
    sp = sp[(sp.ts.dt.hour >= h_from) & (sp.ts.dt.hour < h_to)]
    return sp.groupby("site_id")["cost_eur"].sum().reindex(config.ALL_SITES, fill_value=0.0)


def baseline_human_work(data, day=None) -> pd.DataFrame:
    """Human actions on the close-up sites on the KPI day, from operations_log + service_tickets."""
    day = (config.DAY_START if day is None else day).strftime("%Y-%m-%d")
    ops = data["operations_log"]
    ops = ops[(ops.date == day) & ops.site_id.isin(config.ALL_SITES)]
    return ops.groupby(["site_id", "action"])[["count", "staff_minutes"]].sum().reset_index()


if __name__ == "__main__":
    d = load.load_all()
    print(baseline_from_data(d).to_string(index=False))
    print(d["baseline_kpis"][d["baseline_kpis"].period == "scenario_day"].to_string(index=False))
