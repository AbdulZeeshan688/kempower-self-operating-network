# Project: "The network that schedules its own failures"

Kempower challenge, LahtiES Energy Tech Hackathon 2026. **Submission deadline: Saturday 7.11. at 13:00.** Time is short, so follow the build order at the bottom and get something working end to end before polishing anything.

## What we're building

A Python simulation of a charging network that runs Friday 13.11.2026 with no human in the loop. It replays the real (synthetic) data for the 5 close-up sites (S1–S5), but our "autopilot" makes the decisions instead of people. At the end we compare our results with the baseline (`baseline_kpis.csv`) and show it all in a Streamlit dashboard.

The core idea in one sentence: chargers show warning signs hours before they fail, so the system spots them, turns the weak equipment down on purpose, moves vehicles to healthy chargers, and books the repair for daytime, while also dodging price spikes and grid limits.

## The data

The dataset is in `data/` (copy the `kempower-challenge-data/` folder there). Read `data/README.md` and `data/DATA_DICTIONARY.md` before writing code. All timestamps are ISO 8601 with +02:00 (Finnish time). A 15-min row covers the 15 minutes **starting** at its timestamp. Parse timestamps with pandas and keep them timezone-aware.

Files we use the most:

| File | What we use it for |
|---|---|
| `sites.csv` | Real grid limit per site, tariff, PV, connectivity quality |
| `power_cabinets.csv` | `max_kw` per cabinet (shared by its dispensers) |
| `chargers.csv` | Dispensers, which cabinet they hang off, `max_kw` |
| `vehicles.csv` | Battery size, max charge kW, consumption (buses B01–B36, trucks T01–T14) |
| `fleet_schedule.csv` | Every depot visit: arrive time + SoC, depart time, required SoC |
| `prices.csv` | 15-min spot price + grid tariff energy part. Cost = (spot + tariff) × energy |
| `tariffs.csv` | Day/night tariff and peak power fee (€/kW/month) |
| `grid_events.csv` | G1: S2 capped at 400 kW, Fri 16:00–19:00, notified 14:00 |
| `connectivity_outages.csv` | S3 loses the cloud Fri 11:00–13:00 |
| `telemetry.csv` | 15-min per dispenser: status, power, cabinet temp, error count |
| `telemetry_1min.csv` | 1-min detail before failures, includes `fan_rpm` |
| `faults.csv`, `service_tickets.csv` | What broke, when, how it was fixed today |
| `spare_parts.csv`, `technicians.csv`, `travel_times.csv` | For booking repairs (TE1 works Mon–Fri 08–16) |
| `site_config.csv` | How sites are configured today. Compare with `sites.csv` and `chargers.csv` |
| `departures_baseline.csv`, `sessions_baseline.csv`, `site_power_baseline.csv` | What happened with humans in the loop |
| `baseline_kpis.csv` | Baseline numbers we must beat (rows `scenario_day` and `all_14_days`) |
| `operations_log.csv` | Human work per action type (for the "hours of human work removed" number) |
| `public_arrivals.csv` | Public drivers at S3/S4 (only if time allows) |

### What happens on Friday 13.11. (we already found this; verify it in code)

1. **S1 cabinet C1-3 fan dies slowly.** In `telemetry_1min.csv`, `fan_rpm` on D1-9..D1-12 drops from ~1800 to ~500 between Thu 02:25 and Fri 02:25. Cabinet derates to 50% at 01:50 (F015), stops at 02:25 (F016), technician dispatched 04:54, fixed 06:31 with a €180 fan (P_FAN). 4 buses left S1 under-charged that day.
2. **Price spike.** Spot goes up to 799 €/MWh at 18:00, with 17:00–19:00 being very expensive (normal days are 20–60 €/MWh average).
3. **Grid cap at S2.** 400 kW limit 16:00–19:00 (normal 800), with 2 h notice.
4. **S3 cloud outage** 11:00–13:00. The site must keep running without the cloud.
5. **S4 D4-2 cable damage.** Errors start 18:57, lockout at 21:30 (F017).
6. **Earlier warnings to use as extra proof:** D3-3 at S3 shows error events from 10.11. 18:59, about 22 h before its power module failure (F014, 11.11. 16:40).
7. **Bonus finding: S5 was commissioned with wrong settings.** `site_config.csv` says S5 grid limit is 600 kW but `sites.csv` says 900. D5-7 is configured at 50 kW but rated 150. One S5 dispenser has `sim_active = false` (check it against fault F011 "offline from backend"). This probably explains S5's 5 under-charged departures.

