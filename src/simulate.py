"""Runs the close-up network from Wed 11.11. 12:00 (warm-up) to Sat 14.11. 00:00 with no human decisions.

Three layers, kept apart on purpose:
  charger layer  hard limits only: dispenser/cabinet max, real faults, the real grid cap. It enforces
                 physics whatever the site brain asks for.
  site brain     plans charging, assigns bays, tracks health, derates weak equipment. Runs on the last
                 prices / schedules / grid notices it received, so it keeps working when the cloud is gone.
  cloud          publishes prices, schedules, grid notices; books technicians and parts.

Modes:
  autopilot          predictive maintenance on: C1-3's fan is swapped on Thursday, the real Friday failure never happens
  worst_case         no technician at all until Fri 08:00: C1-3 still fails, the system survives by re-planning alone
  worst_case_oncall  as worst_case, but the on-call technician (TE2, week 46) may be called out at night -
                     the system does so only if its re-plan shows a departure would be missed
Usage: python -m src.simulate
"""
import os
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
from src import bays, events, health, kpis, load, planner, public, repairs
from src.commissioning_check import site_parameters

INF = pd.Timestamp("2100-01-01T00:00:00+02:00")


@dataclass
class Visit:
    key: str
    vid: str
    site: str
    arrive: pd.Timestamp
    depart: pd.Timestamp
    soc: float
    req: float
    target: float
    battery: float
    vmax: float
    route_km: float
    depart_in_data: bool
    status: str = "future"       # future / waiting / plugged / parked / gone
    charger: str | None = None
    needs_move: bool = False      # was unplugged from a failed bay and must be re-plugged by someone
    risk_logged: bool = False


def build_visits(data, site) -> dict:
    fs = data["fleet_schedule"]
    fs = fs[(fs.site_id == site) & (fs.arrive_ts < config.SIM_END) &
            ((fs.depart_ts >= config.SIM_START) | fs.depart_ts.isna())]
    veh = data["vehicles"].set_index("vehicle_id")
    w = data["weather"].set_index("ts")["temp_c"]
    out = {}
    for i, r in fs.reset_index(drop=True).iterrows():
        v = veh.loc[r.vehicle_id]
        in_data = not pd.isna(r.depart_ts)
        # assumption: no departure in the data -> leaves next morning 06:00 (outside the KPI day)
        depart = r.depart_ts if in_data else (r.arrive_ts.normalize() + pd.Timedelta(days=1, hours=6))
        temp = w.asof(depart.floor("1h")) if depart.floor("1h") >= w.index[0] else w.iloc[0]
        extra = 0.0
        if temp < config.COLD_TEMP_C:
            extra = 100 * config.COLD_EXTRA_CONSUMPTION * r.next_route_km * v.consumption_kwh_per_km / v.battery_kwh
        key = f"{r.vehicle_id}@{r.arrive_ts:%d%H%M}"
        out[key] = Visit(key, r.vehicle_id, site, max(r.arrive_ts, config.SIM_START), depart, float(r.arrive_soc_pct),
                         float(r.required_soc_pct), float(min(100.0, r.required_soc_pct + extra)), float(v.battery_kwh),
                         float(v.max_charge_kw), float(r.next_route_km), in_data)
        # vehicles already on site when the warm-up starts begin at their arrival SoC: no risk notices for them
        out[key].risk_logged = bool(r.arrive_ts < config.SIM_START)
    return out


def price_view(now, series: pd.Series, slots: pd.DatetimeIndex) -> np.ndarray:
    """What the site knows: day-ahead prices published at 14:00 for the next day; beyond that, yesterday's price."""
    known_until = now.normalize() + pd.Timedelta(days=2 if now.hour >= config.PRICE_DAY_AHEAD_PUBLISH_HOUR else 1)
    arr = series.reindex(slots).values.copy()
    late = slots >= known_until
    if late.any():
        arr[late] = series.reindex(slots[late] - pd.Timedelta(days=1)).values
    return np.nan_to_num(arr, nan=float(np.nanmean(series.values)))


