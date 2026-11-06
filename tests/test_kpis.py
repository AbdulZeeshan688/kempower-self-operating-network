import numpy as np
import pandas as pd
import pytest

import config
from src import kpis, load


@pytest.fixture(scope="module")
def data():
    return load.load_all()


@pytest.mark.parametrize("period,start,end", [
    ("scenario_day", config.DAY_START, config.DAY_END),
    ("all_14_days", pd.Timestamp("2026-11-02T00:00:00+02:00"), pd.Timestamp("2026-11-16T00:00:00+02:00")),
])
def test_kpis_reproduce_baseline(data, period, start, end):
    ours = kpis.baseline_from_data(data, start, end).set_index("site_id")
    ref = data["baseline_kpis"]
    ref = ref[ref.period == period].set_index("site_id")
    for col in ["departures", "departures_below_required_soc", "public_arrivals", "public_abandoned"]:
        assert (ours[col] == ref[col]).all(), col
    assert np.allclose(ours["energy_mwh"], ref["energy_mwh"], atol=0.01)
    assert np.allclose(ours["energy_cost_eur"], ref["energy_cost_eur"], atol=1.0)
    assert np.allclose(ours["peak_kw"], ref["peak_kw"], atol=0.5)
    assert np.allclose(ours["charger_availability_pct"], ref["charger_availability_pct"], atol=0.06)
