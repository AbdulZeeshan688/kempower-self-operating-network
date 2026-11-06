"""Builds outputs/kpi_comparison.csv and the other result tables from the simulation runs.
Every number is computed here from simulation output or the dataset; nothing is typed in."""
import json
import os

import numpy as np
import pandas as pd

import config
from src import commissioning_check, health, kpis, repairs

D0, D1 = config.DAY_START, config.DAY_END
THU = pd.Timestamp("2026-11-12T00:00:00+02:00")


def _win(df, col, a, b):
    return df[(df[col] >= a) & (df[col] < b)]


def minutes_per_move(data):
    ops = data["operations_log"]
    m = ops[ops.action == "move_and_replug_vehicle"]
    return float(m.staff_minutes.sum() / m["count"].sum())


def baseline_human(data, a=D0, b=D1) -> pd.DataFrame:
    ops = data["operations_log"]
    days = pd.date_range(a, b - pd.Timedelta("1min"), freq="D").strftime("%Y-%m-%d")
    ops = ops[ops.date.isin(days) & ops.site_id.isin(config.ALL_SITES)]
    g = ops.groupby(["site_id", "action"])[["count", "staff_minutes"]].sum().reset_index()
    return g


def baseline_night_callouts(data, a=D0, b=D1):
    t = data["service_tickets"].dropna(subset=["dispatched_ts"])
    t = _win(t, "dispatched_ts", a, b)
    return t[~t.dispatched_ts.apply(repairs.in_shift)]


def autopilot_human(data, out, a=D0, b=D1) -> pd.DataFrame:
    """Human actions in our run: yard moves, technician visits, risk approvals (from the decision log)."""
    lg = _win(out["log"], "ts", a, b)
    mpm = minutes_per_move(data)
    rows = []
    for s in config.ALL_SITES:
        l = lg[lg.site_id == s]
        moves = l[(l.type == "move") & l.decision.str.startswith("Planned yard move")]
        rows.append((s, "move_and_replug_vehicle", len(moves), len(moves) * mpm))
        visits = l[(l.type == "repair") & l.decision.str.contains("repaired by")]
        mins = 0.0
        for _, v in visits.iterrows():
            unit = v.decision.split(" ")[0]
            b_ = [x for x in out["bookings"] if x["unit"] == unit and x["site_id"] == s]
            if b_:
                bk = b_[-1]
                mins += (bk["done"] - bk["depart"]).total_seconds() / 60 + (bk["arrive"] - bk["depart"]).total_seconds() / 60
        rows.append((s, "technician_visit", len(visits), mins))
        risk = l[(l.type == "risk")]
        rows.append((s, "risk_notice_approval", len(risk), len(risk) * 5.0))
    return pd.DataFrame(rows, columns=["site_id", "action", "count", "staff_minutes"])


def night_callouts(out, a=D0, b=D1):
    return [x for x in out["bookings"] if x.get("night_callout") and a <= x["depart"] < b]


def energy_window(out_power, h0, h1):
    p = _win(out_power, "ts", D0, D1)
    p = p[(p.ts.dt.hour >= h0) & (p.ts.dt.hour < h1)]
    return p.groupby("site_id").power_kw.sum().mul(config.SLOT_H).reindex(config.ALL_SITES, fill_value=0.0)


