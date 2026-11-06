"""Streamlit dashboard: the self-operating charging network on Friday 13.11.2026 (synthetic data).
Run: streamlit run app.py   (after python -m src.simulate)"""
import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import config
from src import load

st.set_page_config(page_title="Self-operating charging network", layout="wide")
OUT = config.OUT_DIR
D0, D1 = config.DAY_START, config.DAY_END
RUNS = {"Baseline (people in the loop)": "baseline", "Autopilot": "autopilot", "Autopilot – worst case": "worst_case"}
COLORS = {"healthy": "#2e7d32", "watch": "#9e9d24", "degrading": "#ef6c00", "critical": "#c62828",
          "faulted": "#616161", "repair": "#1565c0", "offline": "#9e9e9e"}


@st.cache_data
def data():
    return load.load_all()


@st.cache_data
def run_files(run):
    out = {}
    for name in ["power", "status", "departures", "bays", "health", "log", "public", "bookings", "moves"]:
        p = os.path.join(OUT, run, f"{name}.csv")
        if os.path.exists(p):
            df = pd.read_csv(p)
            for c in ["ts", "depart_ts", "flagged", "depart", "arrive", "done", "eta_stop"]:
                if c in df:
                    df[c] = pd.to_datetime(df[c], format="ISO8601", errors="coerce")
            out[name] = df
    return out


@st.cache_data
def table(name):
    p = os.path.join(OUT, name)
    return pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()


if not os.path.exists(os.path.join(OUT, "kpi_comparison.csv")):
    st.error("No results yet. Run `python -m src.simulate` first.")
    st.stop()

d = data()
kpi = table("kpi_comparison.csv")
head = json.load(open(os.path.join(OUT, "headline.json")))

st.title("The network that schedules its own failures")
st.caption("Kempower challenge · LahtiES 2026 · close-up sites S1–S5 on Friday 13.11.2026 · "
           "**all data is synthetic** (generated for the hackathon)")

run_label = st.sidebar.radio("Show", list(RUNS), index=1)
run = RUNS[run_label]
st.sidebar.markdown("**Autopilot**: fan problem predicted Thursday, fixed in the day shift.\n\n"
                    "**Worst case**: nobody can fix it before Fri 08:00 – the cabinet really fails at night and the "
                    "system has to cope on its own.")


def k(site, name, col):
    r = kpi[(kpi.site_id == site) & (kpi.kpi == name)]
    return None if r.empty else r[col].iloc[0]


# ---------------------------------------------------------------- 1. big numbers
col = "baseline" if run == "baseline" else run
cards = [("Buses/trucks leaving under-charged", "departures_below_required_soc", "{:.0f}"),
         ("Public drivers who gave up", "public_abandoned", "{:.0f}"),
         ("Energy cost (€)", "energy_cost_eur", "{:,.0f}"),
         ("Cost 17–19 price spike (€)", "cost_17_19_eur", "{:,.0f}"),
         ("Staff hours on site", "staff_hours", "{:.1f}"),
         ("Night call-outs", "night_callouts", "{:.0f}")]
cols = st.columns(len(cards))
for c, (label, name, fmt) in zip(cols, cards):
    v, b = k("ALL", name, col), k("ALL", name, "baseline")
    delta = None if run == "baseline" or v is None else fmt.format(v - b)
    c.metric(label, fmt.format(v) if v is not None else "–", delta, delta_color="inverse")
peaks = head["peak_kw"][col]
st.caption("Peak grid power (kW): " + " · ".join(f"{s} {peaks[s]:.0f}" + (f" (today {head['peak_kw']['baseline'][s]:.0f})" if run != "baseline" else "")
                                                for s in config.ALL_SITES)
           + f" · Friday decisions taken by the system: {head['decisions_friday']}, of which "
             f"{head['decisions_friday_autonomous']} needed no person")

# ---------------------------------------------------------------- 2. timeline
st.header("Friday, slot by slot")
c1, c2 = st.columns([1, 3])
site = c1.selectbox("Site", config.ALL_SITES, index=0,
                    format_func=lambda s: f"{s} {d['sites'].set_index('site_id').loc[s, 'name']}")
t = c2.slider("Time", min_value=D0.to_pydatetime(), max_value=(D1 - config.STEP).to_pydatetime(),
              value=pd.Timestamp("2026-11-13T02:30:00+02:00").to_pydatetime(),
              step=pd.Timedelta("15min").to_pytimedelta(), format="ddd HH:mm")
t = pd.Timestamp(t)

