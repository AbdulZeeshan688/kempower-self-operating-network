"""Smoke test: the dashboard renders without exceptions for every run toggle and site."""
import os

import pytest

import config

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.mark.skipif(not os.path.exists(os.path.join(config.OUT_DIR, "kpi_comparison.csv")),
                    reason="run python -m src.simulate first")
@pytest.mark.parametrize("run_idx", [0, 1, 2])
@pytest.mark.parametrize("site", ["S1", "S2", "S3"])
def test_dashboard_renders(run_idx, site):
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.run()
    at.sidebar.radio[0].set_value(at.sidebar.radio[0].options[run_idx]).run()
    at.selectbox[0].set_value(site).run()
    assert not at.exception, at.exception
