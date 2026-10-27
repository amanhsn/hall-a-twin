"""A logical twin of the network fabric. This is the job NVIDIA DSX Air does.

sim.py simulates physics: heat and power. This file simulates the network that ties the 80
racks into one training job. Like sim.py it has no USD in it. server.py reads the topology
from USD relationships (rack -> uplink -> leaf -> uplink -> spine) and writes the results
into their own layer, live/network.usda, on top of the physics layer.

DSX Air does this properly: it simulates the real switches, NICs and DPUs and the software
running on them, so a change can be tried on a copy of the fabric before it reaches
production. Here a few lines of arithmetic stand in for it, the same way sim.py stands in
for Cadence and ETAP.

The chain of cause and effect:
  switch power, failure or bad config -> uplinks up, and goodput through each leaf
  -> share of the bandwidth each rack asks for that it actually gets -> its step time
  -> the slowest rack sets the pace of the whole job (training is synchronous)
  -> job speed = that pace x the share of racks still in the job
A rack that drops out only rejoins after a minute of healthy running, the way a cluster
scheduler keeps a flapping node out of a job.
"""

CROSS_ROW_SHARE = 0.875    # the job spans 8 rows, so ~7/8 of a rack's traffic leaves its own leaf
COMM_SHARE = 0.3           # share of a training step spent on the network, at full bandwidth
THROTTLED_COMPUTE = 0.62   # a throttled rack does a step's maths at about this speed
BAD_CONFIG_GOODPUT = 0.2   # MTU mismatch: most RDMA frames are dropped, and resends eat the rest
REJOIN_S = 60              # seconds a dropped rack must stay healthy before it rejoins the job
RUNNING = ("ok", "warn", "throttled")

# The change waiting in the queue. The left panel offers a dry run before pushing it.
PROPOSAL = {
    "leaf": "/World/Hall/Network/Leaf_F",
    "ticket": "NET-2041",
    "title": "New RoCE QoS template on Leaf_F",
    "detail": "Moves Leaf_F's GPU-facing ports to the new QoS template. Peer-reviewed, 14-line diff.",
    "cause": "The template sets MTU 1500 but the GPUs send 4 KB RDMA frames, so most of them are dropped and resent.",
}


def short(path):
    return path.rsplit("/", 1)[-1]


