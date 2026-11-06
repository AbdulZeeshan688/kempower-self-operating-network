# Assumptions and simplifications

Everything here is a choice we made. The dataset is synthetic (generated for the hackathon) and nothing in it is real Kempower, customer or market data. All thresholds live in [`config.py`](../config.py).

## Simulation set-up
| Topic | What we did | Why |
|---|---|---|
| Window | Simulate **Wed 11.11. 12:00 → Sat 14.11. 00:00**, report KPIs for **Fri 13.11.** only (same definition as `baseline_kpis.csv` `scenario_day`). | The warm-up lets the buses that arrive on Wednesday and Thursday evening be charged by our policy, and makes the Thursday fan warning visible. |
| Vehicles already on site at the warm-up start | Start at their arrival SoC; no risk notices logged for them. | Conservative; they only affect Wednesday departures, which are outside the KPIs. |
| Time step | Depots: 15-minute slots, a vehicle can charge from the next slot boundary after it arrives and until its departure minute (partial last slot). Public hubs: 1-minute queue model. | Matches the data resolution; public sessions are short (~40 min) and drivers wait 11–37 min, so they need minutes. |
| Departures missing in the data | Assumed to leave the next day at 06:00 (outside the KPI day). | S2 trucks arriving Friday night have no departure in the data. |
| KPI definitions | Re-implemented and **tested to reproduce `baseline_kpis.csv` exactly** for both periods (`tests/test_kpis.py`). Energy = Σ grid power × 0.25 h; cost = (spot + grid-tariff energy part) × energy; peak = highest 15-min power; availability = % of dispenser-slots not `faulted`/`offline`; abandoned = `left_full` + `left_queue` + `left_after_interruption`. | Like-for-like comparison. |

## Information the site brain has (no peeking into the future)
* Telemetry: only rows with timestamp **before** the current time (15-min rows only once their 15 minutes are over). Tested in `tests/test_health.py::test_no_future_leakage`.
* Day-ahead prices for tomorrow become known at **14:00 Finnish time** (13:00 CET publication). Further out, the planner assumes yesterday's price.
* Fleet schedule: known **12 h ahead** (published by the fleet operator). This is what lets the site plan the whole night when the first bus arrives.
* Grid events: only after `notified_ts` (G1 at S2: the site learns at Fri 14:00 about the 16:00–19:00 cap). The real cap is enforced by the charger layer anyway.
* Cloud outage at S3 (Fri 11:00–13:00): the site keeps running on its last plan and prices. Today the dispensers drop to `offline_fallback_kw_per_dispenser` = 50 kW.

## Health model (our estimates, not manufacturer values)
| Signal | Rule |
|---|---|
| Fan trend (cabinet) | Straight-line fit to fan rpm over the last 6 h (min 3 h of data). Projected to reach **700 rpm** (critical) within 24 h → `degrading`; within 4 h or below 700 → `critical`. Drop > 10 % in 6 h → `watch`. Projected **500 rpm** = trip time (ETA). |
| Errors (dispenser) | ≥ 2 error events in 6 h → `watch`; ≥ 3 in 2 h → `degrading`; ≥ 6 in 2 h → `critical`. 1-minute `error_events` where they exist, else 15-min `error_count`; rows logged while the dispenser is already `faulted` are ignored. |
| Temperature (cabinet) | Difference to the sibling cabinets on the same site (cancels the weather), last 6 h vs previous 3 days. **Supporting evidence only**: alone it raised false `watch` alarms on most cabinets during the cold snap, so it never raises the state by itself. |
| Fan data after our repair | Treated as nominal. The real telemetry after Thursday comes from a world where the fan was not swapped, so it cannot be used. |
| Diagnosis | Fan trend → `P_FAN`. Error bursts cannot tell a cable from a power module, so the technician carries the cable (local stock) and the power module is pre-ordered. |

Results of the backtest (`outputs/health_leadtimes.csv`): C1-3 flagged `degrading` Thu 05:30, **20.3 h before** the real derate (Fri 01:50); projected trip ~Fri 01:30–03:00 vs real stop 02:25. D4-2 `degrading` 1.5 h before the 21:30 lockout. D3-3 first `watch` 18.9 h before its power-module failure on 11.11.