def kpi_table(data, outs) -> pd.DataFrame:
    base = data["baseline_kpis"].query("period == 'scenario_day'").set_index("site_id")
    runs = {"autopilot": outs["autopilot"], "worst_case": outs["worst_case"]}
    k = {n: kpis.compute_kpis(data, o["power"][["ts", "site_id", "power_kw"]], o["departures"], o["status"],
                              o["public"] if len(o["public"]) else None).set_index("site_id") for n, o in runs.items()}
    bp = data["site_power_baseline"]
    cost_b = kpis.window_cost(data, bp, 17, 19)
    e_b = energy_window(bp, 17, 19)
    cost_r = {n: kpis.window_cost(data, o["power"][["ts", "site_id", "power_kw"]], 17, 19) for n, o in runs.items()}
    e_r = {n: energy_window(o["power"], 17, 19) for n, o in runs.items()}
    hb = baseline_human(data)
    hr = {n: autopilot_human(data, o) for n, o in runs.items()}
    rows = []

    def add(site, kpi, b, vals, better="lower"):
        a = vals["autopilot"]
        ch = None if b is None or a is None or (isinstance(b, float) and np.isnan(b)) else round(a - b, 2)
        rows.append({"site_id": site, "kpi": kpi, "baseline": b, "autopilot": a, "change": ch,
                     "worst_case": vals["worst_case"], "better_if": better})

    for s in config.ALL_SITES:
        for col in ["departures", "departures_below_required_soc", "public_arrivals", "public_abandoned",
                    "public_abandon_pct", "energy_mwh", "energy_cost_eur", "peak_kw", "charger_availability_pct"]:
            b = base.loc[s, col]
            b = None if pd.isna(b) else float(b)
            vals = {n: (None if pd.isna(k[n].loc[s, col]) else float(k[n].loc[s, col])) for n in runs}
            if col in ("public_arrivals", "public_abandoned", "public_abandon_pct") and s in config.DEPOT_SITES:
                continue
            if col in ("departures", "departures_below_required_soc") and s in config.PUBLIC_SITES:
                continue
            add(s, col, b, vals, "higher" if col == "charger_availability_pct" else ("same" if col in ("departures", "public_arrivals") else "lower"))
        b_avg = base.loc[s, "energy_cost_eur"] / base.loc[s, "energy_mwh"]
        add(s, "avg_price_eur_mwh", round(b_avg, 1),
            {n: round(k[n].loc[s, "energy_cost_eur"] / k[n].loc[s, "energy_mwh"], 1) if k[n].loc[s, "energy_mwh"] else None for n in runs})
        add(s, "cost_17_19_eur", round(cost_b[s], 0), {n: round(cost_r[n][s], 0) for n in runs})
        add(s, "energy_17_19_kwh", round(e_b[s], 0), {n: round(e_r[n][s], 0) for n in runs})
        if s in config.DEPOT_SITES:
            mv = lambda h: float(h[(h.site_id == s) & (h.action == "move_and_replug_vehicle")]["count"].sum())
            add(s, "staff_vehicle_moves", mv(hb), {n: mv(hr[n]) for n in runs})
        hh = lambda h: round(float(h[h.site_id == s].staff_minutes.sum()) / 60, 2)
        add(s, "staff_hours", hh(hb), {n: hh(hr[n]) for n in runs})
    # network totals
    tot = pd.DataFrame(rows)
    for kpi in ["departures", "departures_below_required_soc", "public_abandoned", "energy_mwh", "energy_cost_eur",
                "cost_17_19_eur", "energy_17_19_kwh", "staff_vehicle_moves", "staff_hours"]:
        t = tot[tot.kpi == kpi]
        add("ALL", kpi, round(t.baseline.sum(), 2), {"autopilot": round(t.autopilot.sum(), 2), "worst_case": round(t.worst_case.sum(), 2)},
            "same" if kpi == "departures" else "lower")
    nb = len(baseline_night_callouts(data))
    add("ALL", "night_callouts", nb, {n: len(night_callouts(o)) for n, o in runs.items()})
    # repair cost of the C1-3 job vs the real night ticket K013
    k013 = data["service_tickets"].set_index("ticket_id").loc["K013", "cost_eur"]
    rc = {}
    for n, o in runs.items():
        b_ = [x for x in o["bookings"] if x["unit"] == "C1-3"]
        rc[n] = b_[-1]["cost_eur"] if b_ else None
    add("S1", "repair_cost_C1-3_eur", float(k013), rc)
    return pd.DataFrame(rows)