class Fabric:
    def __init__(self, topo, sim):
        self.topo = topo
        self.sim = sim                 # reads rack status and utilization, shares the event log
        self.bad_config = set()        # leaves running the broken template
        self.dry_run_result = None
        self.members = set(topo["racks"])   # racks in the training job
        self._healthy_since = {}       # rack -> sim second it last became able to run the job
        self._seen = {}                # last status of each switch, to log changes
        self._job = None               # (racks in job, bottleneck) at the last step
        self.out, self.info = self.evaluate(self.bad_config)

    # Controls, called from the UI ------------------------------------------------

    def dry_run(self):
        """What DSX Air is for: apply the change to a simulated copy and look at the result."""
        leaf = PROPOSAL["leaf"]
        before, _ = self.evaluate(self.bad_config)
        after, info = self.evaluate(self.bad_config | {leaf})
        b, a = before["/World"]["jobSpeedPct"], after["/World"]["jobSpeedPct"]
        share = round(100 * info["ratio"][leaf])
        links = f'{after[leaf]["linksUp"]} of {after[leaf]["linksTotal"]}'
        all_up = after[leaf]["linksUp"] == after[leaf]["linksTotal"]
        self.dry_run_result = {"before": b, "after": a, "rowShare": share, "linksUp": links, "allUp": all_up,
                               "safe": a >= b - 1.0, "t": self.sim.t}
        if self.dry_run_result["safe"]:
            self.sim.log(f"Dry run of {PROPOSAL['ticket']}: no change to the job ({b}% → {a}%). Safe to push.")
        else:
            self.sim.log(f"Dry run of {PROPOSAL['ticket']}: job speed would fall from {b}% to {a}%. "
                         f"Row {short(leaf)[-1]} would get {share}% of the bandwidth it needs"
                         f"{', with every uplink still up' if all_up else ''}. Not pushed.", "warn")

    def push(self):
        self.bad_config.add(PROPOSAL["leaf"])
        self.sim.log(f"{PROPOSAL['ticket']} pushed to {short(PROPOSAL['leaf'])}.")

    def rollback(self):
        self.bad_config.discard(PROPOSAL["leaf"])
        self.sim.log(f"{PROPOSAL['ticket']} rolled back on {short(PROPOSAL['leaf'])}.")

    def reset(self):
        self.bad_config.clear()
        self.dry_run_result = None
        self.members = set(self.topo["racks"])

    # The model ------------------------------------------------------------------

    def _switch_status(self, path, feeds):
        if path in self.sim.offline:
            return "maintenance"
        if feeds & self.sim.offline:
            return "no power"
        return "ok"

    def evaluate(self, bad_config):
        """One pass over the fabric. Pure: it reads the world and returns results, so a dry
        run can call it with a change applied without touching anything."""
        topo, racks = self.topo, self.sim.racks
        out, ratios = {}, {}
        spine_up = {}
        for path, s in topo["spines"].items():
            status = self._switch_status(path, {s["ups"]})
            spine_up[path] = status == "ok"
            out[path] = {"status": status}

        in_job, paces, candidates = [], [], []
        leaf_traffic = {}
        for path, leaf in topo["leaves"].items():
            busway = leaf["busway"]
            status = self._switch_status(path, {busway, topo["busways"][busway]["ups"]})
            up = status == "ok"
            total_links = leaf["per_spine"] * len(leaf["spines"])
            uplinks = leaf["per_spine"] * sum(spine_up[s] for s in leaf["spines"]) if up else 0
            goodput = BAD_CONFIG_GOODPUT if path in bad_config else 1.0
            capacity = uplinks * leaf["link"] * goodput

            # What the racks in this row ask the spines for, and the share of it they get.
            demand = {rp: racks[rp]["util"] * topo["racks"][rp]["links"] * topo["racks"][rp]["link"] * CROSS_ROW_SHARE
                      for rp in leaf["racks"] if racks[rp]["status"] in RUNNING}
            asked = sum(demand.values())
            ratio = (min(1.0, capacity / asked) if asked else 1.0) if up else 0.0
            ratios[path] = ratio
            leaf_traffic[path] = asked * ratio

            for rp in leaf["racks"]:
                if rp not in demand:
                    net = "offline"
                elif not up:
                    net = "isolated"
                else:
                    net = "degraded" if ratio < 0.98 else "ok"
                healthy = net in ("ok", "degraded")
                waited = self.sim.t - self._healthy_since.get(rp, self.sim.t)
                joins = healthy and (rp in self.members or waited >= REJOIN_S)
                out[rp] = {"netStatus": net, "fabricGbps": round(demand.get(rp, 0.0) * ratio), "inJob": joins}
                if healthy:
                    candidates.append(rp)
                if joins:
                    compute = THROTTLED_COMPUTE if racks[rp]["status"] == "throttled" else 1.0
                    step = (1 - COMM_SHARE) / compute + COMM_SHARE / max(ratio, 0.05)
                    why = " + ".join(w for w, hit in (("thermal", compute < 1), ("network", ratio < 0.98)) if hit)
                    paces.append((1 / step, rp, why))
                    in_job.append(rp)

            if up and (path in bad_config or uplinks < total_links or ratio < 0.98):
                status = "degraded"
            out[path] = {"status": status, "linksUp": uplinks, "linksTotal": total_links,
                         "trafficGbps": round(asked * ratio),
                         "utilPct": round(100 * asked / (uplinks * leaf["link"]), 1) if uplinks else 0.0,
                         "servedPct": round(100 * ratio, 1),
                         "config": "QoS v2, MTU 1500" if path in bad_config else "QoS v1, MTU 9216"}

        # Spines carry every leaf's cross-row traffic, split across the spines that are up.
        for path, s in topo["spines"].items():
            links, traffic = 0, 0.0
            for lp, leaf in topo["leaves"].items():
                if path not in leaf["spines"] or not spine_up[path] or out[lp]["linksUp"] == 0:
                    continue
                links += leaf["per_spine"]
                traffic += leaf_traffic[lp] / sum(spine_up[x] for x in leaf["spines"])
            out[path].update({"linksUp": links, "linksTotal": s["links"], "trafficGbps": round(traffic),
                              "utilPct": round(100 * traffic / (links * s["link"]), 1) if links else 0.0})

        pace, slowest, why = min(paces) if paces else (0.0, None, "")
        speed = pace * len(in_job) / len(topo["racks"])
        out["/World"] = {"jobSpeedPct": round(100 * speed, 1), "jobRacks": len(in_job),
                         "jobBottleneck": f"{short(slowest)} ({why})" if why else "none"}
        return out, {"ratio": ratios, "in_job": in_job, "candidates": candidates,
                     "why": why, "slowest": slowest, "speed": speed}

    def step(self):
        out, info = self.evaluate(self.bad_config)

        changes = {}
        for path in list(self.topo["spines"]) + list(self.topo["leaves"]):
            status, before = out[path]["status"], self._seen.get(path, "ok")
            down = ("no power", "maintenance")
            if status in down and before not in down:
                changes.setdefault("down", []).append(short(path))
            elif before in down and status not in down:
                changes.setdefault("up", []).append(short(path))
            elif status == "degraded" and before != "degraded" and path in self.bad_config:
                self.sim.log(f"{short(path)} is dropping RDMA traffic. Every port is up, so link monitoring "
                             "still shows green.", "alert")
            self._seen[path] = status
        if "down" in changes:
            self.sim.log(f"Switches offline: {', '.join(changes['down'])}.", "alert")
        if "up" in changes:
            self.sim.log(f"Switches back online: {', '.join(changes['up'])}.")

        for rp in self.topo["racks"]:
            if rp in info["candidates"]:
                self._healthy_since.setdefault(rp, self.sim.t)
            else:
                self._healthy_since.pop(rp, None)
        self.members = set(info["in_job"])

        total = len(self.topo["racks"])
        n, why = len(info["in_job"]), info["why"]
        if self._job:
            last_n, last_why = self._job
            if n < last_n:
                self.sim.log(f"Training job lost {last_n - n} racks. It restarts from its last checkpoint "
                             f"on {n} of {total}.", "alert")
            elif n > last_n:
                self.sim.log(f"{n - last_n} racks rejoined the training job after a minute of healthy "
                             f"running ({n} of {total}).")
            if why and why != last_why:
                self.sim.log(f"Job slowed to {100 * info['speed']:.0f}%. {short(info['slowest'])} is the slowest "
                             f"rack ({why}), and every other rack waits for it.", "warn")
            elif last_why and not why:
                self.sim.log("No stragglers left. Every rack in the job is running at full pace.")
        self._job = (n, why)
        self.out, self.info = out, info

    def snapshot(self):
        return self.out
