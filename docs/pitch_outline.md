# Pitch outline (10 minutes)

**Title:** *The network that schedules its own failures*
**Perspective:** the equipment supplier, selling **guaranteed departures** to the fleet operator that owns the depot. The same autopilot also runs the operator's public hubs.
**One-line idea:** chargers show warning signs hours before they fail, so the system finds them, plans around the weak equipment, books the repair in daytime, and dodges price spikes and grid caps along the way.

All numbers come from `outputs/kpi_comparison.csv` and the other output files. The data is synthetic; say so once, early.

---

### 0:00–1:30 · The 2029 story
Thursday night, −14 °C. The depot manager of a city bus operator goes to bed without checking anything.
Friday 06:00, one message on the phone:
> *"Cabinet 3 fan swapped yesterday 09:43 (predicted failure Fri ~02:00). All 32 buses left on time. Tonight: 24 buses, 12 planned swaps, no risk."*

That's the whole job now. Nobody watched a screen overnight, nobody was called out, nobody moved buses by guesswork.

### 1:30–3:00 · What really happened on 13.11.2026 (the data)
* 01:50 cabinet C1-3 overheats; 02:25 it stops. The fan had been dying for 24 h (1 800 → 500 rpm).
* The ticket opens 03:24. The routine is remote reset, then wait, then dispatch at 04:54; fixed at 06:31 (K013).
* **4 buses leave under-charged** at S1, 3 of them at 8 %. Across the five sites: 13 under-charged departures, 77 drivers giving up at the public hubs, and a 799 €/MWh price spike from 17:15.
* Today, people do all the balancing: 51 staff actions and 18 staff hours over Thursday and Friday at five sites.

### 3:00–4:30 · How it works (one diagram)
Three layers:
1. **Charger** keeps the hard limits only and stops on safety faults.
2. **Site brain (edge)** plans every vehicle's charging for the next 30 h, assigns bays, keeps health scores, derates or empties weak equipment, and **keeps running when the cloud is gone**.
3. **Cloud** sends prices, schedules and grid notices, and books technicians and parts.

The health model fits a straight line to fan speed: "fan 1 640 rpm, falling 57 rpm/h, projected trip Fri ~01:30". It's explainable in one sentence and needs no black box.

### 4:30–7:00 · Live demo (`streamlit run app.py`)
1. Toggle **Baseline → Autopilot**: big numbers turn green.
2. **Health panel**: the fan curve, flagged Thu 05:30, fan swapped 09:43, real failure 02:25. It is flagged 20 h before the failure.
3. **S2 timeline**: the DSO notice at 14:00 is re-planned in the same step; the trucks charge before 16:00, the site stays at 400 kW, and nothing is bought during the spike.
4. **S3 timeline**: the cloud outage 11:00–13:00 runs at full power instead of the 50 kW fallback, and 76 → 51 drivers give up.
5. **Worst case**: nobody can fix C1-3 before 08:00. The cabinet really fails, and the system empties it 30 min before the projected trip and re-plans. If the on-call technician may be called, it calls them **only because** its re-plan shows 266 kWh missing, and all 32 buses leave full.
6. **Decision log**: filter to "person needed = yes". That short list is all the human work that's left.

### 7:00–8:30 · Numbers (scenario day, five close-up sites)
| | Today | Autopilot |
|---|---|---|
| Under-charged departures | 13 | **0** |
| Public drivers who gave up | 77 | **51** |
| Energy cost during 17–19 spike | €1 398 | **€948** |
| Peak power S1 / S2 / S5 (kW) | 1080 / 800 / 600 | **834 / 699 / 462** |
| Night call-outs | 1 | **0** |
| Staff hours Thu + Fri | 18.2 | **6.1** |
| S5 commissioning check | 600 kW limit, 50 kW dispenser, dead SIM | found at handover |

### 8:30–9:30 · Business model
* The customer is the fleet operator, buying **guaranteed departures**: [€/vehicle/month] plus a gain share on energy and peak savings.
* The supplier is accountable for decisions it makes: a service credit per missed departure. The customer is accountable for late arrivals or wrong data, which the decision log proves.
* Show the one-page service agreement.

### 9:30–10:00 · Where people stay, and why
* **Physical repairs**: a person with the right part, at a planned time, in daylight.
* **Moving vehicles** when there are more buses than plugs. The 2029 redesign is one low-cost outlet per vehicle on the same cabinets, which in the simulation means 0 moves.
* **Approving risk notices**: the customer decides what a late bus means for the timetable.

Close: *"We didn't remove the people. We removed the surprises."*
