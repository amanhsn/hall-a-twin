# Hall A twin: teardown

A small but real digital twin of an AI factory hall, built the way Omniverse twins are built.
80 GB200 NVL72-class racks (about 9.6 MW peak), 4 CDUs, 8 busways, 2 UPS systems, 2 chillers,
and a leaf-spine network fabric (8 leaf switch racks, 2 spines) with its own simulation.

```
cd hall-a-twin
.venv/bin/python server.py        # opens http://localhost:8765
```

Ctrl+C stops it. The server rebuilds the factory every time it starts, so edits to
`build_factory.py` or the asset files show up after a restart.

---

## The architecture in one picture

```
 assets/*.usda            hand-written equipment (rack, CDU, switch rack)
      │  referenced by
      ▼
 layout.usda              the design: racks, CDUs, busways, UPS, chillers, switches + relationships
      │  sublayered under          ▲ topology read once
      ▼                            │
 live/telemetry.usda  ◀── server.py ◀── sim.py      physics step every second
      │  sublayered under          │
      ▼                            │
 live/network.usda    ◀── server.py ◀── fabric.py   network step every second (the DSX Air role)
      │  sublayered under
      ▼
 factory.usda             the root. The browser only ever reads THIS composed stage.
```

| This sample | What Omniverse uses instead |
|---|---|
| `assets/*.usda` written by hand | SimReady assets from Vertiv, Schneider Electric and others |
| `build_factory.py` | Layout tools in USD Composer, or CAD/BIM converters |
| `sim.py` | Cadence (CFD/thermal), ETAP (electrical), PhysicsNeMo surrogates, via the DSX Blueprint |
| `fabric.py` | NVIDIA DSX Air: a logical simulation of the switches, NICs and DPUs and their software, driven through its Python SDK and REST API |
| `server.py` writing a live layer | Kit extensions and live layers, shared through Nucleus |
| `web/` with three.js | Kit app streaming RTX views into a web front end |

The shape is the same. Only the parts get more powerful.

---

## Lesson 1: run it and break things (20 min)

Open the app and do the "Try this" list in the right-hand panel. Then answer these:

1. Fail CDU_2. How long until rows C and D start throttling? Why doesn't it happen instantly?
   (Hint: look for `0.06` in `sim.py`.)
2. Turn on the surge with everything online. Nothing goes red. Now fail Chiller_1. Why does
   one chiller cope at normal load but not at peak?
3. Fail UPS_2. Which rows go dark? Use the **Topology** tab on UPS_2 to check.
4. Compare PUE at full load and after failing UPS_2. Why does PUE get *worse* when less IT is running?

## Lesson 2: an asset (40 min)

Open `assets/nvl72_rack.usda`. It's long, but it's made of only five ideas, so skim by section:

- `defaultPrim = "Rack"` says what you get when someone references this file.
- `kind = "component"` marks it as a reusable model.
- **Looks** holds `UsdPreviewSurface` materials: diffuse colour, metallic, roughness, and
  `emissiveColor` for the status lights. Each part binds one with `rel material:binding`.
  This is the portable material model every USD app understands. Omniverse adds MDL on top.
- **Parts** holds `class` prims: one compute tray, one switch tray, one power shelf.
  `class` means abstract: it isn't drawn itself. The **Front** stack *internally references*
  them 36 times (`prepend references = </Rack/Parts/ComputeTray>`). Change the tray once and
  all 18 compute trays change, in all 80 racks.
- **Shell** (side panels and roof) is what the twin tints with live data. Everything else
  keeps its real material, so you can see the hardware and the data at once.
- The `cooling` **variant set** switches geometry *and* data together: the liquid build has
  manifolds, hoses and a 132 kW limit, and the air build has a rear door, fans and a 40 kW
  limit. Notice that the air variant uses `over "Shell"` to add a rear door to a prim
  defined outside the variant.

The CDU, chiller and UPS assets follow the same pattern, and are much shorter.

**Exercise:** add a `"rearDoorHX"` variant (a rear-door heat exchanger): 60 kW max,
`liquidFraction` 0.6, with a door-sized panel bound to `CoolantBlue`. Set Row G to use it in
`build_factory.py`. Restart, switch the colour mode to **Realistic**, and fly to Rack_G01.

**Exercise:** give the compute tray a second status light. Edit `Parts/ComputeTray` only,
restart, and count how many lights changed.

## Lesson 3: references, instancing and relationships (45 min)

Read `build_factory.py`, then open `layout.usda` and search for `Rack_A01`.

- `prepend references = @./assets/nvl72_rack.usda@` means 80 racks share one source file.
  Fix a typo in the asset and all 80 change.
- `instanceable = true` means USD composes the rack once, as a **prototype**, and all 80
  racks point at it (the liquid and air builds get one prototype each). `server.py` sends
  each prototype once plus one transform per rack, and `app.js` turns that straight into GPU
  instancing with three.js `InstancedMesh`: about 15,000 parts in under 60 draw calls.
  Real halls have hundreds of thousands of instanced objects, and this is how they stay fast.
- The overhead services are all plain prims in `layout.usda`: coolant headers
  (`UsdGeom.Cylinder`, see `pipe()`), busway tap-offs, fibre trays, glass hot-aisle
  containment. Toggle **Overhead services** in the app to see the racks underneath.
- `rel facility:poweredBy = </World/Hall/Power/Busway_A>` is a **relationship**: a typed
  pointer to another prim. The whole electrical and cooling topology is stored this way, in USD.

**Exercise:** add a second hall row pair (rows I and J) with a fifth CDU. Change `ROWS`
and check that the CDU count follows. Then watch IT load in the header after a restart.

## Lesson 4: layers and overs (30 min)

This is the most important idea in the sample.

