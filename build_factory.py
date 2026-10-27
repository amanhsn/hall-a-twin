"""Build the sample AI factory as OpenUSD layers.

Run:  .venv/bin/python build_factory.py

It writes four files:
  layout.usda          - the design: every rack, CDU, busway, UPS, chiller and switch. Never changes at runtime.
  live/telemetry.usda  - an empty layer. The server writes live physics readings here as "overs".
  live/network.usda    - another empty layer, for the network simulation (fabric.py).
  factory.usda         - the root. It stacks both live layers on top of layout, so live values win.

USD ideas used here (see TEARDOWN.md):
  references     - every rack points to one asset file instead of copying its geometry 80 times
  instancing     - the renderer shares that geometry in memory too
  variants       - Row H picks the air-cooled build of the same asset
  relationships  - poweredBy / cooledBy / uplink wire up the electrical, cooling and network topology
  materials      - UsdPreviewSurface looks, bound to geometry with MaterialBindingAPI
  sublayers      - design data and live data live in separate files
"""

from pathlib import Path

from pxr import Gf, Kind, Sdf, Usd, UsdGeom, UsdShade

HERE = Path(__file__).parent
ROWS = "ABCDEFGH"
RACKS_PER_ROW = 10
RACK_PITCH = 0.6   # metres between rack centres
ROW_PITCH = 3.0    # metres between row centres
AIR_ROWS = {"H"}   # a legacy air-cooled row, to show the variant
DESIGN_UTILIZATION = 0.75

# Network: a leaf switch rack at the end of every row, two spines in the network room.
LINK_GBPS = 800.0
UPLINKS_PER_SPINE = 40   # each leaf has 40 x 800G to each spine: 64 Tb/s up, the same as its 10 racks send down

# Heights of the overhead services, in metres. Racks are 2.3 m tall.
SUPPLY_Z = 2.48    # coolant supply header (the rack asset's supply hose rises to here)
RETURN_Z = 2.62    # coolant return header
BUSWAY_Z = 2.62
TRAY_Z = 3.05      # fibre trays over the cold aisles

# Shared looks for everything built here. Colours are linear RGB, like USD expects.
LOOKS = {
    "Concrete":      dict(color=(0.032, 0.034, 0.036), roughness=0.85),
    "Yard":          dict(color=(0.018, 0.019, 0.018), roughness=0.95),
    "Wall":          dict(color=(0.07, 0.075, 0.08), roughness=0.8),
    "SafetyYellow":  dict(color=(0.75, 0.5, 0.02), roughness=0.6),
    "Busway":        dict(color=(0.42, 0.33, 0.12), metallic=0.8, roughness=0.4),
    "TapOff":        dict(color=(0.35, 0.36, 0.37), metallic=0.6, roughness=0.45),
    "Cable":         dict(color=(0.01, 0.01, 0.01), roughness=0.7),
    "FibreTray":     dict(color=(0.8, 0.55, 0.0), roughness=0.55),
    "Steel":         dict(color=(0.32, 0.33, 0.34), metallic=0.9, roughness=0.32),
    "Glass":         dict(color=(0.5, 0.6, 0.65), roughness=0.08, opacity=0.16),
    "CoolantBlue":   dict(color=(0.02, 0.16, 0.6), metallic=0.15, roughness=0.35),
    "CoolantRed":    dict(color=(0.62, 0.07, 0.03), metallic=0.15, roughness=0.35),
    "FacilityGreen": dict(color=(0.03, 0.3, 0.1), metallic=0.2, roughness=0.4),
    "FacilityGrey":  dict(color=(0.3, 0.31, 0.32), metallic=0.5, roughness=0.4),
}


def define_looks(stage):
    """One UsdPreviewSurface material per entry in LOOKS, under /World/Looks."""
    UsdGeom.Scope.Define(stage, "/World/Looks")
    for name, p in LOOKS.items():
        mat = UsdShade.Material.Define(stage, f"/World/Looks/{name}")
        shader = UsdShade.Shader.Define(stage, f"/World/Looks/{name}/Surface")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*p["color"]))
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(p.get("metallic", 0.0))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(p.get("roughness", 0.5))
        if "opacity" in p:
            shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(p["opacity"])
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")


def bind(stage, prim, look):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material.Get(stage, f"/World/Looks/{look}"))


def box(stage, path, centre, size, look):
    """A plain box prim, for things that don't need their own asset file."""
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    cube.AddTranslateOp().Set(Gf.Vec3d(*centre))
    cube.AddScaleOp().Set(Gf.Vec3f(*size))
    bind(stage, cube.GetPrim(), look)
    return cube