Also notice in `service_tickets.csv`: faults at night don't get a ticket until after 08:00, and every ticket starts with a remote reset followed by a ~2 h wait before dispatch. That's the human gap we close.

## Architecture (also the story we tell judges)

Three layers. Code them as separate Python modules so the split is visible.

1. **Charger layer (local safety).** Hard limits only: never exceed the dispenser or cabinet max, stop on a safety fault. If cut off from everything, use `offline_fallback_kw_per_dispenser`.
2. **Site brain (edge).** Owns the charging plan for the next 24 h, bay assignment, health scores and derating. **It must keep running when the cloud is gone** (S3 outage). It runs on the last prices and schedules it received.
3. **Cloud.** Sends prices, fleet schedules and grid notices to sites; handles repair booking and spare parts. When a site goes offline, the cloud just stops receiving updates; nothing breaks.

## Project structure

```
autopilot/
  data/                      # the dataset (unchanged)
  src/
    load.py                  # read + clean all CSVs, one function per file
    health.py                # failure prediction + health score
    planner.py               # charging schedule (power per vehicle per 15 min)
    bays.py                  # which vehicle goes to which dispenser
    repairs.py               # repair booking + spare part check
    commissioning_check.py   # bonus: config vs reality check
    simulate.py              # runs Friday step by step, edge/cloud split
    kpis.py                  # computes KPIs, same definitions as baseline_kpis.csv
    events.py                # decision log (every decision the system makes)
  app.py                     # Streamlit dashboard
  outputs/                   # results CSVs, charts, decision log
  docs/
    service_agreement.md
    pitch_outline.md
    assumptions.md
  tests/
  requirements.txt
  README.md
```

`requirements.txt`: pandas, numpy, pulp, streamlit, plotly, pytest. Use Python 3.11+.

## Module details

### load.py
- One function per file, returns a DataFrame with parsed timestamps.
- A `load_all()` that returns a dict.
- A helper that filters to the scenario window. Simulate **Thu 12.11. 00:00 → Sat 14.11. 00:00**, so the buses that arrive Thursday evening are included and the fan warning on Thursday is visible. Report KPIs for **Fri 13.11. only** (same definition as `baseline_kpis.csv` `scenario_day`).

### health.py: failure prediction
Keep it explainable. Judges must understand it in 20 seconds. No black-box ML.

