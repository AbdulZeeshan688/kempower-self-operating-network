"""Charging schedule for one depot: kW per vehicle per 15-min slot.

Model-predictive control: the site brain solves this every 15 minutes over the next ~30 h and
executes only the first slot. Main solver is a linear program (PuLP/CBC); a greedy heuristic is the
fallback if the solver fails (and is what a charger could run with no site brain at all).

Objective (all in €):
    energy cost  (spot + grid tariff) x kWh
  + peak fee     €/kW-month x highest site power (only above the peak already set this month)
  + 100 €/kWh    for every kWh missing at departure        (hard target as soft constraint)
  + 1.00 €/kWh   for missing cold-weather margin              (worth more than the priciest hour)
  + 0.05 €/kWh   for not topping up to 100 %                   (only fills cheap night hours)
Constraints: dispenser and vehicle max, taper above 80 % SoC, cabinet max x health factor,
site grid limit (incl. notified DSO caps), vehicle on that dispenser only inside its window.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pulp

import config

TOPUP_VALUE_EUR_KWH = 0.05


@dataclass
class Segment:
    key: str
    vid: str
    charger_id: str
    cabinet_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    soc: float
    battery: float
    pmax: float
    req: float
    target: float


def slot_fractions(seg: Segment, slots: pd.DatetimeIndex) -> np.ndarray:
    s0 = slots.as_unit("ns").asi8
    s1 = s0 + config.STEP.value
    lo = np.maximum(s0, seg.start.value)
    hi = np.minimum(s1, seg.end.value)
    return np.clip((hi - lo) / config.STEP.value, 0, 1)


def _taper_cap(pmax, soc):
    if soc <= config.TAPER_START_SOC:
        return pmax
    k = (soc - config.TAPER_START_SOC) / (100 - config.TAPER_START_SOC)
    return pmax * (1 - (1 - config.TAPER_END_FRACTION) * k)


def plan_lp(segments, slots, price, cab_cap, grid_cap, peak_floor, peak_fee):
    n = len(slots)
    prob = pulp.LpProblem("site", pulp.LpMinimize)
    obj = []
    site_sum = [[] for _ in range(n)]
    cab_sum = {c: [[] for _ in range(n)] for c in cab_cap}
    xs = {}
    for seg in segments:
        frac = slot_fractions(seg, slots)
        idx = np.nonzero(frac > 0)[0]
        if len(idx) == 0:
            continue
        kwh_per_pct = seg.battery / 100.0
        x = {s: pulp.LpVariable(f"x_{seg.key}_{s}", 0, seg.pmax * frac[s]) for s in idx}
        xs[seg.key] = x
        # cumulative energy before slot s, for the taper constraint
        cum = 0
        a = (1 - config.TAPER_END_FRACTION) / (100 - config.TAPER_START_SOC)
        for s in idx:
            soc_s = seg.soc + cum / kwh_per_pct
            # taper: x <= pmax*frac*(1 - a*(soc_s - 80)). Below 80 % SoC this is looser than the
            # upper bound, so it only binds above 80 %. Skipped when the vehicle cannot reach 80 % here.
            if seg.soc + seg.pmax * config.SLOT_H * len(idx) / kwh_per_pct > config.TAPER_START_SOC:
                prob += x[s] <= seg.pmax * frac[s] * (1 - a * (soc_s - config.TAPER_START_SOC))
            cum = cum + x[s] * config.SLOT_H
            obj.append(x[s] * config.SLOT_H * (price[s] + config.EARLY_EPS_EUR_KWH * s))
            site_sum[s].append(x[s])
            cab_sum[seg.cabinet_id][s].append(x[s])
        gain = cum / kwh_per_pct
        prob += seg.soc + gain <= 100.0
        short = pulp.LpVariable(f"short_{seg.key}", 0, max(0.0, seg.req - seg.soc))
        cmiss = pulp.LpVariable(f"cmiss_{seg.key}", 0, max(0.0, seg.target - max(seg.req, seg.soc)))
        tmiss = pulp.LpVariable(f"tmiss_{seg.key}", 0, max(0.0, 100 - max(seg.target, seg.soc)))
        prob += seg.soc + gain + short >= seg.req
        prob += seg.soc + gain + short + cmiss >= seg.target
        prob += seg.soc + gain + short + cmiss + tmiss >= 100
        obj += [short * kwh_per_pct * config.SHORTFALL_PENALTY_EUR_KWH,
                cmiss * kwh_per_pct * config.COMFORT_VALUE_EUR_KWH,
                tmiss * kwh_per_pct * TOPUP_VALUE_EUR_KWH]
    peak = pulp.LpVariable("peak", lowBound=peak_floor)
    for s in range(n):
        if site_sum[s]:
            prob += pulp.lpSum(site_sum[s]) <= grid_cap[s]
            prob += pulp.lpSum(site_sum[s]) <= peak
        for c in cab_sum:
            if cab_sum[c][s]:
                prob += pulp.lpSum(cab_sum[c][s]) <= cab_cap[c][s]
    obj.append(peak_fee * peak)
    prob += pulp.lpSum(obj)
    status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=20))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(pulp.LpStatus[status])
    out = {}
    for seg in segments:
        arr = np.zeros(n)
        for s, v in xs.get(seg.key, {}).items():
            arr[s] = max(0.0, v.value() or 0.0)
        out[seg.key] = arr
    return out


def plan_greedy(segments, slots, price, cab_cap, grid_cap, **_):
    """Least slack first; each vehicle takes the cheapest free slots in its window."""
    n = len(slots)
    grid_left = np.array(grid_cap, dtype=float).copy()
    cab_left = {c: np.array(v, dtype=float).copy() for c, v in cab_cap.items()}
    out = {}

    def need_kwh(seg):
        return max(0.0, seg.target - seg.soc) * seg.battery / 100

    def slack(seg):
        hours = max(0.0, (seg.end - max(seg.start, slots[0])).total_seconds() / 3600)
        return hours - need_kwh(seg) / max(seg.pmax, 1)

    for seg in sorted(segments, key=slack):
        frac = slot_fractions(seg, slots)
        arr = np.zeros(n)
        need = need_kwh(seg)
        for s in sorted(np.nonzero(frac > 0)[0], key=lambda s: (price[s], s)):
            if need <= 1e-6:
                break
            p = min(seg.pmax * frac[s], grid_left[s], cab_left[seg.cabinet_id][s], need / config.SLOT_H)
            if p <= 0:
                continue
            arr[s] = p
            need -= p * config.SLOT_H
            grid_left[s] -= p
            cab_left[seg.cabinet_id][s] -= p
        out[seg.key] = arr
    return out


def plan(segments, slots, price, cab_cap, grid_cap, peak_floor=0.0, peak_fee=0.0):
    """Returns ({segment key: kW array over slots}, solver name)."""
    if not segments:
        return {}, "none"
    try:
        return plan_lp(segments, slots, price, cab_cap, grid_cap, peak_floor, peak_fee), "lp"
    except Exception:
        return plan_greedy(segments, slots, price, cab_cap, grid_cap), "greedy"


def taper_cap(pmax, soc):
    return _taper_cap(pmax, soc)