- `factory.usda` holds no prims at all. It only lists three sublayers, strongest first
  (`live/network.usda`, `live/telemetry.usda`, `layout.usda`).
- `live/telemetry.usda` contains only `over` prims: "for the prim defined elsewhere at this
  path, use these values". Click **Open telemetry.usda** in the app to see it change.
- In the inspector's **Live data** tab, every attribute shows the layer its value comes from.

**Exercise:** turn off the **Physics layer** in the app. The chips switch from `live/telemetry.usda` to
`layout.usda`, and the header shows design values. Nothing in the UI code changed. That's
why Omniverse apps can mix simulators, sensors and people editing the same stage without
stepping on each other: each writes to its own layer.

## Lesson 5: the simulation (45 min)

Read `sim.py`. The cause-and-effect chain is at the top of the file. Every constant has a name.

**Exercises:**
1. Change `RACK_DELTA_T` from 12 to 15. Which scenarios now go red that didn't before?
2. Add a scenario: "Facility water too warm", which raises every CDU's design supply by 6 °C.
   Wire it to a new switch in `web/index.html` and a new action in `server.py`'s `control()`.
3. Advanced: make each rack's `offset` drift over time, so the hall has hot spots that move.

## Lesson 6: the network twin, or what DSX Air does (45 min)

Everything up to now has been the *physical* twin: heat, power, geometry. An AI factory has a
second twin, the *logical* one: the network that turns 80 racks into one computer. NVIDIA's
version is DSX Air. `fabric.py` is a stand-in for it, the same way `sim.py` stands in for
Cadence and ETAP. It is not connected to DSX Air; that needs an account and its API.

Read `fabric.py`. The cause-and-effect chain is at the top. Then look at what changed elsewhere:

- `assets/switch_rack.usda` is a new asset with the same five ideas as the rack. One file is used
  for both the leaves (an eleventh slot at the end of each row) and the spines (between the UPSs).
- `rel facility:uplink` is a third relationship next to `poweredBy` and `cooledBy`:
  rack → leaf → both spines. Open the **Topology** tab on any rack to see its network path.
- Leaves are `poweredBy` their row's busway, and each spine by a different UPS. So the physical and
  logical twins are wired together *in USD*: fail UPS_2 and you lose Spine_2 as well as rows E to H.
- `factory.usda` now stacks **two** live layers. Each simulator writes only to its own. Mute
  them separately in **USD layers** and watch the source chips in the inspector.
- The **Fabric** button (or N, then H to go back) draws the same prims as a graph. Clicking either
  view selects the same USD path. That shared path is how one twin can serve both the facility
  team and the network team.

Then try these, and answer the questions:

1. Fail Spine_1. Nothing loses power and nothing gets hot. Why does the whole job slow down, and
   why only to about 92%? (Hint: `COMM_SHARE`.) Turn on the surge. Why does it get worse?
2. Fail CDU_2 and watch the **Training job** KPI. Throttling rows C and D first slows the *whole*
   job to 70%. When those racks shut down, the job gets *faster* (75%, on 60 racks). Why?
   What does that tell an operator about draining hot racks instead of letting them limp along?
3. Click **Dry run in simulation** on NET-2041, then push it anyway. Look at Leaf_F in the hall:
   every port is up and it's green in **Status** mode. Switch to **Network** mode. Which of the
   two would your monitoring wall show at 3 a.m.? This gap is the reason a logical twin exists.

**Exercises:**
1. Add a third spine. Change `build_factory.py` only. Does Spine_1 failing still hurt as much?
2. Make `REJOIN_S` 0 and fail CDU_2. Read the event log. What problem does the delay solve?
3. Advanced: racks that drop out of the job still burn power in `sim.py`. Feed `inJob` back so an
   idle rack's utilization falls to 10%. You'll need to decide which simulator owns that value,
   and why it shouldn't be both.

## Lesson 7: the interface (your strongest area)

Read `web/app.js`. It's plain JavaScript and three.js, with no framework.

- `/api/scene` sends prototypes, instances, world-space parts and materials. The note in
  `buildScene` explains why a USD matrix drops straight into three.js.
- Colour modes are just functions from USD values to colours (`colorFor`). **Realistic**
  shows the materials as authored. The status lights (`ledColor`) show rack status in every mode.
- The **Camera** section is worth reading as a designer: zoom heads for the point under the
  cursor, and a trackpad pans with two fingers and zooms with a pinch, like Figma.
  `onWheel` has the heuristic that tells a mouse wheel from a trackpad, and where it can fail.

| Input | Does |
|---|---|
| Drag | Orbit |
| Right-drag, shift-drag, two-finger scroll | Pan across the floor |
| Pinch, ⌘-scroll (or the wheel, in "Scroll zooms" mode) | Zoom toward the cursor |
| W A S D / arrows | Move · Q E turn |
| Double-click | Select and fly to it (F flies to the current selection) |
| 1 / 2 / 3 | Overview, top, cold-aisle eye level |

**Design exercise (this becomes week 6 of your sprint):** this UI was built for learning,
not for an operator. List what a shift engineer actually needs at 3 a.m. during a CDU
failure, then redesign this screen in Figma. Which rows are at risk, how long until they
throttle, what to do next.

**Second design exercise:** the network-change panel is a first pass at a change-review
flow. Redesign it for a network engineer who has 12 changes queued for tonight's window.
What does a dry-run result need to show so they trust it, and what happens when the
physical twin and the logical twin disagree?

---

## In week 3

The same `factory.usda` opens in Omniverse USD Composer on your cloud GPU. Open it, select
`Rack_A01`, and you'll see the same composition arcs and `facility:` attributes in the
property panel. Then you'll rebuild the inspector as a Kit extension.