def pipe(stage, path, start, end, radius, look):
    """A straight, axis-aligned pipe from start to end: a UsdGeom.Cylinder."""
    delta = [b - a for a, b in zip(start, end)]
    axis = max(range(3), key=lambda i: abs(delta[i]))
    cyl = UsdGeom.Cylinder.Define(stage, path)
    cyl.CreateAxisAttr("XYZ"[axis])
    cyl.CreateHeightAttr(abs(delta[axis]))
    cyl.CreateRadiusAttr(radius)
    cyl.AddTranslateOp().Set(Gf.Vec3d(*[(a + b) / 2 for a, b in zip(start, end)]))
    bind(stage, cyl.GetPrim(), look)
    return cyl


def tag(prim, kind, **data):
    """Facility data on a prim. facility:kind marks it as something you can select in the twin."""
    prim.CreateAttribute("facility:kind", Sdf.ValueTypeNames.String).Set(kind)
    types = {float: Sdf.ValueTypeNames.Float, int: Sdf.ValueTypeNames.Int,
             str: Sdf.ValueTypeNames.String, bool: Sdf.ValueTypeNames.Bool}
    for name, value in data.items():
        prim.CreateAttribute(f"facility:{name}", types[type(value)]).Set(value)


def place(stage, path, asset, centre, kind=None, **data):
    """Reference an asset file and put it somewhere."""
    prim = stage.DefinePrim(path)
    prim.GetReferences().AddReference(asset)
    UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(*centre))
    if kind:
        tag(prim, kind, **data)
    return prim


def link(prim, name, *targets):
    prim.CreateRelationship(f"facility:{name}").SetTargets([Sdf.Path(t) for t in targets])