bp = d["site_power_baseline"]
bp = bp[(bp.site_id == site) & (bp.ts >= D0 - pd.Timedelta("6h")) & (bp.ts < D1)]
price = load.price_eur_kwh(d)["spot_eur_mwh"]
price = price[(price.index >= D0 - pd.Timedelta("6h")) & (price.index < D1)]
fig = make_subplots(specs=[[{"secondary_y": True}]])
fig.add_trace(go.Scatter(x=bp.ts, y=bp.power_kw, name="Today (people)", line=dict(color="#9e9e9e", shape="hv")))
if run != "baseline":
    rp = run_files(run)["power"]
    rp = rp[(rp.site_id == site) & (rp.ts >= D0 - pd.Timedelta("6h"))]
    fig.add_trace(go.Scatter(x=rp.ts, y=rp.power_kw, name=run_label, line=dict(color="#1565c0", width=3, shape="hv")))
    fig.add_trace(go.Scatter(x=rp.ts, y=rp.grid_limit_kw, name="Grid limit", line=dict(color="#c62828", dash="dash", shape="hv")))
else:
    lim = float(d["sites"].set_index("site_id").loc[site, "grid_limit_kw"])
    fig.add_hline(y=lim, line_dash="dash", line_color="#c62828", annotation_text="grid limit")
fig.add_trace(go.Scatter(x=price.index, y=price.values, name="Spot price €/MWh", line=dict(color="#ef6c00", width=1)),
              secondary_y=True)
fig.add_vline(x=t, line_color="black", line_width=1)
log_all = run_files("autopilot" if run == "baseline" else run).get("log", pd.DataFrame())
if len(log_all):
    ev = log_all[(log_all.site_id == site) & log_all.type.isin(["repair", "grid", "price", "connectivity", "lockout", "fault"]) &
                 (log_all.ts >= D0 - pd.Timedelta("6h")) & (log_all.ts < D1)]
    if run == "baseline":
        ev = ev[ev.type.isin(["grid", "price"])]
    for _, e in ev.drop_duplicates("ts").iterrows():
        fig.add_vline(x=e.ts, line_color="#7b1fa2", line_dash="dot", opacity=0.5)
fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.12),
                  yaxis_title="kW", hovermode="x unified")
fig.update_yaxes(title_text="€/MWh", secondary_y=True)
st.plotly_chart(fig, use_container_width=True)
st.caption("Purple dotted lines: system events (price published, grid notice, repair, cloud outage, lockout).")

# ---------------------------------------------------------------- 3. bays + health
left, right = st.columns([1.1, 1])
with left:
    st.subheader(f"Bay map {site} at {t:%a %H:%M}")
    chargers = d["chargers"][d["chargers"].site_id == site]
    if run == "baseline":
        tel = d["telemetry"]
        tel = tel[(tel.site_id == site) & (tel.ts == t)].set_index("charger_id")
        ss = d["sessions_baseline"]
        ss = ss[(ss.site_id == site) & (ss.start_ts <= t) & (ss.end_ts > t)].set_index("charger_id")
        cells = [(r.charger_id, r.cabinet_id, (ss.vehicle_id.get(r.charger_id) if isinstance(ss.vehicle_id.get(r.charger_id), str) else
                  ("car/van" if r.charger_id in ss.index else "")), None,
                  tel.power_kw.get(r.charger_id, 0), {"faulted": "faulted", "offline": "offline"}.get(tel.status.get(r.charger_id, ""), "healthy"))
                 for r in chargers.itertuples()]
    else:
        b = run_files(run).get("bays", pd.DataFrame())
        b = b[(b.site_id == site) & (b.ts == t)] if len(b) else b
        stt = run_files(run).get("status", pd.DataFrame())
        stt = stt[(stt.site_id == site) & (stt.ts == t)].set_index("charger_id") if len(stt) else stt
        if len(b):
            cells = [(r.charger_id, r.cabinet_id, r.vehicle_id if isinstance(r.vehicle_id, str) else "", r.soc, r.power_kw,
                      "offline" if stt.status.get(r.charger_id) == "offline" else (r.state if isinstance(r.state, str) else "healthy"))
                     for r in b.itertuples()]
        else:   # public hubs: status only
            cells = [(r.charger_id, r.cabinet_id, "in use" if stt.status.get(r.charger_id) == "charging" else "", None, None,
                      "faulted" if stt.status.get(r.charger_id) == "faulted" else "healthy") for r in chargers.itertuples()]
    ncol = 4
    for i in range(0, len(cells), ncol):
        row = st.columns(ncol)
        for c, (cid, cab, veh, soc, kw, state) in zip(row, cells[i:i + ncol]):
            color = COLORS.get(state, "#2e7d32")
            soc_txt = f"{soc:.0f} %" if soc is not None and not pd.isna(soc) else ""
            kw_txt = f"{kw:.0f} kW" if kw is not None and not pd.isna(kw) and kw > 0 else ""
            c.markdown(f"<div style='border-left:8px solid {color};padding:6px;margin:3px;background:#f5f5f5;border-radius:4px'>"
                       f"<b>{cid}</b> <span style='color:#777'>{cab}</span><br>"
                       f"<span style='font-size:1.2em'>{veh or '&nbsp;'}</span> {soc_txt}<br>"
                       f"<span style='color:#555'>{kw_txt} · {state}</span></div>", unsafe_allow_html=True)

