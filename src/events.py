"""Decision log: every decision the system takes is one row. This is the proof of autonomy."""
import pandas as pd

COLUMNS = ["ts", "site_id", "layer", "type", "decision", "reason", "human_needed"]
# human_needed: "no" = fully autonomous, "yes" = a person must act (technician, yard move),
#               "driver" = done by the vehicle's own driver while parking (no extra staff)


class DecisionLog:
    def __init__(self):
        self.rows = []

    def log(self, ts, site_id, layer, type_, decision, reason, human_needed="no"):
        assert layer in ("charger", "site", "cloud")
        self.rows.append({"ts": ts, "site_id": site_id, "layer": layer, "type": type_,
                          "decision": decision, "reason": reason, "human_needed": human_needed})

    def extend(self, other: "DecisionLog"):
        self.rows.extend(other.rows)

    def frame(self) -> pd.DataFrame:
        if not self.rows:
            return pd.DataFrame(columns=COLUMNS)
        return pd.DataFrame(self.rows, columns=COLUMNS).sort_values("ts", kind="stable").reset_index(drop=True)
