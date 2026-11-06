"""Failure prediction: an explainable health state per cabinet and per dispenser.

Rules (thresholds in config.HEALTH):
  * Fan (cabinet): fit a straight line to fan rpm over the last 6 h. If the trend reaches the
    critical level (700 rpm) within 24 h -> degrading, within 4 h or already below -> critical.
    A drop of more than 10 % in the window -> watch. The same line gives an ETA for the cabinet
    to trip (500 rpm), which the planner uses.
  * Errors (dispenser): >=2 error events in 6 h -> watch; in the last 2 h >=3 degrading, >=6 critical.
    1-minute error_events are used where they exist, otherwise the 15-minute error_count.
    Rows logged while the dispenser is already 'faulted' are ignored (that is the fault, not a warning).
  * Temperature (cabinet): difference to sibling cabinets on the same site, last 6 h vs previous 3 days.
    Supporting evidence only (adds to the reason, never raises the state by itself).

Only data strictly before `now` is used. Units repaired by us are reset (data before the repair is ignored,
and fan data after a fan swap is treated as nominal because the real telemetry comes from a world without it).
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config

H = config.HEALTH
ORDER = ["healthy", "watch", "degrading", "critical"]


@dataclass
class Health:
    unit: str
    state: str = "healthy"
    score: int = 100
    reasons: list = field(default_factory=list)
    problem: str | None = None        # fan / connector_or_module
    eta_stop: pd.Timestamp | None = None

    def worsen(self, state, reason, problem=None):
        if ORDER.index(state) > ORDER.index(self.state):
            self.state = state
            if problem:
                self.problem = problem
        elif problem and not self.problem and state != "healthy":
            self.problem = problem
        self.reasons.append(reason)

    @property
    def reason(self):
        return "; ".join(self.reasons) if self.reasons else "all signals normal"


class HealthModel:
    def __init__(self, data: dict):
        self.data = data
        ch = data["chargers"]
        self.cabinet_of = dict(zip(ch.charger_id, ch.cabinet_id))
        self.dispensers_of = ch.groupby("cabinet_id")["charger_id"].apply(list).to_dict()
        t1 = data["telemetry_1min"]
        fan = t1.dropna(subset=["fan_rpm"]).copy()
        fan["cabinet_id"] = fan["charger_id"].map(self.cabinet_of)
        self.fan = {c: g.groupby("ts")["fan_rpm"].mean() for c, g in fan.groupby("cabinet_id")}
        self.err1 = {c: g.set_index("ts")["error_events"] for c, g in t1.groupby("charger_id")}
        tel = data["telemetry"]
        ok = tel[tel.status != "faulted"]
        self.err15 = {c: g.set_index("ts")["error_count"] for c, g in ok.groupby("charger_id")}
        tel = tel.copy()
        tel["cabinet_id"] = tel["charger_id"].map(self.cabinet_of)
        cab = tel.groupby(["cabinet_id", "ts"]).agg(temp=("cabinet_temp_c", "mean"), kw=("power_kw", "sum")).reset_index()
        self.cab_temp = {c: g.set_index("ts") for c, g in cab.groupby("cabinet_id")}
        pc = data["power_cabinets"]
        self.site_of_cab = dict(zip(pc.cabinet_id, pc.site_id))
        self.cabs_of_site = pc.groupby("site_id")["cabinet_id"].apply(list).to_dict()
        self.repaired_at: dict[str, pd.Timestamp] = {}
        self.fan_replaced: set[str] = set()

    # ---- signals ------------------------------------------------------------------------------
    def _since(self, unit):
        return self.repaired_at.get(unit, pd.Timestamp.min.tz_localize("UTC"))

    def fan_signal(self, cabinet, now, h: Health):
        if cabinet in self.fan_replaced or cabinet not in self.fan:
            return
        s = self.fan[cabinet]
        start = max(now - pd.Timedelta(hours=H["fan_window_h"]), self._since(cabinet))
        w = s[(s.index >= start) & (s.index < now)]
        if len(w) < 2 or (w.index[-1] - w.index[0]) < pd.Timedelta(hours=H["fan_min_data_h"]):
            return
        hrs = (w.index - w.index[0]).total_seconds().values / 3600
        slope, icpt = np.polyfit(hrs, w.values, 1)          # rpm per hour
        level = float(w.iloc[-15:].mean())
        first = icpt
        drop = 100 * (first - level) / first if first > 0 else 0
        if level < H["fan_critical_rpm"]:
            h.worsen("critical", f"fan {level:.0f} rpm (critical < {H['fan_critical_rpm']})", "fan")
        if slope < 0:
            eta_crit_h = (level - H["fan_critical_rpm"]) / -slope
            eta_stop_h = (level - H["fan_stop_rpm"]) / -slope
            h.eta_stop = now + pd.Timedelta(hours=max(eta_stop_h, 0))
            if 0 < eta_crit_h < H["fan_eta_critical_h"]:
                h.worsen("critical", f"fan reaches {H['fan_critical_rpm']} rpm in {eta_crit_h:.1f} h", "fan")
            elif 0 < eta_crit_h < H["fan_eta_degrading_h"]:
                h.worsen("degrading", f"fan {level:.0f} rpm, falling {-slope:.0f} rpm/h, "
                         f"projected cabinet trip ~{h.eta_stop:%a %H:%M}", "fan")
        if drop > H["fan_watch_drop_pct"]:
            h.worsen("watch", f"fan speed down {drop:.0f}% in {H['fan_window_h']} h", "fan")

    def error_count(self, charger, now, hours=None):
        hours = H["err_window_h"] if hours is None else hours
        start = max(now - pd.Timedelta(hours=hours), self._since(charger))
        if charger in self.err1:
            s = self.err1[charger]
            if len(s) and s.index.min() <= start and s.index.max() >= now - pd.Timedelta("2min"):
                return int(s[(s.index >= start) & (s.index < now)].sum())
        s = self.err15.get(charger)
        if s is None:
            return 0
        # a 15-min row covers [ts, ts+15min): only rows that are complete before now
        return int(s[(s.index >= start) & (s.index + config.STEP <= now)].sum())

    def error_signal(self, charger, now, h: Health):
        n = self.error_count(charger, now)
        if n >= H["err_critical"]:
            h.worsen("critical", f"{n} error events in {H['err_window_h']} h", "connector_or_module")
        elif n >= H["err_degrading"]:
            h.worsen("degrading", f"{n} error events in {H['err_window_h']} h", "connector_or_module")
        else:
            n6 = self.error_count(charger, now, H["err_watch_window_h"])
            if n6 >= H["err_watch"]:
                h.worsen("watch", f"{n6} error events in {H['err_watch_window_h']} h", "connector_or_module")

    def temp_signal(self, cabinet, now, h: Health):
        """Cabinet temperature compared with its sibling cabinets at the same site (cancels weather),
        last 6 h vs the previous 3 days."""
        df = self.cab_temp.get(cabinet)
        site = self.site_of_cab.get(cabinet)
        sibs = [c for c in self.cabs_of_site.get(site, []) if c != cabinet]
        if df is None or not sibs:
            return
        sib = pd.concat([self.cab_temp[c].temp for c in sibs], axis=1).median(axis=1)
        diff = (df.temp - sib).dropna()
        diff = diff[(diff.index + config.STEP <= now) & (diff.index >= self._since(cabinet))]
        recent = diff[diff.index >= now - pd.Timedelta(hours=6)]
        past = diff[(diff.index < now - pd.Timedelta(hours=6)) & (diff.index >= now - pd.Timedelta(days=3))]
        if len(recent) < 8 or len(past) < 48:
            return
        rise = recent.mean() - past.mean()
        # Supporting evidence only: on its own this signal gave false alarms on most cabinets in the
        # cold snap, so it never raises the state by itself (see docs/assumptions.md).
        if rise > H["temp_rise_watch_c"] and h.state != "healthy":
            h.reasons.append(f"cabinet {rise:.1f} °C warmer than sibling cabinets")

    # ---- public API ---------------------------------------------------------------------------
    def cabinet(self, cabinet, now) -> Health:
        h = Health(cabinet)
        self.fan_signal(cabinet, now, h)
        self.temp_signal(cabinet, now, h)
        h.score = config.HEALTH_SCORE[h.state]
        return h

    def dispenser(self, charger, now) -> Health:
        h = Health(charger)
        self.error_signal(charger, now, h)
        h.score = config.HEALTH_SCORE[h.state]
        return h

    def site(self, site_id, now) -> dict[str, Health]:
        ch = self.data["chargers"]
        out = {}
        for cab in self.data["power_cabinets"].query("site_id == @site_id").cabinet_id:
            out[cab] = self.cabinet(cab, now)
        for d in ch[ch.site_id == site_id].charger_id:
            out[d] = self.dispenser(d, now)
        return out

    def mark_repaired(self, unit, ts, fan_swapped=False):
        self.repaired_at[unit] = ts
        if fan_swapped:
            self.fan_replaced.add(unit)


def first_flag_times(model: HealthModel, unit: str, start, end, kind="dispenser", step="15min") -> dict:
    """Backtest: first time each state is reached for one unit (used for the D3-3 slide)."""
    out = {}
    for t in pd.date_range(start, end, freq=step):
        h = model.dispenser(unit, t) if kind == "dispenser" else model.cabinet(unit, t)
        for s in ORDER[1:]:
            if ORDER.index(h.state) >= ORDER.index(s) and s not in out:
                out[s] = (t, h.reason)
    return out