class DepotSim:
    def __init__(self, data, site, mode="autopilot", s5_config="fixed", outlets=None):
        self.data, self.site, self.mode = data, site, mode
        self.log = events.DecisionLog()
        self.hm = health.HealthModel(data)
        self.p = site_parameters(data, site, s5_config if site == "S5" else "fixed", outlets)
        self.info = self.p["info"]
        self.visits = build_visits(data, site)
        tariff = data["sites"].set_index("site_id").loc[site, "tariff_id"]
        self.price = load.price_eur_kwh(data)[tariff]
        self.peak_fee = data["tariffs"].set_index("tariff_id").loc[tariff, "peak_power_fee_eur_kw_month"]
        self.grid_events = data["grid_events"][data["grid_events"].site_id == site]
        self.received_events = set()
        self.plugged: dict[str, str] = {}
        self.bookings: dict[str, repairs.Booking] = {}
        self.state: dict[str, str] = {}
        self.peak = 0.0
        self.rows_power, self.rows_status, self.rows_dep, self.rows_bay, self.rows_health = [], [], [], [], []
        self.moves = {"yard": 0, "driver": 0}
        self.committed: dict[str, tuple] = {}
        self.callout_refused: set = set()
        # worst cases: the fan cannot be swapped before Friday 08:00, so C1-3 really fails at 02:25
        self.tech_unavailable_until = pd.Timestamp("2026-11-13T08:00:00+02:00") if mode.startswith("worst_case") else None
        self.oncall_available = mode == "worst_case_oncall"
        self.faults = self._site_faults()

    # ---------- charger layer: real faults ----------
    def _site_faults(self):
        f = self.data["faults"]
        f = f[(f.site_id == self.site) & (f.start_ts < config.SIM_END) &
              ((f.cleared_ts >= config.SIM_START) | f.cleared_ts.isna())]
        return f[f.fault_category != "communication"]   # comms faults: site brain keeps control locally

    def actual_cab_factor(self, cab, now):
        """Physics: what the cabinet can really deliver right now."""
        fac = 1.0
        b = self.bookings.get(cab)
        if b and b.arrive <= now < b.done:
            return 0.0
        for _, r in self.faults[(self.faults.cabinet_id == cab) & self.faults.charger_id.isna()].iterrows():
            if b and b.done <= r.start_ts:
                continue   # prevented: repaired before it would have happened
            end = b.done if (b and b.done > r.start_ts) else (r.cleared_ts if not pd.isna(r.cleared_ts) else INF)
            if r.fault_id == "F015":
                end = min(end, pd.Timestamp("2026-11-13T02:25:00+02:00"))
            if r.start_ts <= now < end:
                fac = min(fac, 0.5 if "derated" in r.symptom else 0.0)
        return fac

    def actual_disp_ok(self, ch, now):
        b = self.bookings.get(ch)
        if b and b.arrive <= now < b.done:
            return False
        for _, r in self.faults[self.faults.charger_id == ch].iterrows():
            if b and b.done <= r.start_ts:
                continue
            end = r.cleared_ts if not pd.isna(r.cleared_ts) else INF
            if r.start_ts <= now < end:
                return False
        return ch not in self.p["offline"]

    def actual_grid(self, now):
        lim = self.p["grid_limit_kw"]
        for _, g in self.grid_events.iterrows():
            if g.start_ts <= now < g.end_ts:
                lim = min(lim, g.limit_kw)
        return lim

    # ---------- site brain ----------
    def grid_view(self, slots):
        cap = np.full(len(slots), float(self.p["grid_limit_kw"]))
        for _, g in self.grid_events.iterrows():
            if g.event_id in self.received_events:
                cap[(slots >= g.start_ts) & (slots < g.end_ts)] = min(cap.max(), g.limit_kw)
        return cap

    def cloud_sync(self, now):
        for _, g in self.grid_events.iterrows():
            if g.notified_ts <= now and g.event_id not in self.received_events and g.end_ts > now:
                self.received_events.add(g.event_id)
                self.log.log(now, self.site, "cloud", "grid", f"DSO notice {g.event_id}: cap {g.limit_kw:.0f} kW "
                             f"{g.start_ts:%a %H:%M}-{g.end_ts:%H:%M}; site re-plans", g.note, "no")
        if now.hour == config.PRICE_DAY_AHEAD_PUBLISH_HOUR and now.minute == 0:
            nxt = self.price[(self.price.index >= now.normalize() + pd.Timedelta(days=1)) &
                             (self.price.index < now.normalize() + pd.Timedelta(days=2))]
            spot = load.price_eur_kwh(self.data)["spot_eur_mwh"].reindex(nxt.index)
            hi = spot[spot > 200]
            if len(hi):
                self.log.log(now, self.site, "cloud", "price",
                             f"Day-ahead prices for {nxt.index[0]:%a %d.%m}: spike {hi.index.min():%H:%M}-"
                             f"{hi.index.max() + config.STEP:%H:%M}, max {spot.max():.0f} €/MWh; plan moves charging out",
                             "price event in published day-ahead prices", "no")

    def assess_health(self, now):
        hs = self.hm.site(self.site, now)
        for ch in self.info["chargers"]:
            hs.setdefault(ch, health.Health(ch))   # 2029 redesign outlets: no telemetry, assumed healthy
        out = {}
        for unit, h in hs.items():
            st = h.state
            b = self.bookings.get(unit)
            is_cab = unit in self.info["cab_max"]
            if b and b.arrive <= now < b.done:
                st = "repair"
            elif b and now >= b.done and unit not in self.hm.repaired_at:
                self.hm.mark_repaired(unit, b.done, fan_swapped=(b.problem == "fan"))
                self.log.log(b.done, self.site, "cloud", "repair", f"{unit} repaired by {b.technician_id} ({b.part_id}); back in service",
                             "technician job closed", "yes")
                h = self.hm.cabinet(unit, now) if is_cab else self.hm.dispenser(unit, now)
                st = h.state
            if is_cab and self.actual_cab_factor(unit, now) < 1.0 and st != "repair":
                st = "faulted"
            if not is_cab and not self.actual_disp_ok(unit, now) and st != "repair":
                st = "offline" if unit in self.p["offline"] else "faulted"
            prev = self.state.get(unit, "healthy")
            if st != prev and not (st == "offline" and prev == "offline"):
                self.log.log(now, self.site, "charger" if st == "faulted" else "site", "health",
                             f"{unit} {prev} -> {st}", h.reason if st not in ("faulted", "offline", "repair") else
                             ("fault reported by charger" if st == "faulted" else st), "no")
                if st in ("degrading", "critical") and is_cab:
                    f = (config.FAN_DEGRADING_FACTOR if st == "degrading" else config.FAN_CRITICAL_FACTOR) \
                        if h.problem == "fan" and h.eta_stop is not None else config.HEALTH_FACTOR[st]
                    if f < 1:
                        self.log.log(now, self.site, "site", "derate", f"{unit} limited to {f:.0%} to protect it",
                                     h.reason, "no")
                if st in ("degrading", "critical") and is_cab and h.eta_stop is not None:
                    self.log.log(now, self.site, "site", "plan",
                                 f"Plan: vehicles on {unit} finish by {h.eta_stop - config.TRIP_MARGIN:%a %H:%M}; "
                                 f"no new vehicle there unless it can finish before then",
                                 f"projected trip {h.eta_stop:%a %H:%M} from fan trend", "no")
            self.state[unit] = st
            out[unit] = (st, h)
            needs_fix = (st in ("degrading", "critical") and h.problem is not None) or st == "faulted"
            if needs_fix and unit not in self.bookings:
                self.book_repair(now, unit, h, st)
        return out

    def book_repair(self, now, unit, h, st):
        problem = h.problem or ("fan" if unit in self.info["cab_max"] else "connector_or_module")
        flag = now if self.tech_unavailable_until is None else max(now, self.tech_unavailable_until)
        b = repairs.book(self.data, unit, self.site, problem, flag)
        self.bookings[unit] = b
        why = f"{h.reason}; {b.note}" + ("" if self.tech_unavailable_until is None else
                                         f"; technician unavailable until {self.tech_unavailable_until:%a %H:%M}")
        self.log.log(now, self.site, "cloud", "repair",
                     f"{b.technician_id} booked {b.arrive:%a %d.%m %H:%M}-{b.done:%H:%M} for {unit} with {b.part_id} "
                     f"(no remote-reset-and-wait step)", why, "yes")

    def maybe_night_callout(self, now, unit, shortfall_kwh, at_risk):
        b = self.bookings.get(unit)
        if b is None or b.night_callout or b.arrive <= now or repairs.in_shift(now) or shortfall_kwh < 1:
            return
        if self.tech_unavailable_until is not None and now < self.tech_unavailable_until and not self.oncall_available:
            if unit not in self.callout_refused:
                self.callout_refused.add(unit)
                self.log.log(now, self.site, "cloud", "repair", f"Night call-out for {unit} wanted but no technician available",
                             f"{shortfall_kwh:.0f} kWh at risk for {', '.join(at_risk)}; keep re-planning, risk notice sent", "no")
            return
        # on-call technician (TE2 is on call in week 46) only if it saves a departure before the booked fix
        nb = repairs.book(self.data, unit, self.site, b.problem, now, allow_night=True, technician="TE2")
        if nb.done < b.done:
            self.bookings[unit] = nb
            self.log.log(now, self.site, "cloud", "repair",
                         f"Night call-out: on-call TE2 to {unit}, on site {nb.arrive:%H:%M}, fixed ~{nb.done:%H:%M}",
                         f"re-plan shows {shortfall_kwh:.0f} kWh missing for {', '.join(at_risk)} before the day shift", "yes")

    def factors(self, now, hs):
        cab_factor, disp_factor, usable_until = {}, {}, {}
        for cab in self.info["cab_max"]:
            st, h = hs[cab]
            cab_factor[cab] = config.HEALTH_FACTOR.get(st, 1.0)
            if h.problem == "fan" and h.eta_stop is not None and st in ("degrading", "critical"):
                cab_factor[cab] = config.FAN_DEGRADING_FACTOR if st == "degrading" else config.FAN_CRITICAL_FACTOR
        for ch in self.info["chargers"]:
            st, h = hs[ch]
            disp_factor[ch] = config.HEALTH_FACTOR.get(st, 1.0) if st not in ("offline",) else 0.0
            if ch in self.p["offline"]:
                disp_factor[ch] = 0.0
            cab = self.info["cabinet"][ch]
            u = INF
            if cab_factor[cab] == 0 or disp_factor[ch] == 0:
                u = now
            cst, chh = hs[cab]
            b = self.bookings.get(cab)
            if cst in ("degrading", "critical") and chh.eta_stop is not None and not (b and b.done <= chh.eta_stop):
                u = min(u, chh.eta_stop - config.TRIP_MARGIN)
            if b and b.arrive > now:
                u = min(u, b.arrive)
            usable_until[ch] = u
        self.info["disp_factor"] = disp_factor
        return cab_factor, disp_factor, usable_until

    def cab_cap_forecast(self, now, slots, hs, cab_factor):
        caps = {}
        for cab, mx in self.info["cab_max"].items():
            st, h = hs[cab]
            arr = np.full(len(slots), mx * cab_factor[cab])
            b = self.bookings.get(cab)
            if st in ("degrading", "critical") and h.eta_stop is not None and not (b and b.done <= h.eta_stop):
                arr[slots >= h.eta_stop - config.TRIP_MARGIN] = 0.0
            if b:
                arr[(slots >= b.arrive) & (slots < b.done)] = 0.0
                arr[slots >= b.done] = mx
            caps[cab] = arr
        return caps

    # ---------- bays ----------
    def do_swap(self, now, sw: bays.Swap):
        q = self.visits[sw.in_key]
        cur = self.plugged.get(sw.charger_id)
        if cur is not None:
            v = self.visits[cur]
            if v.soc < v.req - 0.5 and v.depart > now:
                return False   # outgoing bus not ready yet: wait
            v.status, v.charger = "parked", None
        by_driver = (now - q.arrive) <= pd.Timedelta(minutes=config.DRIVER_SWAP_WINDOW_MIN) and not q.needs_move
        q.status, q.charger = "plugged", sw.charger_id
        self.plugged[sw.charger_id] = q.key
        if by_driver and cur is None:
            self.log.log(now, self.site, "site", "bay", f"{q.vid} -> bay {sw.charger_id}",
                         f"arrives {q.soc:.0f} %, leaves {q.depart:%a %H:%M}, needs {q.target:.0f} %", "driver")
        elif by_driver:
            self.moves["driver"] += 1
            self.log.log(now, self.site, "site", "bay", f"{q.vid} -> bay {sw.charger_id}; driver moves full "
                         f"{self.visits[cur].vid} ({self.visits[cur].soc:.0f} %) to parking", "planned swap at arrival", "driver")
        else:
            self.moves["yard"] += 1
            out = f"move full {self.visits[cur].vid} ({self.visits[cur].soc:.0f} %) out, " if cur else ""
            self.log.log(now, self.site, "site", "move", f"Planned yard move: {out}plug {q.vid} into {sw.charger_id}",
                         "more vehicles than dispensers" if not q.needs_move else "vehicle was on a failed bay", "yes")
        q.needs_move = False
        return True

    # ---------- main loop ----------
    def step(self, now):
        self.cloud_sync(now)
        for v in self.visits.values():
            if v.status in ("plugged", "parked", "waiting") and v.depart <= now:
                if v.charger:
                    self.plugged.pop(v.charger, None)
                v.status, v.charger = "gone", None
                if v.depart_in_data:
                    self.rows_dep.append({"vehicle_id": v.vid, "site_id": self.site, "depart_ts": v.depart,
                                          "depart_soc_pct": round(v.soc, 1), "required_soc_pct": v.req,
                                          "below_required": int(v.soc + 1e-6 < v.req)})
        new = [v for v in self.visits.values() if v.status == "future" and v.arrive <= now]
        for v in new:
            v.status = "waiting"
        hs = self.assess_health(now)
        cab_factor, disp_factor, usable_until = self.factors(now, hs)
        # vehicles stuck on a bay that can no longer charge them must be moved
        for ch, k in list(self.plugged.items()):
            v = self.visits[k]
            if usable_until[ch] <= now and v.soc < v.target - 0.5:
                self.plugged.pop(ch)
                v.status, v.charger, v.needs_move = "waiting", None, True
                self.log.log(now, self.site, "site", "move", f"{v.vid} must leave {ch} ({hs[self.info['cabinet'][ch]][0]})",
                             f"at {v.soc:.0f} %, needs {v.target:.0f} %", "no")
        self.opportunistic_plugs(now, usable_until, disp_factor)
        queue = self.planning_queue(now)
        for k in list(self.committed):
            if k not in queue or usable_until.get(self.committed[k][0], now) <= now:
                self.committed.pop(k)
        swaps = bays.plan_swaps(now, self.plugged, queue, self.visits, self.info, cab_factor, usable_until, self.committed)
        did = False
        for sw in sorted(swaps, key=lambda s: (s.at, -self.visits[s.in_key].depart.value)):
            if sw.at <= now and self.visits[sw.in_key].status == "waiting":
                if sw.charger_id in self.plugged and self.plugged[sw.charger_id] != sw.out_key and sw.out_key is not None:
                    continue
                if sw.out_key is None and sw.charger_id in self.plugged:
                    continue
                did |= self.do_swap(now, sw)
        if did:
            queue = self.planning_queue(now)
            for k in list(self.committed):
                if k not in queue:
                    self.committed.pop(k)
            swaps = bays.plan_swaps(now, self.plugged, queue, self.visits, self.info, cab_factor, usable_until, self.committed)
        for sw in swaps:
            self.committed.setdefault(sw.in_key, (sw.charger_id, sw.at))
        for v in new:
            if v.status == "waiting":
                sw = next((s for s in swaps if s.in_key == v.key), None)
                txt = f"{v.vid} -> waiting row; planned bay {sw.charger_id} at {sw.at:%H:%M}" if sw else f"{v.vid} -> waiting row"
                self.log.log(now, self.site, "site", "bay", txt, "all bays busy", "driver")

        # ---- segments for the planner
        segs, first_swap = [], {}
        for sw in sorted(swaps, key=lambda s: s.at):
            first_swap.setdefault(sw.charger_id, []).append(sw)
        for ch, k in self.plugged.items():
            v = self.visits[k]
            end = v.depart
            lst = first_swap.get(ch, [])
            if lst and lst[0].out_key == k:
                end = min(end, lst[0].at)
            segs.append(self._segment(v, ch, now, end, disp_factor))
        for ch, lst in first_swap.items():
            for i, sw in enumerate(lst):
                q = self.visits[sw.in_key]
                end = q.depart
                if i + 1 < len(lst) and lst[i + 1].out_key == q.key:
                    end = min(end, lst[i + 1].at)
                segs.append(self._segment(q, ch, max(sw.at, q.arrive), end, disp_factor))
        segs = [s for s in segs if s.end > now and s.end > s.start]
        horizon_end = min(now + pd.Timedelta(hours=config.PLANNING_HORIZON_H),
                          max([s.end for s in segs], default=now + config.STEP).ceil("15min"))
        slots = pd.date_range(now, max(horizon_end, now + config.STEP) - config.STEP, freq=config.STEP)
        price = price_view(now, self.price, slots)
        caps = self.cab_cap_forecast(now, slots, hs, cab_factor)
        grid = self.grid_view(slots)
        for s in segs:
            s.end = min(s.end, slots[-1] + config.STEP)
        plan, solver = planner.plan(segs, slots, price, caps, grid, self.peak, self.peak_fee)

        # risk check -> notice to the fleet (before 22:00 rule lives in the service agreement)
        shortfall_by_cab, at_risk = {}, {}
        for s in segs:
            v = self.visits[s.vid_key]
            if not v.depart_in_data or s.end < v.depart:
                continue
            final = s.soc + plan.get(s.key, np.zeros(1)).sum() * config.SLOT_H / s.battery * 100
            if final < s.req - 0.5:
                miss = (s.req - final) * s.battery / 100
                shortfall_by_cab[s.cabinet_id] = shortfall_by_cab.get(s.cabinet_id, 0) + miss
                at_risk.setdefault(s.cabinet_id, []).append(v.vid)
                if not v.risk_logged and v.depart > now:
                    v.risk_logged = True
                    self.log.log(now, self.site, "site", "risk",
                                 f"Risk notice: {v.vid} projected {final:.0f} % at {v.depart:%a %H:%M} (needs {s.req:.0f} %)",
                                 "not enough healthy capacity before departure", "yes")
        # night call-out decision for failed cabinets
        tot_short = sum(shortfall_by_cab.values())
        for cab in self.info["cab_max"]:
            if hs[cab][0] == "faulted" and tot_short > 0:
                self.maybe_night_callout(now, cab, tot_short, sorted({x for l in at_risk.values() for x in l}))

        # ---- charger layer executes the first slot
        req = {}
        for s in segs:
            if s.start <= now and s.key in plan and self.visits[s.vid_key].status == "plugged":
                v = self.visits[s.vid_key]
                p = float(plan[s.key][0])
                p = min(p, planner.taper_cap(min(v.vmax, self.info["disp_max"][s.charger_id]), v.soc))
                if not self.actual_disp_ok(s.charger_id, now):
                    p = 0.0
                req[s.key] = (s, p)
        for cab in self.info["cab_max"]:
            limit = self.info["cab_max"][cab] * self.actual_cab_factor(cab, now)
            keys = [k for k, (s, p) in req.items() if s.cabinet_id == cab]
            tot = sum(req[k][1] for k in keys)
            if tot > limit + 1e-6:
                for k in keys:
                    s, p = req[k]
                    req[k] = (s, p * limit / tot)
        tot = sum(p for s, p in req.values())
        glim = self.actual_grid(now)
        if tot > glim + 1e-6:
            for k in req:
                s, p = req[k]
                req[k] = (s, p * glim / tot)
            tot = glim
        site_kw = 0.0
        for k, (s, p) in req.items():
            v = self.visits[s.vid_key]
            e = p * config.SLOT_H
            e = min(e, (100 - v.soc) / 100 * v.battery)
            v.soc += e / v.battery * 100
            site_kw += e / config.SLOT_H
        self.peak = max(self.peak, site_kw)
        self.rows_power.append({"ts": now, "site_id": self.site, "power_kw": round(site_kw, 2),
                                "grid_limit_kw": glim, "planned_grid_cap_kw": grid[0], "solver": solver,
                                "price_eur_kwh": float(self.price.get(now, np.nan))})
        kw_by_ch = {s.charger_id: p for s, p in req.values()}
        for ch in self.info["chargers"]:
            st_h = hs[ch][0]
            cab_st = hs[self.info["cabinet"][ch]][0]
            if ch in self.p["offline"]:
                status = "offline"
            elif not self.actual_disp_ok(ch, now) or self.actual_cab_factor(self.info["cabinet"][ch], now) == 0 \
                    or st_h in ("critical", "repair") or cab_st == "repair":
                status = "faulted"
            elif kw_by_ch.get(ch, 0) > 0.01:
                status = "charging"
            elif ch in self.plugged:
                status = "occupied"
            else:
                status = "available"
            self.rows_status.append({"ts": now, "charger_id": ch, "site_id": self.site, "status": status})
            k = self.plugged.get(ch)
            self.rows_bay.append({"ts": now, "site_id": self.site, "charger_id": ch, "cabinet_id": self.info["cabinet"][ch],
                                  "vehicle_id": self.visits[k].vid if k else "", "soc": round(self.visits[k].soc, 1) if k else None,
                                  "power_kw": round(kw_by_ch.get(ch, 0.0), 1), "state": cab_st if cab_st != "healthy" else st_h})
        for cab in self.info["cab_max"]:
            st, h = hs[cab]
            self.rows_health.append({"ts": now, "site_id": self.site, "unit": cab, "state": st,
                                     "score": config.HEALTH_SCORE.get(st, 100), "reason": h.reason,
                                     "eta_stop": h.eta_stop})

    def opportunistic_plugs(self, now, usable_until, disp_factor):
        """A bay that is free now, fully usable, and not promised to any vehicle (now or later tonight) is never
        left idle while a vehicle that has already arrived is waiting: earliest departure gets it.
        (Typical case: a bay comes back from repair in the middle of the night.)"""
        promised = {c for k, (c, at) in self.committed.items()}
        waiting = sorted((v for v in self.visits.values() if v.status == "waiting"), key=lambda v: v.depart)
        for c in self.info["chargers"]:
            if not waiting:
                break
            if c in self.plugged or c in promised or disp_factor.get(c, 1.0) == 0:
                continue
            if usable_until.get(c, now) < waiting[0].depart:
                continue
            q = waiting.pop(0)
            self.committed.pop(q.key, None)
            self.do_swap(now, bays.Swap(c, None, q.key, now, q.depart, True))

    def planning_queue(self, now):
        """Vehicles waiting now plus those the fleet schedule says will arrive within the horizon:
        the site brain plans the whole night when the first bus arrives, not bus by bus."""
        horizon = now + pd.Timedelta(hours=config.SCHEDULE_LOOKAHEAD_H)
        return [k for k, v in self.visits.items()
                if v.status == "waiting" or (v.status == "future" and v.arrive < horizon)]

    def _segment(self, v, ch, start, end, disp_factor):
        seg = planner.Segment(key=f"{v.key}|{ch}", vid=v.vid, charger_id=ch, cabinet_id=self.info["cabinet"][ch],
                              start=start, end=end, soc=v.soc, battery=v.battery,
                              pmax=min(v.vmax, self.info["disp_max"][ch] * max(disp_factor.get(ch, 1.0), 0.0)),
                              req=v.req, target=v.target)
        seg.vid_key = v.key
        return seg

    def run(self):
        for now in pd.date_range(config.SIM_START, config.SIM_END - config.STEP, freq=config.STEP):
            self.step(now)
        return {
            "power": pd.DataFrame(self.rows_power), "status": pd.DataFrame(self.rows_status),
            "departures": pd.DataFrame(self.rows_dep), "bays": pd.DataFrame(self.rows_bay),
            "health": pd.DataFrame(self.rows_health), "log": self.log.frame(), "moves": dict(self.moves),
            "bookings": [b.__dict__ for b in self.bookings.values()],
        }