## Planner
* Linear programme per depot (PuLP/CBC), re-solved every 15 min over the next ≤ 30 h, only the first slot executed (model-predictive control). Greedy fallback if the solver fails.
* Objective weights: energy (spot + tariff) × kWh; **peak fee 6.5 €/kW-month** on the highest site power above the peak already reached in our run; **100 €/kWh** missing at departure (must outweigh any peak saving; at 10 €/kWh the peak fee made under-charging "cheaper"); **1.00 €/kWh** missing cold-weather margin (worth more than the most expensive hour, much less than a missed departure); **0.05 €/kWh** for not topping up to 100 % (fills only cheap night hours).
* Taper: above 80 % SoC accepted power falls linearly to 30 % of max at 100 %.
* Cold weather: if the forecast at departure is below −10 °C we add **10 % consumption** on the next route as a soft margin (≈ +7–8 % SoC for a 250 km bus route). KPIs are still measured against `required_soc_pct`.
* Cabinet power = `max_kw` × health factor: degrading 0.5 for connector problems; for a **fan problem with a projected trip time** the cabinet keeps full power and is **emptied on purpose 30 min before the projected trip** (in the data the fan declines with time, not load, so early derating would only waste capacity). Critical connector = locked out. Planned repair window = 0.
* Communication faults ("offline from backend") do not stop charging: the site brain controls dispensers over the local network.

## Bays
* Every arrival gets a bay at once if one is free: healthiest dispenser first, then the cabinet with fewest vehicles.
* With more vehicles than bays, the site plans each swap (which bus, which bay, what time) including buses that have not yet arrived, and **commits** to it. Without commitment the swap time slid later every 15 min and the second wave of buses ran out of time.
* With a queue, a bus gives up its bay at its *required* SoC (the cold margin is a bonus).
* A plug-in done by the arriving driver within 15 min of arrival = no extra staff ("driver"). Any later plug-in is a **yard move** (8 min each, the average in `operations_log.csv`). The physical move still needs a person; what changes is that it is planned, not discovered at 2 a.m.

## Repairs
* Technician TE1 works Mon–Fri 08–16; travel from `travel_times.csv`; on-site time = average arrived→closed of today's tickets for the same part (fan 88 min, cable 119 min, power module 100 min). Cost = (2 × travel + on-site) × hourly rate + part.
* No "remote reset and wait 2 h" for faults the health model explains.
* Night call-out (on-call TE2, week 46) **only if the re-plan shows a departure would be missed** before the day shift.
* **Prevented failure rule**: if our repair of C1-3 is finished before the real failure (Fri 01:50), F015/F016 do not happen in our run. We assume a fan swap fixes the overheating, which is what fixed it in the real ticket K013.
* **Worst case**: no technician at all before Fri 08:00 → C1-3 really fails, the system survives by re-planning only. **Worst case + on-call**: same, but the on-call technician may be called.
* D3-3 (S3) stays faulted in all runs: the power module is in the central warehouse with a 3-day lead time, so even predictive ordering on 10.11. would not have fixed it before Friday.

## Public hubs (S3, S4)
* Charging power per vehicle class = median average power of completed baseline sessions (already includes taper).
* An arriving driver drives on if **2** drivers are already waiting (`left_full`); waits at most `max_wait_min` (`left_queue`).
* **Calibration**: the same engine with today's rules (50 kW fallback during the outage, first free dispenser) gives S3 Fri **70** abandoned vs **76** in the data, Thu 37 vs 41; S4 Fri 1 vs 1; energy within 4 %. We compare autopilot with the data baseline and show the replay in `outputs/public_calibration.csv`.
* Autopilot: full power during the cloud outage, right-sized dispenser choice, D4-2 taken out of rotation when `degrading` (finishes the current session at 50 %, no new sessions). This lowers S4 availability slightly (97.9 vs 98.7 %): the dispenser is withdrawn 1.5 h before it would have tripped mid-session.
* Public energy cost goes up slightly at S3 because more drivers are served (more energy sold).

## 2029 redesign scenario
* S1 with 24 outlets (one per bus) and S5 with 12, sharing the **same** cabinets (power stays 3 × 360 kW): no yard moves needed. Illustrative extra cost from `equipment_costs.csv`: a dispenser 18 000 € + civil works 9 000 € per outlet (public ballpark figures, not quotes). Low-cost satellite outlets would be cheaper.

## Things we did not model
* Battery degradation, V2G, reserve markets, CO₂ optimisation.
* Driver behaviour at depots (we assume drivers park where told).
* Weekend technician cover beyond the on-call rule.
* Price changes for public drivers (S3/S4 sell at a fixed public price).
