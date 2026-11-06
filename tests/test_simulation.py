"""Checks on the saved simulation output (run `python -m src.simulate` first)."""
import os

import pandas as pd
import pytest

import config
from src import load
from src.commissioning_check import site_parameters

OUT = config.OUT_DIR
T = lambda s: pd.Timestamp(s + "+02:00")


def _read(run, name):
    p = os.path.join(OUT, run, f"{name}.csv")
    if not os.path.exists(p):
        pytest.skip("run python -m src.simulate first")
    df = pd.read_csv(p)
    for c in ("ts", "depart_ts"):
        if c in df:
            df[c] = pd.to_datetime(df[c], format="ISO8601")
    return df


def test_oncall_variant_saves_all_departures():
    dep = _read("worst_case_oncall", "departures")
    fri = dep[(dep.depart_ts >= config.DAY_START) & (dep.depart_ts < config.DAY_END)]
    assert fri.below_required.sum() == 0


@pytest.mark.parametrize("run", ["autopilot", "worst_case"])
def test_s2_respects_grid_cap(run):
    p = _read(run, "power")
    cap = p[(p.site_id == "S2") & (p.ts >= T("2026-11-13T16:00")) & (p.ts < T("2026-11-13T19:00"))]
    assert len(cap) == 12
    assert cap.power_kw.max() <= 400 + 1e-6


@pytest.mark.parametrize("run", ["autopilot", "worst_case"])
def test_site_power_never_above_grid_limit(run):
    data = load.load_all()
    p = _read(run, "power")
    assert (p.power_kw <= p.grid_limit_kw + 1e-6).all()
    for s in config.ALL_SITES:
        real = float(data["sites"].set_index("site_id").loc[s, "grid_limit_kw"])
        assert p[p.site_id == s].power_kw.max() <= real + 1e-6


def test_s3_keeps_charging_during_cloud_outage():
    p = _read("autopilot", "power")
    out = p[(p.site_id == "S3") & (p.ts >= T("2026-11-13T11:00")) & (p.ts < T("2026-11-13T13:00"))]
    fallback = 50 * 9   # today's offline fallback x working dispensers
    assert (out.power_kw > 0).all()
    assert out.power_kw.max() > fallback


def test_price_spike_avoided_at_depots():
    """No depot energy is bought in any slot where the spot price is above 200 €/MWh (Fri 17:15-18:45)."""
    data = load.load_all()
    p = _read("autopilot", "power")
    spot = load.price_eur_kwh(data)["spot_eur_mwh"]
    spike_slots = spot[spot > 200].index
    spike = p[p.ts.isin(spike_slots) & p.site_id.isin(config.DEPOT_SITES)]
    assert len(spike) > 0
    assert spike.power_kw.sum() * 0.25 < 1.0   # kWh


def test_autopilot_beats_baseline_on_departures():
    data = load.load_all()
    dep = _read("autopilot", "departures")
    fri = dep[(dep.depart_ts >= config.DAY_START) & (dep.depart_ts < config.DAY_END)]
    base = data["baseline_kpis"].query("period=='scenario_day'").departures_below_required_soc.sum()
    assert fri.below_required.sum() < base
    assert len(fri) == data["baseline_kpis"].query("period=='scenario_day'").departures.sum()


def test_cabinet_limits_respected():
    data = load.load_all()
    b = _read("autopilot", "bays")
    cab_max = data["power_cabinets"].set_index("cabinet_id").max_kw
    per = b.groupby(["ts", "cabinet_id"]).power_kw.sum().reset_index()
    per["max"] = per.cabinet_id.map(cab_max)
    assert (per.power_kw <= per["max"] + 0.5).all()   # bay powers are rounded to 0.1 kW per dispenser


def test_decision_log_has_required_columns():
    lg = _read("autopilot", "log")
    assert list(lg.columns) == ["ts", "site_id", "layer", "type", "decision", "reason", "human_needed"]
    assert set(lg.layer) <= {"charger", "site", "cloud"}


def test_s5_config_mismatches_found():
    from src.commissioning_check import mismatches
    m = mismatches(load.load_all())
    assert {"grid_limit_kw", "max_kw", "sim_active"} <= set(m.parameter)
    p = site_parameters(load.load_all(), "S5", "as_configured")
    assert p["grid_limit_kw"] == 600 and "D5-8" in p["offline"] and p["info"]["disp_max"]["D5-7"] == 50
