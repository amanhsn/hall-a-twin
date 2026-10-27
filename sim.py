"""A deliberately simple physics model of the AI hall.

No USD in this file. It takes the topology (who powers and cools whom, which server.py
reads from USD relationships) and steps the facility forward one second at a time.

Real twins swap these equations for proper solvers: Cadence for airflow and thermal (CFD),
ETAP for electrical, PhysicsNeMo for fast AI approximations of both. The shape stays the same:
read the topology and state, compute, write results back to USD.

The chain of cause and effect:
  GPU utilization -> rack power -> heat into the coolant loop -> CDU supply temperature
  -> rack return temperature -> warn, then throttle, then protective shutdown
"""

import random
from collections import deque

BASE_UTILIZATION = 0.72
SURGE_UTILIZATION = 0.97
THROTTLED_UTILIZATION = 0.45

WARN_C = 45.0       # rack coolant return temperature
CRITICAL_C = 50.0   # GPUs start throttling
SHUTDOWN_C = 58.0   # protective shutdown
RECOVER_C = 44.0    # below this, throttled or shut-down racks recover

RACK_DELTA_T = 12.0   # coolant warms ~12 °C across a rack at full load
OVERLOAD_RISE = 25.0  # how far supply temperature climbs when a loop has no capacity at all
ROOM_AIR_C = 24.0     # for air-cooled racks
FIXED_OVERHEAD_KW = 150.0


