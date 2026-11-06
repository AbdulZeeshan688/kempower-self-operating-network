"""Which vehicle goes to which dispenser, and when (planned) swaps happen.

Today, night staff move vehicles whenever a bus is full and another is waiting, and again when
a charger fails. Here the site brain:
  * gives every arriving vehicle a bay at once, so most never need moving: the vehicles leaving
    soonest get the healthiest dispensers with the most cabinet headroom, and a weak cabinet is
    only used if nothing healthy is free;
  * when there are more vehicles than dispensers, plans each swap in advance (which bus, which
    bay, what time) so the planner can make sure the outgoing bus is full by then;
  * tells the driver where to park ("B12 -> bay D1-4").
A plug-in done by the arriving driver needs no extra staff. Any later plug-in is a yard move.
"""
from dataclasses import dataclass

import pandas as pd

import config


@dataclass
class Swap:
    charger_id: str
    out_key: str | None      # vehicle currently on the bay (None = bay free / occupant leaves first)
    in_key: str
    at: pd.Timestamp
    finish: pd.Timestamp
    on_time: bool


def fair_share_kw(charger, info, cab_factor, vmax):
    """Conservative charging power for a projection: cabinet power split over its dispensers."""
    cab = info["cabinet"][charger]
    share = info["cab_max"][cab] * cab_factor.get(cab, 1.0) / info["n_on_cab"][cab]
    return max(1e-3, min(info["disp_max"][charger], vmax, share))


def need_kwh(v, to="target"):
    goal = v.target if to == "target" else v.req
    return max(0.0, goal - v.soc) * v.battery / 100


def rank_free(free, info, cab_factor, plugged_by_cab):
    """Healthiest first, then the cabinet with the fewest vehicles on it, then dispenser order."""
    def key(c):
        cab = info["cabinet"][c]
        return (-cab_factor.get(cab, 1.0) * info["disp_factor"].get(c, 1.0), plugged_by_cab.get(cab, 0), info["order"][c])
    return sorted(free, key=key)


def plan_swaps(now, plugged: dict, queue: list, visits: dict, info, cab_factor, usable_until,
               committed: dict | None = None) -> list[Swap]:
    """Greedy timeline: each waiting vehicle (earliest departure first) gets the bay that frees up first
    and still lets it finish before it leaves (and before that bay's cabinet is expected to fail).

    Swaps already promised (`committed`: vehicle -> (bay, time)) keep their bay and time. Without this the
    swap time slides later every step (the outgoing bus is always "3.6 h from full"), the planner keeps
    deferring it to cheaper hours, and the waiting bus runs out of time."""
    committed = committed or {}
    ready, occupant = {}, {}
    for c in info["chargers"]:
        if usable_until.get(c, now) <= now:
            continue
        if c in plugged:
            v = visits[plugged[c]]
            p = fair_share_kw(c, info, cab_factor, v.vmax)
            # with a queue, a bus gives up its bay once it has its *required* SoC (margin is a bonus)
            t_full = now + pd.Timedelta(hours=need_kwh(v, "req") / p * config.SWAP_POWER_MARGIN)
            ready[c] = min(t_full, v.depart)
            occupant[c] = v.key if t_full < v.depart else None
        else:
            ready[c], occupant[c] = now, None
    swaps = []
    for q in sorted((visits[k] for k in queue if k in committed and committed[k][0] in ready),
                    key=lambda v: committed[v.key][1]):
        c, at = committed[q.key]
        start = max(now, at, q.arrive.ceil("15min"))
        p = fair_share_kw(c, info, cab_factor, q.vmax)
        finish = start + pd.Timedelta(hours=need_kwh(q, "req") / p * config.SWAP_POWER_MARGIN)
        swaps.append(Swap(c, occupant[c], q.key, start, finish, finish <= q.depart))
        ready[c] = max(ready[c], min(finish, q.depart))
        occupant[c] = q.key if finish < q.depart else None
    for q in sorted((visits[k] for k in queue if not (k in committed and committed[k][0] in ready)),
                    key=lambda v: v.depart):
        best = None
        for c in ready:
            start = max(ready[c], q.arrive, now).ceil("15min")
            p = fair_share_kw(c, info, cab_factor, q.vmax)
            finish = start + pd.Timedelta(hours=need_kwh(q, "req") / p * config.SWAP_POWER_MARGIN)
            ok = finish <= min(q.depart, usable_until.get(c, pd.Timestamp.max.tz_localize("UTC")))
            score = (0 if ok else 1, start if ok else finish, info["order"][c])
            if best is None or score < best[0]:
                best = (score, c, start, finish, ok)
        if best is None:
            continue
        _, c, start, finish, ok = best
        swaps.append(Swap(c, occupant[c], q.key, start, finish, ok))
        ready[c] = min(finish, q.depart)
        occupant[c] = q.key if finish < q.depart else None
    return swaps
