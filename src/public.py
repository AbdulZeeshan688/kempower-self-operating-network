"""Public hubs (S3, S4): 1-minute queue simulation on the real public arrivals.

Same engine, two rule sets, so the comparison is like for like:
  baseline   today's rules: when the site loses the cloud each dispenser falls back to 50 kW; drivers take the
             first free dispenser; a dispenser keeps being offered until it trips.
  autopilot  the site brain controls power locally, so a cloud outage changes nothing; drivers are steered
             to the healthiest, right-sized dispenser; a dispenser whose health turns 'degrading' is taken out
             of rotation (cable risk: safety first) before it trips mid-session.
Driver behaviour (from the data): wants energy_wanted_kwh, waits at most max_wait_min, drives on if the queue is
already PUBLIC_QUEUE_MAX long. Charging power per vehicle class = median average session power in the baseline
sessions (this already includes taper). Calibration result is printed by the report and in docs/assumptions.md.
"""
import numpy as np
import pandas as pd

import config
from src import events, health, load, repairs
from src.commissioning_check import site_parameters

START = pd.Timestamp("2026-11-12T00:00:00+02:00")


def class_power(data, site) -> dict:
    ss = data["sessions_baseline"]
    pa = data["public_arrivals"]
    ss = ss[ss.site_id == site].dropna(subset=["anon_user_id"]).copy()
    ss["arrival_id"] = "A" + ss.anon_user_id.str[5:]
    m = ss.merge(pa[["arrival_id", "vehicle_class"]], on="arrival_id")
    m = m[m.end_reason == "completed"]
    hrs = (m.end_ts - m.start_ts).dt.total_seconds() / 3600
    m["avg_kw"] = m.energy_kwh / hrs.where(hrs > 0)
    out = m.groupby("vehicle_class").avg_kw.median().to_dict()
    for k, v in config.PUBLIC_VEHICLE_MAX_KW.items():
        out.setdefault(k, v * 0.6)
    return out


