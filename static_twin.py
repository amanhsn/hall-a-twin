"""server.py's Twin without USD, for the static build. It runs in the browser under Pyodide.

static_build.py bakes the stage into JSON with the live layers muted, so the baked values are
the design values. This file runs the unchanged sim.py and fabric.py and keeps each one's
results as a plain dict, standing in for its live layer. Composing is then what USD does with
sublayers: the strongest layer that has a value wins, in the order factory.usda lists them.
"""

import json

from fabric import PROPOSAL, Fabric
from sim import Sim

LIVE = ("network", "telemetry")    # strongest first, as in factory.usda's subLayers
TYPE = {float: "float", int: "int", str: "string", bool: "bool"}


def plain(value):
    return round(value, 3) if isinstance(value, float) else value


def usda_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return json.dumps(value)
    text = repr(value)
    return text[:-2] if text.endswith(".0") else text


def usda_overs(snapshot):
    """Print {prim path: values} as nested overs, the way Sdf exports a layer."""
    tree = {"kids": {}, "values": {}}
    for path, values in snapshot.items():
        node = tree
        for name in path.strip("/").split("/"):
            node = node["kids"].setdefault(name, {"kids": {}, "values": {}})
        node["values"] = values

    def block(name, node, depth):
        pad = "    " * depth
        attrs = "\n".join(f"{pad}    custom {TYPE[type(v)]} facility:{k} = {usda_value(v)}"
                          for k, v in sorted(node["values"].items()))
        kids = "\n\n".join(block(n, kid, depth + 1) for n, kid in node["kids"].items())
        body = "\n\n".join(part for part in (attrs, kids) if part)
        return f'{pad}over "{name}"\n{pad}{{\n{body}\n{pad}}}'

    return "\n\n".join(block(n, kid, 0) for n, kid in tree["kids"].items())


class Twin:
    def __init__(self, data_json):
        data = json.loads(data_json)
        self.design, self.prims, self.docs = data["design"], data["prims"], data["docs"]
        self.sim = Sim(data["topology"])
        self.fabric = Fabric(data["topology"], self.sim)
        self.layers = {name: {} for name in LIVE}
        self.muted = set()

    def tick(self):
        self.sim.step()
        self.fabric.step()     # after physics: it reads which racks are running or throttled
        self.layers["telemetry"] = self.sim.snapshot()
        self.layers["network"] = self.fabric.snapshot()

    # What the browser asks for. Each returns JSON text, which is the cheapest way out of Pyodide.

    def composed(self, path):
        values = dict(self.design.get(path, {}))
        for name in reversed(LIVE):    # weakest first, so stronger layers overwrite
            if name not in self.muted:
                values.update({k: plain(v) for k, v in self.layers[name].get(path, {}).items()})
        return values

    def state_data(self):
        fabric = {"proposal": PROPOSAL, "pushed": PROPOSAL["leaf"] in self.fabric.bad_config,
                  "dryRun": self.fabric.dry_run_result}
        return {"values": {path: self.composed(path) for path in self.design},
                "events": list(self.sim.events), "surge": self.sim.surge,
                "offline": sorted(self.sim.offline), "muted": sorted(self.muted), "t": self.sim.t,
                "fabric": fabric}

    def state(self):
        return json.dumps(self.state_data())

    def prim(self, path):
        """The baked teardown, with the live layers' opinions put on top of it."""
        base = self.prims.get(path)
        if not base:
            return json.dumps(None)
        attrs = {a["name"]: dict(a, layers=list(a["layers"])) for a in base["attrs"]}
        stack = list(base["stack"])
        for name in reversed(LIVE):    # weakest first, so each insert(0) puts a stronger layer on top
            own = self.layers[name].get(path)
            if name in self.muted or not own:
                continue
            layer = f"live/{name}.usda"
            for key, value in own.items():
                attr = attrs.setdefault(f"facility:{key}",
                                        {"name": f"facility:{key}", "type": TYPE[type(value)], "layers": []})
                attr["layers"].insert(0, layer)
            subtree = {p: v for p, v in self.layers[name].items() if p == path or p.startswith(path + "/")}
            stack.insert(0, {"layer": layer, "path": path, "usda": usda_overs(subtree)})   # a spec prints with its children
        values = self.composed(path)
        for attr in attrs.values():
            attr["value"] = values[attr["name"].split(":", 1)[1]]
            attr["source"] = attr["layers"][0] if attr["layers"] else "fallback"
        return json.dumps(dict(base, attrs=[attrs[k] for k in sorted(attrs)], stack=stack))

    def live_usda(self, name):
        doc = self.docs.get(name)
        header = f"#usda 1.0\n(\n    {json.dumps(doc)}\n)\n" if doc else "#usda 1.0\n"
        return json.dumps({"usda": f"{header}\n{usda_overs(self.layers[name])}\n\n"})

    def control(self, body_json):
        body = json.loads(body_json)
        action = body.get("action")
        if action == "toggle":
            self.sim.toggle(body["path"])
        elif action == "surge":
            self.sim.set_surge(bool(body.get("on")))
        elif action == "reset":
            self.sim.reset()
            self.fabric.reset()
            for name in list(self.muted):
                self._mute(name, False)
        elif action == "mute":
            self._mute(body.get("layer", "telemetry"), bool(body.get("on")))
        elif action == "dryrun":
            self.fabric.dry_run()
        elif action == "push":
            self.fabric.push()
        elif action == "rollback":
            self.fabric.rollback()
        return self.state()

    def _mute(self, name, on):
        if name not in self.layers or on == (name in self.muted):
            return
        layer = f"live/{name}.usda"
        if on:
            self.muted.add(name)
            self.sim.log(f"{layer} muted. Its values fall back to the layers underneath.", "warn")
        else:
            self.muted.discard(name)
            self.sim.log(f"{layer} back on.")
