"""Run the sample AI factory twin.

    .venv/bin/python server.py      then open http://localhost:8765

What this file does, once a second:
  1. sim.py steps the physics forward, then fabric.py steps the network (the DSX Air role)
  2. the results are written as "overs" (USD's word for "change these values on a prim
     defined somewhere else"): physics into live/telemetry.usda, network into live/network.usda
  3. the browser asks for state, and gets it by reading the COMPOSED stage

The UI never talks to the simulation directly. USD sits in the middle as the shared
source of truth. That's the Omniverse pattern: simulators, sensors and apps all read
and write the same stage. In Omniverse, Kit does this job and Nucleus or a live layer
shares the stage between apps.
"""

import json
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pxr import Sdf, Usd, UsdGeom, UsdShade

from build_factory import build
from fabric import PROPOSAL, Fabric
from sim import Sim

HERE = Path(__file__).parent
PORT = 8765
SAVE_EVERY = 5  # seconds between writes of the live layer to disk

VALUE_TYPES = {float: Sdf.ValueTypeNames.Float, int: Sdf.ValueTypeNames.Int,
               str: Sdf.ValueTypeNames.String, bool: Sdf.ValueTypeNames.Bool}


def short_layer(layer):
    path = Path(layer.realPath) if layer.realPath else None
    if path and HERE in path.parents:
        return path.relative_to(HERE).as_posix()
    return layer.identifier


def plain(value):
    """USD values to JSON-friendly Python values."""
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 3)
    return str(value)