class PublicSim:
    def __init__(self, data, site, policy="autopilot"):
        self.data, self.site, self.policy = data, site, policy
        self.p = site_parameters(data, site)
        self.info = self.p["info"]
        self.chargers = self.info["chargers"]
        self.kw = class_power(data, site)
        pa = data["public_arrivals"]
        self.arr = pa[(pa.site_id == site) & (pa.ts >= START) & (pa.ts < config.SIM_END)].sort_values("ts").reset_index(drop=True)
        co = data["connectivity_outages"]
        self.outages = co[(co.site_id == site) & (co.end_ts > START) & (co.start_ts < config.SIM_END)]
        f = data["faults"]
        self.faults = f[(f.site_id == site) & (f.start_ts < config.SIM_END) & ((f.cleared_ts > START) | f.cleared_ts.isna())]
        pv = data["pv_production"]
        self.pv = pv[pv.site_id == site].set_index("ts")["pv_kw"]
        self.hm = health.HealthModel(data)
        self.log = events.DecisionLog()
        self.out_of_rotation: dict[str, str] = {}
        self.booked = set()
        self.health_state = {}

    def offline(self, t):
        return any((o.start_ts <= t < o.end_ts) for o in self.outages.itertuples())

    def fault_blocks(self, ch, t):
        for r in self.faults[self.faults.charger_id == ch].itertuples():
            end = r.cleared_ts if not pd.isna(r.cleared_ts) else pd.Timestamp("2100-01-01T00:00:00+02:00")
            if r.start_ts <= t < end:
                if r.fault_category == "communication" and self.policy == "autopilot":
                    return False   # site brain keeps the dispenser running on the local network
                return True
        return False

    def site_brain_health(self, t):
        """Every 15 min: health per dispenser; take degrading ones out of rotation, book the repair."""
        for ch in self.chargers:
            h = self.hm.dispenser(ch, t)
            prev = self.health_state.get(ch, "healthy")
            if h.state != prev:
                self.log.log(t, self.site, "site", "health", f"{ch} {prev} -> {h.state}", h.reason, "no")
                self.health_state[ch] = h.state
            if h.state in ("degrading", "critical") and ch not in self.out_of_rotation and not self.fault_blocks(ch, t):
                self.out_of_rotation[ch] = h.state
                self.log.log(t, self.site, "site", "lockout",
                             f"{ch} taken out of rotation: current session finishes at reduced power, no new sessions",
                             f"{h.reason}; connector/cable risk, safety first", "no")
            if h.state in ("degrading", "critical") and ch not in self.booked:
                self.booked.add(ch)
                b = repairs.book(self.data, ch, self.site, h.problem or "connector_or_module", t)
                self.log.log(t, self.site, "cloud", "repair",
                             f"{b.technician_id} booked {b.arrive:%a %d.%m %H:%M}-{b.done:%H:%M} for {ch} with {b.part_id}",
                             f"{b.note}; 7 of 8 dispensers stay in service, so no night/weekend call-out", "yes")

    def usable(self, ch, t):
        if self.fault_blocks(ch, t):
            return False
        if self.policy == "autopilot" and ch in self.out_of_rotation:
            return False
        return True

    def choose(self, free, vclass, load_by_cab):
        if self.policy == "baseline":
            return free[0]
        need = self.kw.get(vclass, 100)

        def key(c):
            cab = self.info["cabinet"][c]
            dmax = self.info["disp_max"][c]
            fits = dmax >= need
            headroom = self.info["cab_max"][cab] - load_by_cab.get(cab, 0)
            return (not fits, dmax if fits else -dmax, -headroom, self.info["order"][c])
        return sorted(free, key=key)[0]

    def run(self):
        minutes = pd.date_range(START, config.SIM_END - pd.Timedelta("1min"), freq="1min")
        sess = {}            # charger -> dict(arrival idx, delivered)
        queue = []           # arrival indices
        outcome = {}
        delivered = np.zeros(len(self.arr))
        ai = 0
        power_min = []
        status_rows = []
        was_offline = False
        cls = self.arr.vehicle_class.values
        want = self.arr.energy_wanted_kwh.values
        maxwait = self.arr.max_wait_min.values
        ats = self.arr.ts.values
        for t in minutes:
            off = self.offline(t)
            if off != was_offline and self.policy == "autopilot":
                self.log.log(t, self.site, "site", "connectivity",
                             "Cloud unreachable: site brain runs on its last plan, full power within grid limit" if off
                             else "Cloud back: decision log and telemetry uploaded", "connectivity outage" if off else "reconnected", "no")
            if off != was_offline and self.policy == "baseline":
                self.log.log(t, self.site, "charger", "connectivity",
                             f"Backend lost: dispensers fall back to {self.p['fallback_kw']:.0f} kW" if off else "Backend back",
                             "today's offline fallback", "no")
            was_offline = off
            if t.minute % 15 == 0 and self.policy == "autopilot":
                self.site_brain_health(t)
            # sessions interrupted by a fault
            for ch in list(sess):
                if self.fault_blocks(ch, t):
                    i = sess.pop(ch)["i"]
                    outcome[i] = "left_after_interruption" if delivered[i] < want[i] - 0.5 else "charged"
                    self.log.log(t, self.site, "charger", "fault", f"{ch} tripped during a session", "fault", "no")
            # arrivals this minute
            while ai < len(self.arr) and ats[ai] < (t + pd.Timedelta("1min")).to_datetime64():
                queue.append(ai)
                ai += 1
            # drivers who waited too long
            for i in list(queue):
                if (t.to_datetime64() - ats[i]) > np.timedelta64(int(maxwait[i]), "m"):
                    queue.remove(i)
                    outcome[i] = "left_queue"
            # assign free dispensers (FIFO)
            load_by_cab = {}
            for ch, s in sess.items():
                cab = self.info["cabinet"][ch]
                load_by_cab[cab] = load_by_cab.get(cab, 0) + self.kw.get(cls[s["i"]], 100)
            free = [c for c in self.chargers if c not in sess and self.usable(c, t)]
            while queue and free:
                i = queue.pop(0)
                c = self.choose(free, cls[i], load_by_cab)
                free.remove(c)
                sess[c] = {"i": i}
                cab = self.info["cabinet"][c]
                load_by_cab[cab] = load_by_cab.get(cab, 0) + self.kw.get(cls[i], 100)
            # drive on if the queue is too long (only fresh arrivals still in queue this minute)
            for i in list(queue):
                if ats[i] >= t.to_datetime64() and queue.index(i) >= config.PUBLIC_QUEUE_MAX:
                    queue.remove(i)
                    outcome[i] = "left_full"
            # power allocation
            req = {}
            for ch, s in sess.items():
                p = min(self.kw.get(cls[s["i"]], 100), self.info["disp_max"][ch])
                if ch in self.out_of_rotation and self.policy == "autopilot":
                    p *= 0.5
                if off and self.policy == "baseline":
                    p = min(p, self.p["fallback_kw"])
                req[ch] = p
            for cab, mx in self.info["cab_max"].items():
                ks = [c for c in req if self.info["cabinet"][c] == cab]
                tot = sum(req[c] for c in ks)
                if tot > mx:
                    for c in ks:
                        req[c] *= mx / tot
            pv = float(self.pv.asof(t)) if len(self.pv) else 0.0
            lim = self.p["grid_limit_kw"] + pv
            tot = sum(req.values())
            if tot > lim:
                for c in req:
                    req[c] *= lim / tot
                tot = lim
            for ch, p in req.items():
                i = sess[ch]["i"]
                delivered[i] += p / 60
                if delivered[i] >= want[i] - 1e-6:
                    delivered[i] = want[i]
                    outcome[i] = "charged"
                    sess.pop(ch)
            power_min.append((t, max(0.0, tot - pv)))
            if t.minute % 15 == 0:
                for ch in self.chargers:
                    blocked = not self.usable(ch, t) if self.policy == "autopilot" else self.fault_blocks(ch, t)
                    st = "faulted" if blocked else ("charging" if ch in sess else "available")
                    status_rows.append({"ts": t, "charger_id": ch, "site_id": self.site, "status": st})
        for i in queue:
            outcome.setdefault(i, "period_end")
        for ch, s in sess.items():
            outcome.setdefault(s["i"], "period_end")
        pm = pd.DataFrame(power_min, columns=["ts", "power_kw"]).set_index("ts")["power_kw"].resample("15min").mean()
        price = load.price_eur_kwh(self.data)["T_PUBLIC"]
        power = pd.DataFrame({"ts": pm.index, "site_id": self.site, "power_kw": pm.values.round(2),
                              "grid_limit_kw": self.p["grid_limit_kw"], "planned_grid_cap_kw": self.p["grid_limit_kw"],
                              "solver": "realtime", "price_eur_kwh": price.reindex(pm.index).values})
        pub = self.arr[["arrival_id", "site_id", "ts", "vehicle_class", "energy_wanted_kwh"]].copy()
        pub["outcome"] = [outcome.get(i, "period_end") for i in range(len(pub))]
        pub["energy_delivered_kwh"] = delivered.round(2)
        return {"power": power, "status": pd.DataFrame(status_rows), "public": pub, "log": self.log.frame(),
                "departures": pd.DataFrame(), "moves": {}, "bookings": []}


if __name__ == "__main__":
    import time
    d = load.load_all()
    for s in config.PUBLIC_SITES:
        for pol in ["baseline", "autopilot"]:
            t0 = time.time()
            r = PublicSim(d, s, pol).run()
            pub = r["public"]
            for day in [12, 13]:
                p = pub[pub.ts.dt.day == day]
                ab = p.outcome.isin(["left_full", "left_queue", "left_after_interruption"]).sum()
                real = d["public_arrivals"]
                real = real[(real.site_id == s) & (real.ts.dt.day == day)]
                rab = real.outcome.isin(["left_full", "left_queue", "left_after_interruption"]).sum()
                print(f"{s} {pol:9s} day {day}: abandoned {ab:3d} (data {rab:3d}), "
                      f"energy {r['power'][r['power'].ts.dt.day == day].power_kw.sum() * .25 / 1000:.2f} MWh  [{time.time() - t0:.1f}s]")