with right:
    st.subheader("Health: S1 cabinet C1-3 cooling fan")
    t1 = d["telemetry_1min"]
    fan = t1[t1.charger_id == "D1-9"].dropna(subset=["fan_rpm"])
    eta = table("c1_3_fan_eta.csv")
    eta["ts"] = pd.to_datetime(eta.ts, format="ISO8601")
    hf = make_subplots(specs=[[{"secondary_y": True}]])
    hf.add_trace(go.Scatter(x=fan.ts, y=fan.fan_rpm, name="fan rpm", line=dict(color="#455a64")))
    hf.add_trace(go.Scatter(x=eta.ts, y=eta.score, name="health score", line=dict(color="#ef6c00", shape="hv")), secondary_y=True)
    lt = table("health_leadtimes.csv")
    flag = pd.Timestamp(lt[(lt.unit == "C1-3") & (lt.state == "degrading")].flag_ts.iloc[0])
    marks = [(flag, "predicted", "#ef6c00"), (pd.Timestamp("2026-11-13T02:25:00+02:00"), "real failure", "#c62828")]
    bk = run_files("autopilot").get("bookings", pd.DataFrame())
    if len(bk):
        r = bk[bk.unit == "C1-3"].iloc[0]
        marks.append((r.done, "fan swapped (autopilot)", "#1565c0"))
    for x, label, colr in marks:
        hf.add_vline(x=x, line_color=colr, line_dash="dash")
        hf.add_annotation(x=x, y=1800, text=label, showarrow=False, textangle=-90, xshift=-8, font=dict(color=colr))
    hf.update_layout(height=330, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.1))
    hf.update_yaxes(title_text="rpm", secondary_y=False)
    hf.update_yaxes(title_text="score", range=[0, 105], secondary_y=True)
    st.plotly_chart(hf, use_container_width=True)
    st.dataframe(lt[["unit", "state", "flag_ts", "lead_h", "reason"]], hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- 4. decision log
st.header("Decision log – the proof of autonomy")
if run == "baseline":
    st.info("Today there is no decision log: people decide. Pick Autopilot to see every decision the system took.")
else:
    lg = run_files(run)["log"]
    f1, f2, f3, f4 = st.columns(4)
    sites_f = f1.multiselect("Site", config.ALL_SITES, default=config.ALL_SITES)
    layers = f2.multiselect("Layer", ["charger", "site", "cloud"], default=["charger", "site", "cloud"])
    types = f3.multiselect("Type", sorted(lg.type.unique()), default=[x for x in sorted(lg.type.unique()) if x != "bay"])
    human = f4.selectbox("Person needed?", ["all", "yes", "no", "driver"])
    day_only = st.checkbox("Thursday + Friday only", value=True)
    v = lg[lg.site_id.isin(sites_f) & lg.layer.isin(layers) & lg.type.isin(types)]
    if human != "all":
        v = v[v.human_needed == human]
    if day_only:
        v = v[(v.ts >= D0 - pd.Timedelta("1D")) & (v.ts < D1)]
    st.dataframe(v.assign(ts=v.ts.dt.strftime("%a %d.%m %H:%M")), hide_index=True, use_container_width=True, height=380)

# ---------------------------------------------------------------- 5. tables
with st.expander("All KPIs per site (Friday 13.11.)"):
    st.dataframe(kpi, hide_index=True, use_container_width=True)
with st.expander("Public hubs: how well does the queue model match today's data?"):
    st.dataframe(table("public_calibration.csv"), hide_index=True)
with st.expander("Human work Thursday + Friday, and the 2029 redesign"):
    st.dataframe(table("human_work_thu_fri.csv"), hide_index=True)
    st.dataframe(table("redesign_2029.csv"), hide_index=True)
    com = head.get("commissioning", {})
    st.write(f"S5 under-charged departures on Friday: today {com.get('baseline_data')}, autopilot with today's wrong settings "
             f"{com.get('autopilot_as_configured')}, autopilot with settings fixed at handover {com.get('autopilot_config_fixed')}.")