class Twin:
    def __init__(self):
        root = build()                               # rebuilt on every start, so edits to build_factory.py show up
        self.stage = Usd.Stage.Open(str(root))
        # One live layer per simulator, so they never write over each other.
        self.layers = {name: Sdf.Layer.FindOrOpen(str(HERE / "live" / f"{name}.usda")) for name in ("telemetry", "network")}
        self.lock = threading.RLock()
        self.muted = set()
        self.objects = self._find_objects()
        topo = self._topology()
        self.sim = Sim(topo)
        self.fabric = Fabric(topo, self.sim)
        self.scene = self._scene()

    # Reading the design from USD -------------------------------------------------

    def _find_objects(self):
        """Every prim with facility:kind is something you can select."""
        objects = {}
        for prim in self.stage.Traverse():
            kind = prim.GetAttribute("facility:kind")
            if kind and kind.Get():
                rels = {r.GetName().split(":", 1)[1]: [str(t) for t in r.GetTargets()]
                        for r in prim.GetRelationships() if r.GetName().startswith("facility:")}
                objects[str(prim.GetPath())] = {"kind": kind.Get(), "name": prim.GetName(), "rels": rels}
        return objects

    def _topology(self):
        """Turn USD relationships into the plain dict the simulation needs."""
        get = lambda path, name: self.stage.GetPrimAtPath(path).GetAttribute(f"facility:{name}").Get()
        topo = {"racks": {}, "busways": {}, "cdus": {}, "chillers": {}, "ups": {}, "leaves": {}, "spines": {}}
        for path, o in self.objects.items():
            rels = o["rels"]
            if o["kind"] == "rack":
                topo["racks"][path] = {"max": get(path, "maxPowerKW"), "idle": get(path, "idlePowerKW"),
                                       "liquid": get(path, "liquidFraction"),
                                       "busway": rels["poweredBy"][0],
                                       "cdu": rels.get("cooledBy", [None])[0],
                                       "leaf": rels["uplink"][0],
                                       "links": get(path, "fabricLinks"), "link": get(path, "linkGbps")}
            elif o["kind"] == "busway":
                topo["busways"][path] = {"ups": rels["poweredBy"][0], "cap": get(path, "capacityKW")}
            elif o["kind"] == "cdu":
                topo["cdus"][path] = {"cap": get(path, "capacityKW"),
                                      "design_supply": get(path, "designSupplyC"), "racks": []}
            elif o["kind"] == "chiller":
                topo["chillers"][path] = {"cap": get(path, "capacityKW"), "cop": get(path, "cop")}
            elif o["kind"] == "ups":
                topo["ups"][path] = {"cap": get(path, "capacityKW"), "eff": get(path, "efficiency")}
            elif o["kind"] == "leaf":
                topo["leaves"][path] = {"busway": rels["poweredBy"][0], "spines": rels["uplink"],
                                        "per_spine": get(path, "uplinksPerSpine"), "link": get(path, "linkGbps"),
                                        "racks": []}
            elif o["kind"] == "spine":
                topo["spines"][path] = {"ups": rels["poweredBy"][0], "links": get(path, "linksTotal"),
                                        "link": get(path, "linkGbps")}
        for path, rack in topo["racks"].items():
            if rack["cdu"]:
                topo["cdus"][rack["cdu"]]["racks"].append(path)
            topo["leaves"][rack["leaf"]]["racks"].append(path)
        return topo

    def _scene(self):
        """Everything the browser needs to draw the stage, shaped the way USD stores it.

        Instanceable racks share a *prototype*: USD composes the rack asset once and all 80
        racks point at it. We send each prototype's parts once, plus one transform per
        instance, and the browser turns that straight into GPU instancing (three.js
        InstancedMesh). That's the same trick that lets Omniverse draw huge halls."""
        xf = UsdGeom.XformCache()
        looks = {}        # material values -> id, so identical looks are shared
        prototypes = {}   # prototype path -> parts, in the prototype's own space
        instances = []    # one per instanceable prim: which prototype, where, and who owns it
        parts = []        # everything that isn't inside an instance, in world space

        for prim in Usd.PrimRange(self.stage.GetPseudoRoot()):   # doesn't descend into instances
            if prim.IsInstance():
                proto = prim.GetPrototype()
                key = str(proto.GetPath())
                if key not in prototypes:
                    prototypes[key] = [p for p in (self._part(q, xf, looks) for q in Usd.PrimRange(proto)) if p]
                instances.append({"proto": key, "owner": str(prim.GetPath()),
                                  "matrix": self._flat(xf.GetLocalToWorldTransform(prim))})
                continue
            part = self._part(prim, xf, looks)
            if part:
                owner = prim.GetParent()
                while owner and not owner.GetAttribute("facility:kind"):
                    owner = owner.GetParent()
                part["owner"] = str(owner.GetPath()) if owner and owner.GetPath() != Sdf.Path.absoluteRootPath else None
                parts.append(part)

        return {"prototypes": prototypes, "instances": instances, "parts": parts,
                "looks": [json.loads(k) for k in looks], "objects": self.objects}   # list index = look id

    @staticmethod
    def _flat(matrix):
        return [round(v, 5) for row in matrix for v in row]

    OVERHEAD = ("/World/Hall/Power/", "/World/Hall/Overhead/", "/World/Hall/Cooling/Piping/", "/World/Hall/Containment/")

    def _part(self, prim, xf, looks):
        """One drawable prim: its shape, transform, material and role in the twin."""
        if prim.IsA(UsdGeom.Cube):
            shape = {"shape": "cube", "size": UsdGeom.Cube(prim).GetSizeAttr().Get()}
        elif prim.IsA(UsdGeom.Cylinder):
            c = UsdGeom.Cylinder(prim)
            shape = {"shape": "cylinder", "radius": c.GetRadiusAttr().Get(), "height": c.GetHeightAttr().Get(),
                     "axis": c.GetAxisAttr().Get()}
        else:
            return None
        if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
            return None
        path = str(prim.GetPath())
        names = path.split("/")
        role = "led" if prim.GetName() == "StatusLed" else "body" if ("Shell" in names or prim.GetName() == "Body") else "detail"
        look = json.dumps(self._look(prim), sort_keys=True)
        look_id = looks.setdefault(look, len(looks))
        return {**shape, "matrix": self._flat(xf.GetLocalToWorldTransform(prim)), "look": look_id, "role": role,
                "overhead": path.startswith(self.OVERHEAD)}

    @staticmethod
    def _look(prim):
        """Read the bound UsdPreviewSurface material, or fall back to displayColor."""
        values = {"color": [0.18, 0.18, 0.18], "metallic": 0.0, "roughness": 0.5, "emissive": None, "opacity": 1.0}
        material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        shader = material.ComputeSurfaceSource()[0] if material else None
        if shader:
            for usd_name, key in (("diffuseColor", "color"), ("metallic", "metallic"), ("roughness", "roughness"),
                                  ("emissiveColor", "emissive"), ("opacity", "opacity")):
                inp = shader.GetInput(usd_name)
                value = inp.Get() if inp else None
                if value is not None:
                    values[key] = [round(c, 4) for c in value] if hasattr(value, "__len__") else round(value, 4)
        else:
            colour = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get()
            if colour:
                values["color"] = [round(c, 4) for c in colour[0]]
        return values

    # Writing live values ------------------------------------------------------------

    def write_live(self, layer, snapshot):
        """Author overs in a live layer. Sdf is USD's low-level layer API: fast, and it
        edits one layer directly instead of going through the composed stage."""
        with Sdf.ChangeBlock():
            for path, values in snapshot.items():
                spec = Sdf.CreatePrimInLayer(layer, path)
                for name, value in values.items():
                    attr_name = f"facility:{name}"
                    attr = spec.attributes.get(attr_name)
                    if not attr:
                        attr = Sdf.AttributeSpec(spec, attr_name, VALUE_TYPES[type(value)])
                        attr.custom = True
                    attr.default = value

    def tick(self):
        with self.lock:
            self.sim.step()
            self.fabric.step()     # after physics: it reads which racks are running or throttled
            self.write_live(self.layers["telemetry"], self.sim.snapshot())
            self.write_live(self.layers["network"], self.fabric.snapshot())
            if self.sim.t % SAVE_EVERY == 0:
                for layer in self.layers.values():
                    layer.Save()

    # What the browser asks for --------------------------------------------------------

    def state(self):
        """Read every facility value from the composed stage. Mute the live layer and
        these become the design values from layout.usda."""
        with self.lock:
            values = {}
            for path in list(self.objects) + ["/World"]:
                prim = self.stage.GetPrimAtPath(path)
                values[path] = {a.GetName().split(":", 1)[1]: plain(a.Get())
                                for a in prim.GetAttributes()
                                if a.GetName().startswith("facility:") and a.HasValue()}
            fabric = {"proposal": PROPOSAL, "pushed": PROPOSAL["leaf"] in self.fabric.bad_config,
                      "dryRun": self.fabric.dry_run_result}
            return {"values": values, "events": list(self.sim.events), "surge": self.sim.surge,
                    "offline": sorted(self.sim.offline), "muted": sorted(self.muted), "t": self.sim.t,
                    "fabric": fabric}

    def prim(self, path):
        """A teardown of one prim: which layers have opinions about it, and what each one says."""
        with self.lock:
            prim = self.stage.GetPrimAtPath(path)
            if not prim:
                return None
            attrs = []
            for a in prim.GetAttributes():
                if not a.GetName().startswith("facility:") or not a.HasValue():
                    continue
                stack = a.GetPropertyStack(Usd.TimeCode.Default())
                attrs.append({"name": a.GetName(), "value": plain(a.Get()),
                              "type": str(a.GetTypeName()),
                              "source": short_layer(stack[0].layer) if stack else "fallback",
                              "layers": [short_layer(s.layer) for s in stack]})
            arcs = []
            for arc in Usd.PrimCompositionQuery(prim).GetCompositionArcs():
                node = arc.GetTargetNode()
                arc_type = str(arc.GetArcType()).replace("Pcp.ArcType", "")
                arcs.append({"type": arc_type,
                             "layer": "factory.usda and its sublayers" if arc_type == "Root"
                             else short_layer(node.layerStack.layers[0]),
                             "path": str(node.path), "hasSpecs": arc.HasSpecs()})
            stack = []
            for spec in prim.GetPrimStack():
                stack.append({"layer": short_layer(spec.layer), "path": str(spec.path),
                              "usda": self._spec_text(spec)})
            variants = {name: prim.GetVariantSets().GetVariantSet(name).GetVariantSelection()
                        for name in prim.GetVariantSets().GetNames()}
            return {"path": path, "type": prim.GetTypeName(), "kind": Usd.ModelAPI(prim).GetKind(),
                    "instance": prim.IsInstance(), "attrs": attrs, "arcs": arcs,
                    "stack": stack, "variants": variants}

    @staticmethod
    def _spec_text(spec):
        """Copy one prim spec into a scratch layer and print it as .usda text."""
        try:
            scratch = Sdf.Layer.CreateAnonymous(".usda")
            if spec.path.IsPrimVariantSelectionPath():
                # e.g. /Rack{cooling=liquid}: rebuild the variant set around it first
                vset, choice = spec.path.GetVariantSelection()
                owner = Sdf.CreatePrimInLayer(scratch, spec.path.GetParentPath().StripAllVariantSelections())
                Sdf.VariantSpec(Sdf.VariantSetSpec(owner, vset), choice)
            elif spec.path.GetParentPath() != Sdf.Path.absoluteRootPath:
                Sdf.CreatePrimInLayer(scratch, spec.path.GetParentPath())
            Sdf.CopySpec(spec.layer, spec.path, scratch, spec.path)
            return scratch.ExportToString().split("\n", 1)[1].strip()
        except Exception as exc:  # variant specs and other edge cases
            return f"# Could not print this spec ({exc}).\n# Open {short_layer(spec.layer)} and find {spec.path}."

    def control(self, body):
        with self.lock:
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

    def _mute(self, name, on):
        """Muting a layer removes it from composition without deleting it."""
        if name not in self.layers or on == (name in self.muted):
            return
        layer = f"live/{name}.usda"
        if on:
            self.stage.MuteLayer(self.layers[name].identifier)
            self.muted.add(name)
            self.sim.log(f"{layer} muted. Its values fall back to the layers underneath.", "warn")
        else:
            self.stage.UnmuteLayer(self.layers[name].identifier)
            self.muted.discard(name)
            self.sim.log(f"{layer} back on.")