class Sim:
    def __init__(self, topo, seed=7):
        self.topo = topo
        self.rng = random.Random(seed)
        self.t = 0
        self.surge = False
        self.offline = set()
        self.events = deque(maxlen=40)
        self.racks = {
            path: {"offset": self.rng.uniform(-0.06, 0.06), "util": BASE_UTILIZATION,
                   "power": 0.0, "ret": 35.0, "status": "ok"}
            for path in topo["racks"]
        }
        self.cdus = {path: {"supply": c["design_supply"], "load": 0.0} for path, c in topo["cdus"].items()}
        self.log("Twin started. Hall running a training job at about 72% GPU utilization.")

    # Controls, called from the UI ------------------------------------------------

    def toggle(self, path):
        name = path.rsplit("/", 1)[-1]
        if path in self.offline:
            self.offline.discard(path)
            self.log(f"{name} back online.")
        else:
            self.offline.add(path)
            self.log(f"{name} taken offline.", "alert")

    def set_surge(self, on):
        self.surge = on
        self.log("Training surge: every GPU pushed to ~97%." if on else "Surge over. Back to normal load.",
                 "alert" if on else "info")

    def reset(self):
        self.offline.clear()
        self.surge = False
        self.log("Reset to normal operation.")

    def log(self, text, level="info"):
        self.events.appendleft({"t": self.t, "text": text, "level": level})

    # Physics --------------------------------------------------------------------

    def powered(self, rack_path):
        busway = self.topo["racks"][rack_path]["busway"]
        ups = self.topo["busways"][busway]["ups"]
        return not ({rack_path, busway, ups} & self.offline)

    def step(self):
        self.t += 1
        topo = self.topo
        changes = {}

        # 1. Utilization and power for every rack.
        for path, r in self.racks.items():
            spec = topo["racks"][path]
            target = (SURGE_UTILIZATION if self.surge else BASE_UTILIZATION) + r["offset"]
            if r["status"] == "throttled":
                target = min(target, THROTTLED_UTILIZATION)
            r["util"] += (target - r["util"]) * 0.15 + self.rng.gauss(0, 0.01)
            r["util"] = min(1.0, max(0.05, r["util"]))

            if path in self.offline:
                status = "maintenance"
            elif not self.powered(path):
                status = "no power"
            elif r["status"] == "shutdown" and r["ret"] > RECOVER_C:
                status = "shutdown"
            else:
                status = None  # decided after temperatures update
            r["pending"] = status
            on = status is None
            r["power"] = spec["idle"] + (spec["max"] - spec["idle"]) * r["util"] if on else 0.0

        # 2. Heat into each cooling loop, limited by the chillers' shared capacity.
        for path, c in self.cdus.items():
            c["load"] = sum(self.racks[rp]["power"] * topo["racks"][rp]["liquid"]
                            for rp in topo["cdus"][path]["racks"])
        chiller_cap = sum(ch["cap"] for p, ch in topo["chillers"].items() if p not in self.offline)
        demand = sum(c["load"] for c in self.cdus.values()) or 1.0
        chiller_share = min(1.0, chiller_cap / demand)

        for path, c in self.cdus.items():
            spec = topo["cdus"][path]
            capacity = 0.0 if path in self.offline else min(spec["cap"], c["load"] * chiller_share)
            overload = max(0.0, c["load"] - capacity) / max(c["load"], 1.0)
            target = spec["design_supply"] + 2.0 * c["load"] / spec["cap"] + OVERLOAD_RISE * overload
            c["supply"] += (target - c["supply"]) * 0.06    # coolant has thermal mass: it warms gradually
            c["capacity"] = capacity

        # 3. Rack return temperatures and status.
        for path, r in self.racks.items():
            spec = topo["racks"][path]
            share = r["power"] / spec["max"]
            if spec["cdu"]:
                target = self.cdus[spec["cdu"]]["supply"] + RACK_DELTA_T * share
            else:
                target = ROOM_AIR_C + 14.0 * share
            r["ret"] += (target - r["ret"]) * 0.25

            status = r.pop("pending")
            if status is None:
                if r["ret"] >= SHUTDOWN_C:
                    status = "shutdown"
                elif r["ret"] >= CRITICAL_C or (r["status"] == "throttled" and r["ret"] > RECOVER_C):
                    status = "throttled"
                elif r["ret"] >= WARN_C:
                    status = "warn"
                else:
                    status = "ok"
            if status != r["status"] and status in ("warn", "throttled", "shutdown"):
                changes.setdefault(status, []).append(path.rsplit("/", 1)[-1])
            r["status"] = status

        for status, names in changes.items():
            level = "alert" if status != "warn" else "warn"
            label = {"warn": "Running hot", "throttled": "GPUs throttling", "shutdown": "Protective shutdown"}[status]
            shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            self.log(f"{label}: {shown}", level)

    # Results, written to USD by server.py ----------------------------------------

    def snapshot(self):
        topo = self.topo
        out = {}
        it_kw = 0.0
        busway_load = {p: 0.0 for p in topo["busways"]}
        for path, r in self.racks.items():
            it_kw += r["power"]
            busway_load[topo["racks"][path]["busway"]] += r["power"]
            out[path] = {"powerKW": round(r["power"], 1), "returnTempC": round(r["ret"], 1),
                         "utilization": round(r["util"] if r["power"] else 0.0, 2), "status": r["status"]}
        ups_load = {p: 0.0 for p in topo["ups"]}
        for path, load in busway_load.items():
            ups_load[topo["busways"][path]["ups"]] += load
            out[path] = {"loadKW": round(load, 1), "online": path not in self.offline}
        for path, load in ups_load.items():
            out[path] = {"loadKW": round(load, 1), "online": path not in self.offline}
        for path, c in self.cdus.items():
            out[path] = {"supplyTempC": round(c["supply"], 1), "heatLoadKW": round(c["load"], 1),
                         "online": path not in self.offline,
                         "loadPct": round(100 * c["load"] / topo["cdus"][path]["cap"], 1)}
        heat_to_chillers = sum(c["load"] for c in self.cdus.values())
        online_chillers = [p for p in topo["chillers"] if p not in self.offline]
        for path in topo["chillers"]:
            share = heat_to_chillers / len(online_chillers) if path in online_chillers else 0.0
            out[path] = {"loadKW": round(share, 1), "online": path in online_chillers}

        # PUE = total facility power / IT power. Chillers move heat using power at their COP,
        # the UPS loses a few percent, and lighting, offices and air handlers draw a fixed amount.
        # That fixed part is why PUE gets worse when the hall is half empty.
        cop = sum(topo["chillers"][p]["cop"] for p in topo["chillers"]) / len(topo["chillers"])
        eff = sum(topo["ups"][p]["eff"] for p in topo["ups"]) / len(topo["ups"])
        overhead = it_kw / cop + it_kw * (1 / eff - 1) + FIXED_OVERHEAD_KW
        out["/World"] = {"itLoadKW": round(it_kw, 1),
                         "pue": round((it_kw + overhead) / it_kw, 3) if it_kw else 0.0,
                         "simSeconds": self.t}
        return out