def health_leadtimes(data) -> pd.DataFrame:
    hm = health.HealthModel(data)
    f = data["faults"].set_index("fault_id")
    cases = [("C1-3", "cabinet", "F015", "2026-11-12T00:00:00+02:00"),
             ("D4-2", "dispenser", "F017", "2026-11-13T12:00:00+02:00"),
             ("D3-3", "dispenser", "F014", "2026-11-10T12:00:00+02:00")]
    rows = []
    for unit, kind, fid, start in cases:
        fail = f.loc[fid, "start_ts"]
        flags = health.first_flag_times(hm, unit, pd.Timestamp(start), fail - pd.Timedelta("1min"), kind)
        for state, (t, reason) in flags.items():
            rows.append({"unit": unit, "fault_id": fid, "failure_ts": fail, "state": state, "flag_ts": t,
                         "lead_h": round((fail - t).total_seconds() / 3600, 1), "reason": reason})
    return pd.DataFrame(rows)


def fan_eta_track(data) -> pd.DataFrame:
    """Projected trip time of C1-3 as the fan degrades (for the health panel)."""
    hm = health.HealthModel(data)
    rows = []
    for t in pd.date_range("2026-11-12T03:00:00+02:00", "2026-11-13T03:15:00+02:00", freq="15min"):
        h = hm.cabinet("C1-3", t)
        fan = hm.fan["C1-3"]
        w = fan[(fan.index < t) & (fan.index >= t - pd.Timedelta("15min"))]
        rows.append({"ts": t, "state": h.state, "score": h.score, "eta_stop": h.eta_stop,
                     "fan_rpm": float(w.mean()) if len(w) else None, "reason": h.reason})
    return pd.DataFrame(rows)


def public_calibration(data, outs) -> pd.DataFrame:
    rows = []
    ab = kpis.ABANDON
    for s in config.PUBLIC_SITES:
        for day in [pd.Timestamp("2026-11-12T00:00:00+02:00"), D0]:
            e = day + pd.Timedelta(days=1)
            real = _win(data["public_arrivals"], "ts", day, e)
            real = real[real.site_id == s]
            rep = _win(outs["public_baseline_rules"]["public"], "ts", day, e)
            rep = rep[rep.site_id == s]
            ap = _win(outs["autopilot"]["public"], "ts", day, e)
            ap = ap[ap.site_id == s]
            rows.append({"site_id": s, "day": f"{day:%a %d.%m}", "arrivals": len(real),
                         "abandoned_data": int(real.outcome.isin(ab).sum()),
                         "abandoned_replay_today_rules": int(rep.outcome.isin(ab).sum()),
                         "abandoned_autopilot": int(ap.outcome.isin(ab).sum())})
    return pd.DataFrame(rows)


def commissioning_effect(data, outs) -> dict:
    fixed = outs["autopilot"]["departures"]
    asc = outs["autopilot_s5_as_configured"]["departures"]
    f = lambda d: int(_win(d[d.site_id == "S5"], "depart_ts", D0, D1).below_required.sum())
    base = int(data["baseline_kpis"].query("period=='scenario_day' and site_id=='S5'").departures_below_required_soc.iloc[0])
    return {"baseline_data": base, "autopilot_as_configured": f(asc), "autopilot_config_fixed": f(fixed),
            "mismatches": commissioning_check.mismatches(data).to_dict("records")}


def redesign_effect(data, outs) -> pd.DataFrame:
    rows = []
    hb = baseline_human(data)
    for s in ["S1", "S5"]:
        r = autopilot_human(data, outs["redesign_2029_outlets"])
        a = autopilot_human(data, outs["autopilot"])
        dep = outs["redesign_2029_outlets"]["departures"]
        rows.append({"site_id": s,
                     "moves_baseline": int(hb[(hb.site_id == s) & (hb.action == "move_and_replug_vehicle")]["count"].sum()),
                     "moves_autopilot_today_hw": int(a[(a.site_id == s) & (a.action == "move_and_replug_vehicle")]["count"].sum()),
                     "moves_with_one_outlet_per_vehicle": int(r[(r.site_id == s) & (r.action == "move_and_replug_vehicle")]["count"].sum()),
                     "below_required_with_outlets": int(_win(dep[dep.site_id == s], "depart_ts", D0, D1).below_required.sum())})
    return pd.DataFrame(rows)