class Handler(SimpleHTTPRequestHandler):
    twin = None

    def log_message(self, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/scene":
            return self.send_json(self.twin.scene)
        if url.path == "/api/state":
            return self.send_json(self.twin.state())
        if url.path == "/api/prim":
            data = self.twin.prim(parse_qs(url.query).get("path", [""])[0])
            return self.send_json(data or {"error": "No prim at that path"}, 200 if data else 404)
        if url.path == "/api/live.usda":
            name = parse_qs(url.query).get("layer", ["telemetry"])[0]
            layer = self.twin.layers.get(name)
            if not layer:
                return self.send_json({"error": "No such layer"}, 404)
            with self.twin.lock:
                return self.send_json({"usda": layer.ExportToString()})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/control":
            return self.send_json({"error": "Not found"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        self.twin.control(json.loads(self.rfile.read(length) or b"{}"))
        return self.send_json(self.twin.state())


def main():
    twin = Twin()
    Handler.twin = twin

    def loop():
        while True:
            start = time.monotonic()
            twin.tick()
            time.sleep(max(0.0, 1.0 - (time.monotonic() - start)))

    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(HERE / "web")))
    url = f"http://localhost:{PORT}"
    print(f"AI factory twin running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. The last live values are saved in live/")
        with twin.lock:
            for layer in twin.layers.values():
                layer.Save()


if __name__ == "__main__":
    main()