# ------------------------------------------------------------------------------------------------
def run_scenario(data, mode="autopilot", s5_config="fixed", outlets=None, sites=None, verbose=True):
    sites = sites or config.ALL_SITES
    res = {}
    for s in sites:
        t = time.time()
        if s in config.DEPOT_SITES:
            res[s] = DepotSim(data, s, mode, s5_config, (outlets or {}).get(s)).run()
        else:
            res[s] = public.PublicSim(data, s, policy="autopilot").run()
        if verbose:
            print(f"  {mode:10s} {s}: {time.time() - t:5.1f} s")
    return combine(res)


def combine(res):
    out = {}
    for key in ["power", "status", "departures", "bays", "health", "log", "public"]:
        frames = [r[key] for r in res.values() if key in r and r[key] is not None and len(r[key])]
        out[key] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out["moves"] = {s: r.get("moves", {}) for s, r in res.items()}
    out["bookings"] = [b for r in res.values() for b in r.get("bookings", [])]
    if len(out["log"]):
        out["log"] = out["log"].sort_values("ts", kind="stable").reset_index(drop=True)
    return out


def scenario_kpis(data, out):
    pub = out["public"] if len(out.get("public", [])) else None
    return kpis.compute_kpis(data, out["power"][["ts", "site_id", "power_kw"]], out["departures"], out["status"], pub)


