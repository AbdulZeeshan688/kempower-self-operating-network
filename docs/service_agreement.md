# Service agreement: Guaranteed Departures

**Supplier:** the charging equipment supplier (operates the autopilot) · **Customer:** the fleet operator that owns the depot (e.g. a city bus operator) · **Term:** 36 months · *Draft for the LahtiES 2026 hackathon. Amounts in [brackets] are placeholders for the team to set.*

### 1. What we promise
* **Every vehicle leaves with its required state of charge**, provided it arrived as scheduled and the schedule data was correct.
* Charging stays within the site's grid connection and any grid-company cap we are notified of.
* We keep the customer's energy cost and peak power as low as the departure promise allows, and report both monthly.
* A **risk notice before 22:00** if the system cannot meet a departure. It names the vehicle, the expected SoC and the cause.

### 2. What the system decides on its own
| Decision | Example from the 13.11.2026 simulation |
|---|---|
| Charging power and timing per vehicle | No energy bought while the spot price was above 200 €/MWh |
| Bay assignment and planned swaps | "B12 → bay D1-4"; swaps planned hours ahead |
| Derating or emptying weak equipment | C1-3 emptied 30 min before its projected fan trip |
| Booking repairs and ordering parts | Fan swap booked Thu 08:15, 20 h before the real failure |
| Reacting to price and grid events | S2 held at 400 kW during the DSO cap |
| Running offline when the cloud is lost | S3 at full power through a 2 h outage |
| Calling the on-call technician at night | Only if the re-plan shows a departure would be missed |

### 3. What needs a person
* **Physical repairs** (the technician the system booked, with the part it chose).
* **Moving vehicles** when there are more vehicles than plugs. These are planned yard moves at known times, not night-shift firefighting.
* **Approving a risk notice** (customer side), e.g. swapping a bus to another route.
* Changes to the agreement, tariffs or site hardware.

### 4. Who is accountable when it goes wrong
| Cause of an under-charged departure | Who pays |
|---|---|
| A system decision (planning, bay, missed prediction, late booking) | **Supplier**: service credit of [€150] per departure (today's contract penalty) |
| Late arrival, wrong SoC or schedule data, vehicle fault | **Customer**: logged with proof in the decision log |
| Grid-company cap with less than [2 h] notice, or a grid outage | **Shared**: no credit, but the supplier must show its best re-plan |
| Equipment failure that the health model flagged and the supplier did not act on | **Supplier**: credit plus root-cause report within [5] working days |

Credits are capped at [20 %] of the monthly fee.

### 5. Price
* **[€ __ per vehicle per month]**, covering equipment service, the autopilot, repairs, parts and the planned yard work.
* **Gain share:** [30 %] of the measured saving in energy cost and peak-power fee versus the agreed baseline year goes to the supplier.
* Anchors from the simulation (`outputs/kpi_comparison.csv`, synthetic data): on the scenario day the three depots needed 0 instead of 13 under-charged departures, the network paid €450 less during the 2-hour price spike, depot peaks fell by 101–246 kW (at 6.5 €/kW-month), and staff time over Thursday and Friday fell from 18.2 h to 6.1 h.

### 6. Data and transparency
* The customer sees the **full decision log** (every decision, the reason, and whether a person was needed) and the health state of every cabinet and dispenser.
* The site keeps working and logging when the cloud connection is lost; the log is uploaded when it returns.
* The customer owns its fleet and schedule data. The supplier owns the models and may use anonymised equipment data to improve them.
