"""Commissioning check: compare how a site is configured (site_config.csv) with what is installed
(sites.csv, chargers.csv). Run automatically at handover so a new site never runs on wrong settings.

Also builds the site parameters the simulation uses, either "as configured today" or "fixed".
"""
import pandas as pd

import config


def mismatches(data) -> pd.DataFrame:
    sc = data["site_config"]
    sites = data["sites"].set_index("site_id")
    ch = data["chargers"].set_index("charger_id")
    rows = []
    for _, r in sc.iterrows():
        if r.parameter == "grid_limit_kw":
            real = sites.loc[r.site_id, "grid_limit_kw"]
            if float(r.configured_value) != float(real):
                rows.append((r.site_id, "", r.parameter, r.configured_value, real,
                             f"Site limited to {r.configured_value} kW, connection allows {real} kW"))
        elif r.parameter == "max_kw":
            real = ch.loc[r.charger_id, "max_kw"]
            if float(r.configured_value) != float(real):
                rows.append((r.site_id, r.charger_id, r.parameter, r.configured_value, real,
                             f"Dispenser capped at {r.configured_value} kW, rated {real} kW"))
        elif r.parameter == "sim_active" and str(r.configured_value).lower() != "true":
            rows.append((r.site_id, r.charger_id, r.parameter, r.configured_value, "true",
                         "SIM card not activated: dispenser never connects to the backend (offline)"))
    fw = sc[sc.parameter == "firmware_version"].groupby("site_id").configured_value.agg(lambda x: sorted(set(x)))
    for site, versions in fw.items():
        if len(versions) > 1:
            rows.append((site, "", "firmware_version", ",".join(versions), "one version", "Mixed firmware on one site"))
    return pd.DataFrame(rows, columns=["site_id", "charger_id", "parameter", "configured", "expected", "finding"])


def offline_evidence(data, charger_id) -> str:
    t = data["telemetry"]
    t = t[t.charger_id == charger_id]
    share = (t.status == "offline").mean() * 100 if len(t) else 0
    return f"{charger_id} offline in {share:.0f} % of telemetry rows"


def site_parameters(data, site, mode="fixed", outlets=None) -> dict:
    """mode 'fixed' = installed reality; 'as_configured' = values in site_config.csv today.
    outlets = total number of plugs (2029 redesign: extra low-cost outlets sharing the same cabinets)."""
    ch = data["chargers"][data["chargers"].site_id == site].copy()
    sc = data["site_config"][data["site_config"].site_id == site]
    grid = float(data["sites"].set_index("site_id").loc[site, "grid_limit_kw"])
    offline = set()
    disp_max = dict(zip(ch.charger_id, ch.max_kw.astype(float)))
    if mode == "as_configured":
        g = sc[sc.parameter == "grid_limit_kw"].configured_value
        grid = float(g.iloc[0]) if len(g) else grid
        for _, r in sc[sc.parameter == "max_kw"].iterrows():
            disp_max[r.charger_id] = float(r.configured_value)
        offline = set(sc[(sc.parameter == "sim_active") & (sc.configured_value.astype(str).str.lower() != "true")].charger_id)
    cab = data["power_cabinets"][data["power_cabinets"].site_id == site]
    cabinet = dict(zip(ch.charger_id, ch.cabinet_id))
    chargers = list(ch.charger_id)
    if outlets and outlets > len(chargers):
        cabs = list(cab.cabinet_id)
        for i in range(outlets - len(chargers)):
            cid = f"{chargers[0].split('-')[0]}-X{i + 1}"
            cabinet[cid] = cabs[i % len(cabs)]
            disp_max[cid] = float(ch.max_kw.max())
            chargers.append(cid)
    n_on_cab = pd.Series(cabinet).value_counts().to_dict()
    info = {"chargers": chargers, "cabinet": cabinet, "cab_max": dict(zip(cab.cabinet_id, cab.max_kw.astype(float))),
            "n_on_cab": n_on_cab, "disp_max": disp_max, "order": {c: i for i, c in enumerate(chargers)},
            "disp_factor": {}}
    fallback = sc[sc.parameter == "offline_fallback_kw_per_dispenser"].configured_value
    return {"grid_limit_kw": grid, "offline": offline, "info": info,
            "fallback_kw": float(fallback.iloc[0]) if len(fallback) else 50.0}


if __name__ == "__main__":
    from src import load
    d = load.load_all()
    m = mismatches(d)
    print(m.to_string(index=False))
    for c in m[m.parameter == "sim_active"].charger_id:
        print(offline_evidence(d, c))