def save(out, name):
    d = os.path.join(config.OUT_DIR, name)
    os.makedirs(d, exist_ok=True)
    for key in ["power", "status", "departures", "bays", "health", "log", "public"]:
        if len(out.get(key, [])):
            out[key].to_csv(os.path.join(d, f"{key}.csv"), index=False)
    pd.DataFrame(out["bookings"]).to_csv(os.path.join(d, "bookings.csv"), index=False)
    pd.DataFrame([{"site_id": s, **m} for s, m in out["moves"].items()]).to_csv(os.path.join(d, "moves.csv"), index=False)


def main():
    from src import report
    data = load.load_all()
    os.makedirs(config.OUT_DIR, exist_ok=True)
    runs = {
        "autopilot": dict(mode="autopilot"),
        "worst_case": dict(mode="worst_case"),
        "worst_case_oncall": dict(mode="worst_case_oncall", sites=["S1"]),
        "autopilot_s5_as_configured": dict(mode="autopilot", s5_config="as_configured", sites=["S5"]),
        "redesign_2029_outlets": dict(mode="autopilot", outlets={"S1": 24, "S5": 12}, sites=["S1", "S5"]),
    }
    outs = {}
    for name, kw in runs.items():
        print(f"Running {name} ...")
        outs[name] = run_scenario(data, **kw)
        save(outs[name], name)
    print("Baseline-rules replay of the public hubs (calibration) ...")
    outs["public_baseline_rules"] = combine({s: public.PublicSim(data, s, policy="baseline").run() for s in config.PUBLIC_SITES})
    save(outs["public_baseline_rules"], "public_baseline_rules")
    report.write_all(data, outs)


if __name__ == "__main__":
    main()
