import pandas as pd
import pytest

from src import health, load

T = lambda s: pd.Timestamp(s + "+02:00")


@pytest.fixture(scope="module")
def model():
    return health.HealthModel(load.load_all())


def test_c1_3_degrading_on_thursday_working_hours(model):
    """Fan must be flagged while TE1 is still on shift on Thursday (before 16:00)."""
    flags = health.first_flag_times(model, "C1-3", T("2026-11-12T00:00"), T("2026-11-12T16:00"), "cabinet")
    assert "degrading" in flags
    assert flags["degrading"][0] < T("2026-11-12T16:00")


def test_c1_3_trip_eta_close_to_real_failure(model):
    h = model.cabinet("C1-3", T("2026-11-12T12:00"))
    assert h.eta_stop is not None
    assert abs((h.eta_stop - T("2026-11-13T02:25")).total_seconds()) < 3 * 3600


def test_d4_2_flagged_before_lockout(model):
    flags = health.first_flag_times(model, "D4-2", T("2026-11-13T12:00"), T("2026-11-13T21:29"), "dispenser")
    assert "degrading" in flags and flags["degrading"][0] <= T("2026-11-13T21:00")


def test_d3_3_warning_well_ahead(model):
    flags = health.first_flag_times(model, "D3-3", T("2026-11-10T12:00"), T("2026-11-11T16:39"), "dispenser")
    lead = (T("2026-11-11T16:40") - flags["watch"][0]).total_seconds() / 3600
    assert lead > 12


def test_healthy_cabinets_not_degrading(model):
    for cab in ["C1-1", "C1-2", "C2-1", "C2-2", "C5-1", "C5-2"]:
        flags = health.first_flag_times(model, cab, T("2026-11-12T00:00"), T("2026-11-14T00:00"), "cabinet", "1h")
        assert "degrading" not in flags, cab


def test_no_future_leakage(model):
    """The assessment at time t must not change if all data from t onwards is removed."""
    t = T("2026-11-12T10:00")
    a = model.cabinet("C1-3", t)
    d = load.load_all()
    cut = dict(d)
    cut["telemetry_1min"] = d["telemetry_1min"][d["telemetry_1min"].ts < t]
    cut["telemetry"] = d["telemetry"][d["telemetry"].ts < t]
    b = health.HealthModel(cut).cabinet("C1-3", t)
    assert (a.state, a.reason, a.eta_stop) == (b.state, b.reason, b.eta_stop)
    t2 = T("2026-11-13T20:30")
    cut["telemetry_1min"] = d["telemetry_1min"][d["telemetry_1min"].ts < t2]
    cut["telemetry"] = d["telemetry"][d["telemetry"].ts < t2]
    assert model.dispenser("D4-2", t2).state == health.HealthModel(cut).dispenser("D4-2", t2).state
