"""Repair booking: pick the part, check stock, book a technician inside shift hours.

The physical fix still needs a person. What we automate is everything around it: diagnosis,
part check, booking, and skipping the "remote reset, wait 2 h, then dispatch" routine for faults
the health model already explains.
"""
from dataclasses import dataclass

import pandas as pd

import config


@dataclass
class Booking:
    unit: str
    site_id: str
    problem: str
    part_id: str
    technician_id: str
    flagged: pd.Timestamp
    depart: pd.Timestamp
    arrive: pd.Timestamp
    done: pd.Timestamp
    cost_eur: float
    night_callout: bool
    note: str


def ticket_durations(data) -> dict:
    """Average on-site time (arrived -> closed) per part, from today's tickets. Multi-day tickets are left out."""
    t = data["service_tickets"].dropna(subset=["arrived_ts", "part_id"]).copy()
    t["min"] = (t.closed_ts - t.arrived_ts).dt.total_seconds() / 60
    t = t[t["min"] < 8 * 60]
    return t.groupby("part_id")["min"].mean().to_dict()


def in_shift(ts: pd.Timestamp) -> bool:
    return ts.weekday() in config.SHIFT_DAYS and config.SHIFT_START_H <= ts.hour < config.SHIFT_END_H


def next_shift_start(ts: pd.Timestamp) -> pd.Timestamp:
    t = ts.ceil("15min")
    while not in_shift(t):
        t = (t + pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=config.SHIFT_START_H) \
            if t.hour >= config.SHIFT_END_H or t.weekday() not in config.SHIFT_DAYS \
            else t.normalize() + pd.Timedelta(hours=config.SHIFT_START_H)
    return t


def candidate_parts(problem: str) -> list[str]:
    if problem == "fan":
        return ["P_FAN"]
    # Error events alone do not tell cable from power module: carry the local part, pre-order the other.
    return ["P_CABLE_CCS2", "P_POWER_MODULE"]


def book(data, unit, site_id, problem, flagged: pd.Timestamp, allow_night=False, technician="TE1",
         part_used=None) -> Booking:
    parts = data["spare_parts"].set_index("part_id")
    tech = data["technicians"].set_index("technician_id").loc[technician]
    travel = data["travel_times"].query("technician_id == @technician and site_id == @site_id").travel_min.iloc[0]
    durations = ticket_durations(data)
    cands = candidate_parts(problem)
    part = part_used or cands[0]
    local = [p for p in cands if parts.loc[p, "stock_location"] == "Local service base" and parts.loc[p, "stock_qty"] > 0]
    ready = flagged
    if part not in local:
        ready = flagged + pd.Timedelta(days=int(parts.loc[part, "lead_time_days"]))
    dur = pd.Timedelta(minutes=durations.get(part, 120))
    tr = pd.Timedelta(minutes=int(travel))
    if allow_night:
        depart = ready.ceil("15min")
    else:
        depart = next_shift_start(ready)
        # the whole job must fit in the shift; otherwise next shift
        while not in_shift(depart + tr + dur - pd.Timedelta("1min")):
            depart = next_shift_start(depart.normalize() + pd.Timedelta(days=1))
    arrive = depart + tr
    done = arrive + dur
    hours = (2 * tr + dur).total_seconds() / 3600
    cost = round(hours * float(tech.hourly_cost_eur) + float(parts.loc[part, "unit_cost_eur"]), 1)
    note = (f"{part} ({parts.loc[part, 'stock_location']}, {parts.loc[part, 'stock_qty']} in stock)"
            + (f"; also carrying/pre-ordering {', '.join(c for c in cands if c != part)}" if len(cands) > 1 else ""))
    return Booking(unit, site_id, problem, part, technician, flagged, depart, arrive, done, cost,
                   night_callout=allow_night and not in_shift(depart), note=note)