- For each cabinet, compute a **health score 0–100** every 15 minutes from:
  - **Fan trend:** slope of `fan_rpm` over the last 6 h (from `telemetry_1min.csv` where available). Falling fast = bad. Also absolute level (e.g. under 1000 rpm = warning, under 700 = critical; put these thresholds in one config dict and say in `assumptions.md` that they're our estimates).
  - **Temperature margin:** cabinet temp vs. power. If temperature at the same power is going up compared with the last few days, cooling is getting worse.
  - **Error rate:** rolling count of `error_count` / `error_events`. Rising errors = warning.
- Output states: `healthy`, `watch`, `degrading`, `critical`, plus a short text reason ("fan speed down 60% in 18 h").
- **Must flag C1-3 as `degrading` on Thursday during working hours** (before 16:00, when TE1 is still on shift), and D4-2 well before 21:30. Write a test for both.
- Also run it backwards on D3-3 (10–11.11.) to show the ~22 h warning. This goes on a slide.
- Important: only use data up to the current simulation time. No peeking into the future. Add a test that checks this.

### planner.py: charging schedule
For each site, every 15-min step, decide kW per plugged-in vehicle.

Constraints:
- Sum of site power ≤ grid limit (use the **real** limit from `sites.csv`, and the G1 cap at S2 during 16:00–19:00 once it's been notified at 14:00).
- Sum per cabinet ≤ cabinet `max_kw` × health factor (healthy = 1.0, degrading = 0.5, critical/faulted = 0).
- Per vehicle ≤ min(dispenser max, vehicle `max_charge_kw`). Add a simple taper above 80% SoC (e.g. linear down to 30% of max at 100%; write it in `assumptions.md`).
- Vehicle must reach `required_soc_pct` by `depart_ts` (soft constraint with a very high penalty, so the model never becomes infeasible).

Objective: minimise energy cost ((spot + grid tariff) × kWh) + peak power fee + a large penalty for every kWh missing at departure.

Start with a **greedy version** that works (charge the vehicles with the least slack first, fill the cheapest 15-min slots first). Then, if time allows, swap in a **PuLP LP** over a rolling 24 h horizon, re-solved every 15 min or whenever something changes (fault, grid notice, new arrival). Keep the greedy version as a fallback; this is also what the site brain uses if the solver fails.

Effect we want to show: charging moves out of 17:00–19:00 on Friday, S2 stays under 400 kW during the cap, and the buses still leave full.

Cold weather: Thursday night is cold. Check `weather.csv`. If it's below about −10 °C, add a margin to required SoC (we assume e.g. +10% consumption). State it in assumptions.

### bays.py: which bay each vehicle parks in
This is the part that removes the biggest human job (`move_and_replug_vehicle`, ~746 staff hours in 2 weeks across the network).

- When a vehicle arrives, assign it a dispenser so it never needs moving: vehicles leaving soonest go to the healthiest dispensers with the most cabinet headroom; never assign to a dispenser on a `degrading` or `critical` cabinet if a healthy one is free.
- If there are more vehicles than dispensers, plan a **single** planned swap, timed and logged as an instruction to the driver/yard (not a night staff job), and count it.
- Output a "driver message" per arrival, e.g. `B12 → bay D1-4`.
- Count `unplugged_by_operator` sessions in `sessions_baseline.csv` for Friday vs. our planned moves. That's our "replugs avoided" number.

### repairs.py: booking the fix
- When health hits `degrading`: check the likely part (fan problem → P_FAN, cable/connector errors → P_CABLE_CCS2, power module errors → P_POWER_MODULE), check stock in `spare_parts.csv`, and book TE1 into the next slot inside his shift (Mon–Fri 08–16) using `travel_times.csv`.
- Repair duration: take the average from `service_tickets.csv` for the same part (arrived → closed).
- Skip the "remote reset then wait 2 hours" step for faults the health model already explains; send the technician with the right part directly.
- Cost of the repair = technician hours × `hourly_cost_eur` + part cost. Compare with the real ticket cost (K013 = €340.60 at night).
- If something fails outside shift hours anyway (D4-2 at 21:30 on Friday), lock the dispenser out, re-plan around it, and book TE1 for the next working day. Never pretend a person isn't needed for the physical fix.

### commissioning_check.py (bonus, only if time allows)
- Compare `site_config.csv` with `sites.csv` / `chargers.csv`. Flag every mismatch (S5 grid limit 600 vs 900, D5-7 max_kw 50 vs 150, inactive SIM).
- In the simulation, run S5 twice: as configured today vs. with the config fixed automatically at handover. Show how many under-charged departures that removes.

### simulate.py
- Steps through 15-min intervals from Thu 00:00 to Sat 00:00.
- At each step:
  1. Cloud pushes new info (prices, schedule, grid notices) to each site, **unless** that site is in a connectivity outage. Then the site keeps its last copy.
  2. Health update per cabinet from telemetry up to now.
  3. Inject real faults from `faults.csv` **unless our system prevented them** (see rule below).
  4. Bay assignment for new arrivals.
  5. Re-plan if anything changed.
  6. Move vehicle SoC forward with the planned power (kWh = kW × 0.25; SoC += kWh / battery_kwh).
  7. Log every decision to the event log.
- **Rule for prevented failures:** if our repair for C1-3 is finished before the real failure time (Fri 01:50), the fault doesn't happen in our run. Be honest about this: write it in assumptions ("we assume a fan swap fixes the overheating, which is what fixed it in the real ticket K013"). Also run a **"worst case" mode** where the repair is not done in time, and the cabinet still fails but our planner has already derated it and moved buses away. The judges want to see the system survive a real failure, so show both runs.
- Use the real public arrivals for S3/S4 if time allows; otherwise copy the baseline numbers for public sites and say so.

### events.py: decision log
Every decision is a row: `ts, site_id, layer (charger/site/cloud), type, decision, reason, human_needed (yes/no)`.
Examples:
- `Thu 10:15, S1, site, health, C1-3 → degrading, "fan rpm down 35% in 8 h", no`
- `Thu 10:15, S1, cloud, repair, TE1 booked Thu 13:00 with P_FAN, "part in stock at local base", yes (physical repair)`
- `Fri 14:00, S2, cloud, grid, re-planned for 400 kW cap 16–19, "DSO notice G1", no`
- `Fri 11:00, S3, site, connectivity, running offline on last plan, "cloud unreachable", no`

This log is our proof of autonomy. Show it in the dashboard and save it to `outputs/decision_log.csv`.

### kpis.py
Compute the same columns as `baseline_kpis.csv` for our run, Friday only and per site: departures, departures_below_required_soc, energy_mwh, energy_cost_eur, peak_kw, charger_availability_pct (plus public_abandoned if we simulate public sites).

Plus our own:
- Human interventions (count from decision log where `human_needed = yes`) vs. baseline (from `operations_log.csv` and `service_tickets.csv` for the close-up sites on 13.11.)
- Replugs avoided
- Night callouts avoided
- Energy cost during 17:00–19:00 vs. baseline
- Repair cost vs. real ticket cost

Output `outputs/kpi_comparison.csv` with columns: `site_id, kpi, baseline, autopilot, change`. **Never hardcode results.** Every number must come from the simulation.

## Streamlit dashboard (app.py)
This is the live demo. It must be easy for a non-specialist to follow in 2 minutes.

1. **Top:** the big numbers. Under-charged departures (baseline vs ours), energy cost, peak kW, human interventions. Green if better.
2. **Timeline of Friday:** a slider or play button through the day. Shows site power vs. grid limit, the price curve, and markers for each event (fault predicted, repair, price spike, grid cap, cloud outage).
3. **Health panel:** C1-3 fan rpm falling with the health score on top, and a vertical line where we booked the repair vs. where it failed in reality.
4. **Bay map for S1:** grid of 12 dispensers, coloured by health, showing which bus is where.
5. **Decision log:** filterable table.
6. **Toggle:** "Baseline (humans) / Autopilot / Autopilot worst case".

Use plotly for charts. Keep the styling simple and clean.

## Docs to write (in docs/)

**service_agreement.md: one page.** Our model is "guaranteed departures":
- Customer: the fleet operator (bus/logistics company owning the depot).
- Promise: every vehicle leaves with its required SoC, as long as it arrived on schedule and the schedule data was correct.
- What the system decides alone: charging power and timing, bay assignment, derating weak equipment, booking repairs, ordering parts, reacting to price and grid events.
- What needs a human: physical repairs; approving a "risk notice" sent before 22:00 if the system can't meet a departure.
- Accountability: under-charged departure caused by a system decision → supplier credits the customer (put a placeholder amount per departure; the team decides the number). Caused by the customer (late arrival, wrong data) → logged with proof, customer's responsibility. Grid operator curtailment with less than X hours notice → shared.
- Pricing: monthly fee per vehicle (placeholder; the team decides). Mention that the KPI numbers from the simulation back up the value.
- Data and transparency: the customer can see the full decision log.

**pitch_outline.md:** a 10-minute pitch around one 2029 customer story. A depot manager at a bus company sleeps through a freezing Thursday night. Friday morning there's one message: "Fan swapped yesterday at 13:00, all buses left on time." Then show what really happened in the 2026 data. Then demo, numbers, business model. End with where humans stay in the loop and why.

**assumptions.md:** every threshold and simplification, written plainly.

## Rules for Claude Code
- Get a full end-to-end run working first (load → greedy plan → simulate → KPIs → simple dashboard), then improve. Don't spend hours on one module.
- No future data leakage: at time t, decisions use only data with timestamp < t (except schedules and day-ahead prices, which are known in advance; grid events only after `notified_ts`).
- Every number shown must be computed, not typed in.
- Put thresholds in one `config.py`.
- Write small pytest tests for: health flags C1-3 before Thu 16:00; S2 stays ≤ 400 kW 16:00–19:00 Fri; site power never above grid limit; S3 keeps charging during the outage; no leakage in health.py; KPI function reproduces `baseline_kpis.csv` when fed the baseline data (good sanity check on our KPI definitions).
- Keep code readable. Short functions, clear names, a few comments where logic isn't obvious.
- The data is synthetic. Never call it real Kempower data anywhere in the app or docs.
- When done, write `README.md` with: how to install, how to run (`python -m src.simulate` then `streamlit run app.py`), and a short description.

## Build order (stop and check after each step)
1. `load.py` + check all files load. Print Friday's events to confirm the list above. (30 min)
2. `kpis.py` and check it reproduces `baseline_kpis.csv` from baseline data. (30 min)
3. Greedy `planner.py` + `bays.py` + basic `simulate.py` for S1 and S2 only. Get KPIs out. (2 h)
4. `health.py` + `repairs.py`, wired into the sim. Normal run + worst-case run. (1.5 h)
5. Add S3 (cloud outage), S4 (D4-2 fault), S5. (1 h)
6. Streamlit dashboard. (1.5 h)
7. Docs: service agreement, pitch outline, assumptions. (1 h)
8. Bonus if time: PuLP planner, commissioning check, public drivers at S3/S4.
9. Final: run all tests, regenerate outputs, check every chart.