def build_layout(path):
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World").GetPrim()
    stage.SetDefaultPrim(world)
    Usd.ModelAPI(world).SetKind(Kind.Tokens.assembly)
    define_looks(stage)

    hall = UsdGeom.Xform.Define(stage, "/World/Hall").GetPrim()
    Usd.ModelAPI(hall).SetKind(Kind.Tokens.assembly)
    hall_len = (RACKS_PER_ROW - 1) * RACK_PITCH
    hall_depth = (len(ROWS) - 1) * ROW_PITCH
    row_x0, row_x1 = -RACK_PITCH / 2, hall_len + RACK_PITCH / 2   # ends of every row

    # The building: a slab floor and cutaway walls, so you can see in from any angle.
    fx0, fx1, fy0, fy1 = -2.25, hall_len + 5.25, -2.5, hall_depth + 2.5
    box(stage, "/World/Hall/Floor", ((fx0 + fx1) / 2, (fy0 + fy1) / 2, -0.05), (fx1 - fx0, fy1 - fy0, 0.1), "Concrete")
    for name, centre, size in (("WallWest", (fx0, (fy0 + fy1) / 2, 0.25), (0.2, fy1 - fy0, 0.5)),
                               ("WallEast", (fx1, (fy0 + fy1) / 2, 0.25), (0.2, fy1 - fy0, 0.5)),
                               ("WallSouth", ((fx0 + fx1) / 2, fy0, 0.25), (fx1 - fx0, 0.2, 0.5)),
                               ("WallNorth", ((fx0 + fx1) / 2, fy1, 0.25), (fx1 - fx0, 0.2, 0.5))):
        box(stage, f"/World/Hall/Walls/{name}", centre, size, "Wall")
    box(stage, "/World/GraySpace/Floor", (-5, hall_depth / 2, -0.05), (4.5, hall_depth + 5, 0.1), "Concrete")
    box(stage, "/World/Plant/Yard", (hall_len + 9, hall_depth / 2, -0.06), (8, hall_depth + 5, 0.1), "Yard")

    # Plant and gray space: outside the white space where the racks live.
    chillers, chiller_ys = [], (hall_depth * 0.25, hall_depth * 0.75)
    for i, y in enumerate(chiller_ys, start=1):
        place(stage, f"/World/Plant/Chiller_{i}", "./assets/chiller.usda", (hall_len + 9, y, 0),
              "chiller", capacityKW=6000.0, cop=6.0)
        place(stage, f"/World/GraySpace/UPS_{i}", "./assets/ups.usda", (-5, y, 0),
              "ups", capacityKW=6000.0, efficiency=0.96)
        chillers.append(f"/World/Plant/Chiller_{i}")

    # Spine switches, between the two UPS lineups. Each one is fed from a different UPS,
    # so losing one UPS never takes out the whole network core.
    spines = []
    for i in (1, 2):
        path = f"/World/GraySpace/Network/Spine_{i}"
        spine = place(stage, path, "./assets/switch_rack.usda", (-5.3 + 0.6 * (i - 1), hall_depth / 2, 0),
                      "spine", linkGbps=LINK_GBPS, linksTotal=UPLINKS_PER_SPINE * len(ROWS),
                      capacityGbps=LINK_GBPS * UPLINKS_PER_SPINE * len(ROWS))
        spine.SetInstanceable(True)
        link(spine, "poweredBy", f"/World/GraySpace/UPS_{i}")
        spines.append(path)

    # One CDU per pair of rows, at the end of the pair's cold aisle.
    cdu_x = hall_len + 1.8
    cdus = []
    for pair in range(len(ROWS) // 2):
        cdu_path = f"/World/Hall/Cooling/CDU_{pair + 1}"
        cdu = place(stage, cdu_path, "./assets/cdu.usda", (cdu_x, (pair * 2 + 0.5) * ROW_PITCH, 0))
        link(cdu, "cooledBy", *chillers)
        cdu.CreateAttribute("facility:supplyTempC", Sdf.ValueTypeNames.Float).Set(30.0)
        cdus.append(cdu_path)

    for r, row in enumerate(ROWS):
        y = r * ROW_PITCH
        ups = "/World/GraySpace/UPS_1" if r < 4 else "/World/GraySpace/UPS_2"

        # The busway: an overhead power rail, with one tap-off box and cable whip per rack.
        busway = f"/World/Hall/Power/Busway_{row}"
        bw = UsdGeom.Xform.Define(stage, busway).GetPrim()
        Usd.ModelAPI(bw).SetKind(Kind.Tokens.component)
        box(stage, f"{busway}/Body", (hall_len / 2, y, BUSWAY_Z), (hall_len + 0.8, 0.2, 0.15), "Busway")
        tag(bw, "busway", capacityKW=1600.0)
        link(bw, "poweredBy", ups)
        for i in range(RACKS_PER_ROW):
            box(stage, f"{busway}/TapOff_{i + 1:02d}", (i * RACK_PITCH, y, BUSWAY_Z - 0.15), (0.22, 0.14, 0.14), "TapOff")
            pipe(stage, f"{busway}/Whip_{i + 1:02d}", (i * RACK_PITCH, y, 2.3), (i * RACK_PITCH, y, BUSWAY_Z - 0.22), 0.015, "Cable")

        # The row's leaf switch rack, in an eleventh slot at the end of the row. It shares the row's busway.
        leaf = f"/World/Hall/Network/Leaf_{row}"
        lf = place(stage, leaf, "./assets/switch_rack.usda", (row_x1 + 0.45, y, 0),
                   "leaf", linkGbps=LINK_GBPS, uplinksPerSpine=UPLINKS_PER_SPINE,
                   linksTotal=UPLINKS_PER_SPINE * len(spines), capacityGbps=LINK_GBPS * UPLINKS_PER_SPINE * len(spines))
        if r % 2 == 0:
            UsdGeom.Xformable(lf).AddRotateZOp().Set(180.0)
        lf.SetInstanceable(True)
        link(lf, "poweredBy", busway)
        link(lf, "uplink", *spines)

        UsdGeom.Xform.Define(stage, f"/World/Hall/Rows/Row_{row}")
        for i in range(RACKS_PER_ROW):
            path = f"/World/Hall/Rows/Row_{row}/Rack_{row}{i + 1:02d}"
            rack = stage.DefinePrim(path)
            rack.GetReferences().AddReference("./assets/nvl72_rack.usda")   # geometry + base data from the asset
            if row in AIR_ROWS:
                rack.GetVariantSets().GetVariantSet("cooling").SetVariantSelection("air")
            rack.SetInstanceable(True)                                      # share that geometry in memory

            xf = UsdGeom.Xformable(rack)
            xf.AddTranslateOp().Set(Gf.Vec3d(i * RACK_PITCH, y, 0))
            if r % 2 == 0:                                                  # pairs of rows face each other
                xf.AddRotateZOp().Set(180.0)

            link(rack, "poweredBy", busway)
            link(rack, "uplink", leaf)
            if row not in AIR_ROWS:
                link(rack, "cooledBy", cdus[r // 2])

            # Design-time values. The live layer overrides these while the twin runs.
            max_kw = 40.0 if row in AIR_ROWS else 132.0
            design_kw = 30 + (max_kw - 30) * DESIGN_UTILIZATION
            rack.CreateAttribute("facility:powerKW", Sdf.ValueTypeNames.Float).Set(round(design_kw, 1))
            rack.CreateAttribute("facility:status", Sdf.ValueTypeNames.String).Set("design")

    # Coolant piping. Each liquid row has a supply and a return header running above the
    # backs of its racks, where the rack hoses come up. The headers cross over to the CDU
    # and drop into its top. (Real halls also have valves, supports and expansion loops.)
    for pair in range(len(ROWS) // 2):
        loop = f"/World/Hall/Cooling/Piping/Loop_{pair + 1}"
        cdu_y = (pair * 2 + 0.5) * ROW_PITCH
        supply_x, return_x = cdu_x - 0.2, cdu_x + 0.2
        ends = {"supply": [cdu_y], "return": [cdu_y]}
        for r in (pair * 2, pair * 2 + 1):
            row = ROWS[r]
            if row in AIR_ROWS:
                continue
            back = -1 if r % 2 == 0 else 1             # which way this row's backs face
            sy, ry = r * ROW_PITCH + back * 0.48, r * ROW_PITCH + back * 0.54
            pipe(stage, f"{loop}/Supply_{row}", (row_x0 - 0.05, sy, SUPPLY_Z), (supply_x, sy, SUPPLY_Z), 0.035, "CoolantBlue")
            pipe(stage, f"{loop}/Return_{row}", (row_x0 - 0.05, ry, RETURN_Z), (return_x, ry, RETURN_Z), 0.035, "CoolantRed")
            ends["supply"].append(sy)
            ends["return"].append(ry)
        pipe(stage, f"{loop}/SupplyCross", (supply_x, min(ends["supply"]) - 0.035, SUPPLY_Z),
             (supply_x, max(ends["supply"]) + 0.035, SUPPLY_Z), 0.035, "CoolantBlue")
        pipe(stage, f"{loop}/ReturnCross", (return_x, min(ends["return"]) - 0.035, RETURN_Z),
             (return_x, max(ends["return"]) + 0.035, RETURN_Z), 0.035, "CoolantRed")
        pipe(stage, f"{loop}/SupplyDrop", (supply_x, cdu_y, 2.1), (supply_x, cdu_y, SUPPLY_Z + 0.035), 0.035, "CoolantBlue")
        pipe(stage, f"{loop}/ReturnDrop", (return_x, cdu_y, 2.1), (return_x, cdu_y, RETURN_Z + 0.035), 0.035, "CoolantRed")

    # Facility water: two big pipes run along the hall, from every CDU out to both chillers.
    fw = "/World/Plant/FacilityWater"
    spine_s, spine_r = cdu_x + 1.4, cdu_x + 1.8
    ys = [(p * 2 + 0.5) * ROW_PITCH for p in range(len(ROWS) // 2)] + list(chiller_ys)
    pipe(stage, f"{fw}/SupplySpine", (spine_s, min(ys) - 1.0, 0.35), (spine_s, max(ys) + 0.4, 0.35), 0.09, "FacilityGreen")
    pipe(stage, f"{fw}/ReturnSpine", (spine_r, min(ys) - 1.0, 0.75), (spine_r, max(ys) + 0.4, 0.75), 0.09, "FacilityGrey")
    for n, path in enumerate(cdus, start=1):
        y = (n * 2 - 1.5) * ROW_PITCH
        pipe(stage, f"{fw}/CDU_{n}_Supply", (cdu_x + 0.7, y + 0.2, 0.35), (spine_s, y + 0.2, 0.35), 0.07, "FacilityGreen")
        pipe(stage, f"{fw}/CDU_{n}_Return", (cdu_x + 0.7, y - 0.2, 0.75), (spine_r, y - 0.2, 0.75), 0.07, "FacilityGrey")
    for n, y in enumerate(chiller_ys, start=1):
        x_end = hall_len + 9 - 2.95
        pipe(stage, f"{fw}/Chiller_{n}_Supply", (spine_s, y - 0.4, 0.35), (x_end, y - 0.4, 0.35), 0.11, "FacilityGreen")
        pipe(stage, f"{fw}/Chiller_{n}_Return", (spine_r, y - 0.8, 0.75), (x_end, y - 0.8, 0.75), 0.11, "FacilityGrey")

    # Hot-aisle containment: glass roof and doors over the aisles where rack backs face each other.
    for n, r in enumerate(range(1, len(ROWS) - 1, 2), start=1):
        aisle = f"/World/Hall/Containment/HotAisle_{n}"
        cy = (r + 0.5) * ROW_PITCH
        width = ROW_PITCH - 1.2
        box(stage, f"{aisle}/Roof", ((row_x0 + row_x1) / 2, cy, 2.31), (row_x1 - row_x0, width, 0.015), "Glass")
        for side in (-1, 1):
            box(stage, f"{aisle}/Rail{'S' if side < 0 else 'N'}", ((row_x0 + row_x1) / 2, cy + side * width / 2, 2.33),
                (row_x1 - row_x0, 0.04, 0.04), "Steel")
        for end, x in (("West", row_x0 - 0.02), ("East", row_x1 + 0.02)):
            box(stage, f"{aisle}/Door{end}", (x, cy, 1.15), (0.02, width, 2.3), "Glass")
            box(stage, f"{aisle}/Door{end}Frame", (x, cy, 1.15), (0.04, 0.05, 2.3), "Steel")
            box(stage, f"{aisle}/Door{end}Header", (x, cy, 2.29), (0.05, width, 0.04), "Steel")

    # Cold aisles: yellow floor lines along the rack fronts, a fibre tray overhead.
    for n, r in enumerate(range(0, len(ROWS), 2), start=1):
        cy = (r + 0.5) * ROW_PITCH
        for side in (-1, 1):
            box(stage, f"/World/Hall/Markings/ColdAisle_{n}_{'S' if side < 0 else 'N'}",
                ((row_x0 + row_x1) / 2, cy + side * 0.75, 0.002), (row_x1 - row_x0, 0.05, 0.004), "SafetyYellow")
        box(stage, f"/World/Hall/Overhead/FibreTray_{n}", ((row_x0 + row_x1) / 2, cy, TRAY_Z),
            (row_x1 - row_x0 + 0.4, 0.3, 0.1), "FibreTray")

    # Design totals for the whole site, so the twin has something to compare live values against.
    design_it = sum(p.GetAttribute("facility:powerKW").Get() for p in stage.Traverse()
                    if p.HasAttribute("facility:powerKW"))
    world.CreateAttribute("facility:itLoadKW", Sdf.ValueTypeNames.Float).Set(round(design_it, 1))
    world.CreateAttribute("facility:pue", Sdf.ValueTypeNames.Float).Set(1.22)
    world.CreateAttribute("facility:jobSpeedPct", Sdf.ValueTypeNames.Float).Set(100.0)
    world.CreateAttribute("facility:jobRacks", Sdf.ValueTypeNames.Int).Set(len(ROWS) * RACKS_PER_ROW)

    stage.GetRootLayer().Save()
    return stage


def build_root(path, live_layers, layout_path):
    comments = {"telemetry": "Live physics telemetry from sim.py. Written by server.py every second as overs on top of layout.usda.",
                "network": "Live network state from fabric.py, the stand-in for DSX Air. Its own layer, so it never collides with physics."}
    root = Sdf.Layer.CreateNew(str(path))
    root.comment = "Root of the twin. Stronger layers come first."
    for name, live_path in live_layers.items():
        live = Sdf.Layer.CreateNew(str(live_path))
        live.comment = comments[name]
        live.Save()
        root.subLayerPaths.append("./" + live_path.relative_to(path.parent).as_posix())
    root.subLayerPaths.append("./" + layout_path.name)
    root.defaultPrim = "World"
    root.Save()


def build():
    (HERE / "live").mkdir(exist_ok=True)
    layout = HERE / "layout.usda"
    live = {"network": HERE / "live" / "network.usda", "telemetry": HERE / "live" / "telemetry.usda"}
    root = HERE / "factory.usda"
    for f in (layout, root, *live.values()):
        f.unlink(missing_ok=True)
    build_layout(layout)
    build_root(root, live, layout)
    return root


if __name__ == "__main__":
    root = build()
    stage = Usd.Stage.Open(str(root))
    racks = [p for p in stage.Traverse() if p.GetAttribute("facility:kind").Get() == "rack"]
    total = sum(p.GetAttribute("facility:maxPowerKW").Get() for p in racks)
    print(f"Built {root.name}: {len(racks)} racks, {total / 1000:.1f} MW peak IT load")