def headline(data, table, outs) -> dict:
    g = lambda site, kpi, col: table[(table.site_id == site) & (table.kpi == kpi)][col].iloc[0]
    lg = outs["autopilot"]["log"]
    fri = _win(lg, "ts", D0, D1)
    h = {k: {"baseline": g("ALL", k, "baseline"), "autopilot": g("ALL", k, "autopilot"), "worst_case": g("ALL", k, "worst_case")}
         for k in ["departures_below_required_soc", "public_abandoned", "energy_cost_eur", "cost_17_19_eur",
                   "staff_vehicle_moves", "staff_hours", "night_callouts", "energy_mwh"]}
    h["peak_kw"] = {n: {s: g(s, "peak_kw", n) for s in config.ALL_SITES} for n in ["baseline", "autopilot", "worst_case"]}
    h["decisions_friday"] = int(len(fri))
    h["decisions_friday_autonomous"] = int((fri.human_needed != "yes").sum())
    s2 = _win(outs["autopilot"]["power"], "ts", pd.Timestamp("2026-11-13T16:00:00+02:00"), pd.Timestamp("2026-11-13T19:00:00+02:00"))
    h["s2_max_kw_during_cap"] = float(s2[s2.site_id == "S2"].power_kw.max())
    return h


def write_all(data, outs):
    os.makedirs(config.OUT_DIR, exist_ok=True)
    table = kpi_table(data, outs)
    table.to_csv(os.path.join(config.OUT_DIR, "kpi_comparison.csv"), index=False)
    outs["autopilot"]["log"].to_csv(os.path.join(config.OUT_DIR, "decision_log.csv"), index=False)
    lt = health_leadtimes(data)
    lt.to_csv(os.path.join(config.OUT_DIR, "health_leadtimes.csv"), index=False)
    fan_eta_track(data).to_csv(os.path.join(config.OUT_DIR, "c1_3_fan_eta.csv"), index=False)
    cal = public_calibration(data, outs)
    cal.to_csv(os.path.join(config.OUT_DIR, "public_calibration.csv"), index=False)
    com = commissioning_effect(data, outs)
    red = redesign_effect(data, outs)
    red.to_csv(os.path.join(config.OUT_DIR, "redesign_2029.csv"), index=False)
    hb = baseline_human(data, THU, D1)
    ha = autopilot_human(data, outs["autopilot"], THU, D1)
    hw = autopilot_human(data, outs["worst_case"], THU, D1)
    h48 = pd.DataFrame({"run": ["baseline", "autopilot", "worst_case"],
                        "actions_thu_fri": [int(hb["count"].sum()), int(ha["count"].sum()), int(hw["count"].sum())],
                        "staff_hours_thu_fri": [round(hb.staff_minutes.sum() / 60, 1), round(ha.staff_minutes.sum() / 60, 1),
                                                round(hw.staff_minutes.sum() / 60, 1)]})
    h48.to_csv(os.path.join(config.OUT_DIR, "human_work_thu_fri.csv"), index=False)
    head = headline(data, table, outs)
    head["commissioning"] = {k: v for k, v in com.items() if k != "mismatches"}
    head["oncall_variant_S1_below"] = int(_win(outs["worst_case_oncall"]["departures"], "depart_ts", D0, D1).below_required.sum()) \
        if "worst_case_oncall" in outs else None
    with open(os.path.join(config.OUT_DIR, "headline.json"), "w") as f:
        json.dump(head, f, indent=2, default=str)
    pd.set_option("display.width", 200)
    print("\n=== KPI comparison (Fri 13.11.) ===")
    print(table.to_string(index=False))
    print("\n=== Human work Thu+Fri ===\n", h48.to_string(index=False))
    print("\n=== Health lead times ===\n", lt[["unit", "state", "flag_ts", "lead_h"]].to_string(index=False))
    print("\n=== Public hubs: abandoned drivers ===\n", cal.to_string(index=False))
    print("\n=== S5 commissioning ===\n", {k: v for k, v in com.items() if k != "mismatches"})
    print("\n=== 2029 redesign: one outlet per vehicle ===\n", red.to_string(index=False))
    print("\n=== Headline ===\n", json.dumps(head, indent=1, default=str)[:3000])
