// The twin's front end. It only knows what the server reads out of the USD stage:
//   /api/scene  every Cube prim with its world matrix (drawn once)
//   /api/state  composed facility:* values for every selectable prim (polled each second)
//   /api/prim   a teardown of one prim: arcs, layer stack, and where each value comes from

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const short = (p) => p.split("/").pop();
const clock = (t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));

const api = {
  get: (url) => fetch(url).then((r) => r.json()),
  control: (body) =>
    fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json()),
};

const KIND_LABEL = { rack: "Rack", cdu: "Coolant distribution unit", busway: "Busway", ups: "UPS", chiller: "Chiller", leaf: "Leaf switch", spine: "Spine switch" };
const FAILABLE = [
  ["/World/Hall/Cooling/CDU_2", "CDU_2", "Cools rows C and D"],
  ["/World/Plant/Chiller_1", "Chiller_1", "Half the heat rejection"],
  ["/World/Hall/Power/Busway_C", "Busway_C", "Feeds row C"],
  ["/World/GraySpace/UPS_2", "UPS_2", "Feeds rows E to H and Spine_2"],
  ["/World/GraySpace/Network/Spine_1", "Spine_1", "Half of every leaf's uplinks"],
  ["/World/Hall/Network/Leaf_D", "Leaf_D", "Row D's network switch"],
];
const MODES = [["temp", "Temperature"], ["power", "Power"], ["status", "Status"], ["net", "Network"], ["loop", "Cooling loop"], ["feed", "Power feed"], ["look", "Realistic"]];
const STATUS_VAR = { ok: "--ok", warn: "--warn", throttled: "--throttled", shutdown: "--shutdown", "no power": "--nopower", maintenance: "--maint", design: "--muted" };
// Network states, written by fabric.py. Racks: can they reach the job? Switches: is traffic getting through?
const NET_VAR = { ok: "--ok", degraded: "--warn", isolated: "--shutdown", offline: "--nopower", "no power": "--nopower", maintenance: "--maint" };
const isSwitch = (path) => objects[path]?.kind === "leaf" || objects[path]?.kind === "spine";
const LOOP_COLORS = ["#5BC3D8", "#A68BF5", "#E6AE4A", "#6DBE8C"];
const FEED_COLORS = { UPS_1: "#5BC3D8", UPS_2: "#E6AE4A" };
const TEMP_RAMP = [[32, "#2D6A8A"], [40, "#3BA59B"], [45, "#E6AE4A"], [50, "#EF8350"], [56, "#E5484D"]];
const LOAD_RAMP = [[0, "#1D3740"], [0.5, "#2C8AA0"], [0.85, "#5BC3D8"], [1, "#D9F4F8"]];
const HISTORY = 120;

let objects = {};
let state = null;
let mode = "temp";
let selected = null;
let tab = "live";
let paneToken = 0;
const feeds = {};          // reverse relationships: target path -> [{rel, path}]
const history = {};

// Colour -------------------------------------------------------------------------

function ramp(stops, v) {
  if (v <= stops[0][0]) return new THREE.Color(stops[0][1]);
  for (let i = 1; i < stops.length; i++) {
    if (v <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i];
      return new THREE.Color(ca).lerp(new THREE.Color(cb), (v - a) / (b - a));
    }
  }
  return new THREE.Color(stops[stops.length - 1][1]);
}

function loopIndex(cduPath) { return Number(short(cduPath).split("_")[1]) - 1; }
function upsOf(path) {
  const o = objects[path];
  if (o.kind === "ups") return short(path);
  const up = o.rels.poweredBy?.[0];
  return up ? upsOf(up) : null;
}

// Returns a colour for the object's main body, or null to keep its USD displayColor.
function colorFor(path) {
  const o = objects[path];
  const v = state?.values[path] || {};
  if (state?.offline.includes(path)) return new THREE.Color(css("--shutdown"));
  if (o.kind === "rack" && (v.status === "no power" || v.status === "maintenance")) return new THREE.Color(css(STATUS_VAR[v.status]));
  if (mode === "look") return null; // the USD materials as authored
  if (mode === "temp") {
    if (o.kind === "rack" && v.returnTempC != null) return ramp(TEMP_RAMP, v.returnTempC);
    if (o.kind === "cdu" && v.supplyTempC != null) return ramp(TEMP_RAMP, v.supplyTempC + 10);
    return null;
  }
  if (mode === "power") {
    const pairs = { rack: ["powerKW", "maxPowerKW"], cdu: ["heatLoadKW", "capacityKW"], busway: ["loadKW", "capacityKW"], ups: ["loadKW", "capacityKW"],
      chiller: ["loadKW", "capacityKW"], leaf: ["trafficGbps", "capacityGbps"], spine: ["trafficGbps", "capacityGbps"] };
    const [num, den] = pairs[o.kind];
    return v[num] != null ? ramp(LOAD_RAMP, v[num] / v[den]) : null;
  }
  if (mode === "status") {
    if (o.kind === "rack") return new THREE.Color(css(STATUS_VAR[v.status] || "--muted"));
    if (isSwitch(path)) return new THREE.Color(css(NET_VAR[v.status] || "--muted"));
    return new THREE.Color(css("--ok"));
  }
  if (mode === "net") {
    if (o.kind === "rack") return new THREE.Color(css(NET_VAR[v.netStatus] || "--muted"));
    if (isSwitch(path)) return new THREE.Color(css(NET_VAR[v.status] || "--muted"));
    return null;
  }
  if (mode === "loop") {
    if (o.kind === "cdu") return new THREE.Color(LOOP_COLORS[loopIndex(path)]);
    if (o.kind === "rack") return new THREE.Color(o.rels.cooledBy ? LOOP_COLORS[loopIndex(o.rels.cooledBy[0])] : "#4A5457");
    return null;
  }
  if (mode === "feed") {
    if (["rack", "busway", "ups", "leaf", "spine"].includes(o.kind)) return new THREE.Color(FEED_COLORS[upsOf(path)] || "#4A5457");
    return null;
  }
  return null;
}

// The status light on every compute tray shows its rack's live status, in every colour mode.
// Switch status lights show the switch's network status.
const LED_OFF = new THREE.Color(0.015, 0.015, 0.015);
function ledColor(path) {
  const s = state?.values[path]?.status;
  if (isSwitch(path)) {
    if (!s || s === "ok") return null;
    return s === "no power" ? LED_OFF : new THREE.Color(css(NET_VAR[s]));
  }
  if (!s || s === "ok" || s === "design") return null;
  return s === "no power" ? LED_OFF : new THREE.Color(css(STATUS_VAR[s]));
}

// Tint the data-carrying parts (rack shells, equipment bodies) and status lights, one instance at a time.
function recolor() {
  for (const mesh of batches) {
    const { role, owners, base } = mesh.userData;
    if (role === "detail") continue;
    owners.forEach((owner, i) => {
      const c = owner ? (role === "led" ? ledColor(owner) : colorFor(owner)) : null;
      mesh.setColorAt(i, c || base);
    });
    mesh.instanceColor.needsUpdate = true;
  }
}

function renderLegend() {
  const el = $("legend");
  const gradient = (stops, lo, hi, fmtv) =>
    `<div class="ramp" style="background:linear-gradient(90deg,${stops.map(([, c]) => c).join(",")})"></div>
     <div class="ends"><span>${fmtv(lo)}</span><span>${fmtv(hi)}</span></div>`;
  const keys = (pairs) => `<div class="keys">${pairs.map(([c, l]) => `<span><i style="background:${c}"></i>${l}</span>`).join("")}</div>`;
  const html = {
    temp: `<span>Rack coolant return temperature</span>${gradient(TEMP_RAMP, 32, 56, (v) => v + " °C")}<span>Warn at 45 °C, throttle at 50 °C, shut down at 58 °C.</span>`,
    power: `<span>Load as a share of capacity</span>${gradient(LOAD_RAMP, 0, 1, (v) => v * 100 + "%")}`,
    status: keys(Object.entries(STATUS_VAR).filter(([k]) => k !== "design").map(([k, v]) => [css(v), k])),
    net: `<span>Racks: can they reach the rest of the job? Switches: is traffic getting through? From live/network.usda.</span>` +
      keys(["ok", "degraded", "isolated", "offline", "maintenance"].map((k) => [css(NET_VAR[k]), k])),
    loop: `<span>Which CDU cools each rack. Read from facility:cooledBy.</span>` + keys([...LOOP_COLORS.map((c, i) => [c, `CDU_${i + 1}`]), ["#4A5457", "Air-cooled"]]),
    feed: `<span>Which UPS powers each rack. Follows facility:poweredBy.</span>` + keys(Object.entries(FEED_COLORS).map(([k, c]) => [c, k])),
    look: `<span>The UsdPreviewSurface materials as authored. Status lights on every compute tray still show the rack's state.</span>` +
      keys([["#4CFF2A", "ok"], ...["warn", "throttled", "shutdown", "maintenance"].map((k) => [css(STATUS_VAR[k]), k]), ["#262626", "no power"]]),
  }[mode];
  el.innerHTML = html;
}

// 3D ----------------------------------------------------------------------------

let renderer, scene, camera, controls, highlight, hallBounds;
const batches = [];      // one InstancedMesh per (shape, look, role) combination
const ownerBoxes = {};   // selectable prim path -> world bounding box, for highlights, labels and fly-to

// One shared geometry per shape. three.js cylinders run along Y; USD cylinders pick an axis.
const GEOMETRY = {
  cube: new THREE.BoxGeometry(1, 1, 1),
  cylinderX: new THREE.CylinderGeometry(1, 1, 1, 28).rotateZ(-Math.PI / 2),
  cylinderY: new THREE.CylinderGeometry(1, 1, 1, 28),
  cylinderZ: new THREE.CylinderGeometry(1, 1, 1, 28).rotateX(Math.PI / 2),
};
for (const g of Object.values(GEOMETRY)) g.computeBoundingBox();

// UsdGeom.Cube has a size; UsdGeom.Cylinder has a radius, height and axis. Both become a scale.
function shapeOf(part) {
  if (part.shape === "cube") return ["cube", new THREE.Matrix4().makeScale(part.size, part.size, part.size)];
  const r = part.radius, h = part.height;
  return [`cylinder${part.axis}`, new THREE.Matrix4().makeScale(...{ X: [h, r, r], Y: [r, h, r], Z: [r, r, h] }[part.axis])];
}

// A UsdPreviewSurface becomes a three.js material. Emissive looks (status lights, screens) are drawn
// unlit and skip tone mapping, so they read as light sources.
function materialFor(look) {
  if (look.emissive) return new THREE.MeshBasicMaterial({ toneMapped: false });
  const clear = look.opacity < 1;
  return new THREE.MeshStandardMaterial({
    metalness: look.metallic, roughness: look.roughness, envMapIntensity: 0.35,
    transparent: clear, opacity: look.opacity, depthWrite: !clear, side: clear ? THREE.DoubleSide : THREE.FrontSide,
  });
}

// Group every part in the stage by shape + look + role, and draw each group as one InstancedMesh.
// Racks arrive as prototype parts plus one transform per rack, so this is USD instancing turned
// into GPU instancing: ~15,000 parts in a few dozen draw calls.
function buildScene(data) {
  const groups = new Map();
  const add = (part, world, owner) => {
    const [geometry, scale] = shapeOf(part);
    const key = `${geometry}|${part.look}|${part.role}|${part.overhead}`;
    if (!groups.has(key)) groups.set(key, { geometry, look: data.looks[part.look], role: part.role, overhead: part.overhead, items: [] });
    // USD matrices are row-major with row vectors; flattened, that's exactly three's column-major layout.
    // So USD's  local * parentWorld  becomes three's  parentWorld * local.
    const matrix = world.clone().multiply(new THREE.Matrix4().fromArray(part.matrix)).multiply(scale);
    groups.get(key).items.push({ matrix, owner });
  };
  const identity = new THREE.Matrix4();
  for (const part of data.parts) add(part, identity, part.owner);
  for (const inst of data.instances) {
    const world = new THREE.Matrix4().fromArray(inst.matrix);
    for (const part of data.prototypes[inst.proto]) add(part, world, inst.owner);
  }

  const box = new THREE.Box3();
  for (const g of groups.values()) {
    const geometry = GEOMETRY[g.geometry];
    const mesh = new THREE.InstancedMesh(geometry, materialFor(g.look), g.items.length);
    const base = new THREE.Color().setRGB(...(g.look.emissive || g.look.color)); // USD colours are linear, like three's working space
    g.items.forEach((item, i) => {
      mesh.setMatrixAt(i, item.matrix);
      mesh.setColorAt(i, base);
      if (item.owner) (ownerBoxes[item.owner] ||= new THREE.Box3()).union(box.copy(geometry.boundingBox).applyMatrix4(item.matrix));
    });
    mesh.computeBoundingSphere();
    const lit = !g.look.emissive;
    mesh.castShadow = lit && g.look.opacity >= 1;
    mesh.receiveShadow = lit;
    mesh.userData = { owners: g.items.map((item) => item.owner), base, role: g.role, overhead: g.overhead };
    scene.add(mesh);
    batches.push(mesh);
  }
}

function setup3D(data) {
  const view = $("view");
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  view.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  scene.background = new THREE.Color(css("--bg"));
  // Image-based lighting from a soft studio room, so metal, glass and coolant pipes have something to reflect.
  scene.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;

  buildScene(data);
  hallBounds = new THREE.Box3();
  Object.values(ownerBoxes).forEach((b) => hallBounds.union(b));
  const centre = hallBounds.getCenter(new THREE.Vector3());
  const span = hallBounds.getSize(new THREE.Vector3()).length();
  scene.fog = new THREE.Fog(scene.background, span * 1.4, span * 4);

  scene.add(new THREE.HemisphereLight(0xdfe9ec, 0x101416, 0.35));
  const sun = new THREE.DirectionalLight(0xfff6ea, 2.8);
  sun.position.copy(centre).add(new THREE.Vector3(-span * 0.35, -span * 0.5, span * 0.9));
  sun.target.position.copy(centre);
  sun.castShadow = true;
  sun.shadow.mapSize.set(4096, 4096);
  sun.shadow.bias = -0.0003;
  sun.shadow.normalBias = 0.015;
  Object.assign(sun.shadow.camera, { left: -span / 2, right: span / 2, top: span / 2, bottom: -span / 2, near: 1, far: span * 3 });
  scene.add(sun, sun.target);

  setupCamera();
  addLabels();

  new ResizeObserver(() => {
    const { clientWidth: w, clientHeight: h } = view;
    renderer.setSize(w, h);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }).observe(view);

  setupPicking();
  let last = performance.now();
  renderer.setAnimationLoop((now) => {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    if (space === "fabric") return; // the hall is hidden behind the fabric view
    stepFly(now);
    stepKeys(dt);
    controls.update();
    renderer.render(scene, camera);
  });
}

function ownerBox(path) {
  return ownerBoxes[path]?.clone() || new THREE.Box3();
}

function label(text, position, scale = 1) {
  const canvas = document.createElement("canvas");
  canvas.width = 512; canvas.height = 128;
  const ctx = canvas.getContext("2d");
  ctx.font = `600 64px ${css("--cond")}`;
  ctx.fillStyle = "#C9D4D7";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, 256, 64);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false }));
  sprite.scale.set(2.4 * scale, 0.6 * scale, 1);
  sprite.position.copy(position);
  scene.add(sprite);
}

function addLabels() {
  for (const row of "ABCDEFGH") {
    const box = ownerBox(`/World/Hall/Rows/Row_${row}/Rack_${row}01`);
    if (!box.isEmpty()) label(`Row ${row}`, new THREE.Vector3(box.min.x - 1.4, (box.min.y + box.max.y) / 2, 1.0), 1.4);
  }
  const groupLabel = (paths, text) => {
    const box = new THREE.Box3();
    paths.forEach((p) => box.union(ownerBox(p)));
    if (!box.isEmpty()) label(text, new THREE.Vector3((box.min.x + box.max.x) / 2, (box.min.y + box.max.y) / 2, box.max.z + 1.6), 1.8);
  };
  groupLabel(["/World/Plant/Chiller_1", "/World/Plant/Chiller_2"], "Chiller plant");
  groupLabel(["/World/GraySpace/UPS_1", "/World/GraySpace/UPS_2", "/World/GraySpace/Network/Spine_1"], "UPS + network");
  groupLabel(["/World/Hall/Cooling/CDU_1", "/World/Hall/Cooling/CDU_4"], "CDUs");
}

// Camera ---------------------------------------------------------------------------
// Drag to orbit, right-drag or shift-drag to pan, like most 3D tools. The wheel is handled here
// rather than by OrbitControls so that zoom heads for whatever is under the cursor, and so a
// trackpad behaves like Figma: two fingers pan, pinch zooms.

const MIN_DIST = 0.6, MAX_DIST = 140;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const nav = { keys: new Set(), fly: null, scroll: pref("scroll", "pan") };
const ray = new THREE.Raycaster();
const FLOOR = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);

function pref(key, fallback) {
  try { return localStorage.getItem(`twin.${key}`) ?? fallback; } catch { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem(`twin.${key}`, value); } catch { /* private window: fine */ }
}

function setupCamera() {
  const el = renderer.domElement;
  camera = new THREE.PerspectiveCamera(40, 1, 0.05, 800);
  camera.up.set(0, 0, 1); // USD stage is Z-up
  controls = new OrbitControls(camera, el);
  controls.enableDamping = true;
  controls.dampingFactor = 0.14;
  controls.enableZoom = false;           // ours: see onWheel
  controls.screenSpacePanning = false;   // panning slides across the floor instead of up the screen
  controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.PAN };
  controls.rotateSpeed = 0.7;
  controls.maxPolarAngle = Math.PI * 0.497; // stay above the floor
  setView("overview", false);

  el.addEventListener("wheel", onWheel, { passive: false });
  el.addEventListener("pointerdown", () => { nav.fly = null; });
  el.addEventListener("dblclick", (ev) => {
    const owner = pickOwner(ev);
    if (owner) { select(owner); focusOn(owner); return; }
    const p = pointUnder(ev);
    if (p) flyTo(p, Math.max(dist() * 0.5, 4));
  });
  window.addEventListener("keydown", onKey);
  window.addEventListener("keyup", (ev) => nav.keys.delete(ev.key.toLowerCase()));
  window.addEventListener("blur", () => nav.keys.clear());
}

const dist = () => camera.position.distanceTo(controls.target);

function ndcOf(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  return new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
}

// The 3D point under the cursor: the first visible surface, or the floor plane.
function pointUnder(ev) {
  ray.setFromCamera(ndcOf(ev), camera);
  const hit = ray.intersectObjects(batches.filter((b) => b.visible), false)[0];
  return hit ? hit.point : ray.ray.intersectPlane(FLOOR, new THREE.Vector3());
}

function onWheel(ev) {
  ev.preventDefault();
  nav.fly = null;
  const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? 400 : 1;
  const dx = ev.deltaX * unit, dy = ev.deltaY * unit;
  // A mouse wheel sends big whole-number steps on one axis; a trackpad sends small, smooth ones.
  const mouseWheel = ev.deltaMode !== 0 || (dx === 0 && Math.abs(dy) >= 50 && Number.isInteger(dy));
  if (ev.ctrlKey) zoomAt(ev, dy * 0.01);                                     // pinch arrives as ctrl + wheel
  else if (ev.metaKey || ev.altKey || mouseWheel || nav.scroll === "zoom") zoomAt(ev, dy * 0.0015);
  else panPixels(dx, dy);
}

// Zoom by moving the camera AND the orbit pivot toward the point under the cursor,
// so what you point at stays put on screen and becomes the new centre of orbit.
function zoomAt(ev, amount) {
  const anchor = pointUnder(ev) || controls.target.clone();
  const d = dist();
  const f = THREE.MathUtils.clamp(d * Math.exp(amount), MIN_DIST, MAX_DIST) / d;
  camera.position.sub(anchor).multiplyScalar(f).add(anchor);
  controls.target.sub(anchor).multiplyScalar(f).add(anchor);
}

function zoomBy(f) {
  flyTo(controls.target, THREE.MathUtils.clamp(dist() * f, MIN_DIST, MAX_DIST));
}

// Slide camera and pivot across the floor. right/forward are in metres.
function panMetres(right, forward) {
  const r = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0).setZ(0).normalize();
  const f = new THREE.Vector3(0, 0, 1).cross(r);
  const move = r.multiplyScalar(right).add(f.multiplyScalar(forward));
  camera.position.add(move);
  controls.target.add(move);
}

function panPixels(dx, dy) {
  const perPixel = (2 * dist() * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) / (renderer.domElement.clientHeight || 1);
  panMetres(dx * perPixel, -dy * perPixel);
}

function orbitBy(angle) {
  const offset = camera.position.clone().sub(controls.target).applyAxisAngle(new THREE.Vector3(0, 0, 1), angle);
  camera.position.copy(controls.target).add(offset);
}

const MOVE_KEYS = new Set(["w", "a", "s", "d", "q", "e", "arrowup", "arrowdown", "arrowleft", "arrowright"]);

function onKey(ev) {
  if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.target.closest?.("input, textarea, select") || $("usda-dialog").open) return;
  const k = ev.key.toLowerCase();
  if (k === "h" || k === "n") { setSpace(k === "n" ? "fabric" : "hall"); return; }
  if (space === "fabric") return;
  if (MOVE_KEYS.has(k)) { nav.keys.add(k); nav.fly = null; ev.preventDefault(); return; }
  const actions = {
    f: () => selected && focusOn(selected), "=": () => zoomBy(0.7), "+": () => zoomBy(0.7), "-": () => zoomBy(1 / 0.7),
    1: () => setView("overview"), 2: () => setView("top"), 3: () => setView("aisle"),
  };
  if (actions[k]) { actions[k](); ev.preventDefault(); }
}

// WASD / arrows move across the floor at a speed that scales with zoom; Q and E turn.
function stepKeys(dt) {
  const k = nav.keys;
  if (!k.size) return;
  const on = (...names) => (names.some((n) => k.has(n)) ? 1 : 0);
  const speed = Math.max(dist(), 3) * 0.9 * dt;
  const right = on("d", "arrowright") - on("a", "arrowleft");
  const forward = on("w", "arrowup") - on("s", "arrowdown");
  if (right || forward) panMetres(right * speed, forward * speed);
  const turn = on("e") - on("q");
  if (turn) orbitBy(turn * 1.2 * dt);
}

function flyTo(target, distance, position) {
  const toT = target.clone();
  const toP = position ? position.clone()
    : toT.clone().add(camera.position.clone().sub(controls.target).normalize().multiplyScalar(distance));
  nav.fly = { fromP: camera.position.clone(), fromT: controls.target.clone(), toP, toT, t0: performance.now(), dur: reducedMotion ? 1 : 700 };
}

function stepFly(now) {
  const f = nav.fly;
  if (!f) return;
  const k = Math.min(1, (now - f.t0) / f.dur);
  const e = 1 - Math.pow(1 - k, 3);
  camera.position.lerpVectors(f.fromP, f.toP, e);
  controls.target.lerpVectors(f.fromT, f.toT, e);
  if (k === 1) nav.fly = null;
}

function focusOn(path) {
  const box = ownerBox(path);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3()).length();
  flyTo(box.getCenter(new THREE.Vector3()), THREE.MathUtils.clamp(size * 1.8, 2.2, 60));
}

function setView(name, animate = true) {
  const c = hallBounds.getCenter(new THREE.Vector3());
  const fit = (hallBounds.getSize(new THREE.Vector3()).length() / 2 / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2))) * 0.8;
  let target = c, position;
  if (name === "overview") position = c.clone().add(new THREE.Vector3(0.38, -0.82, 0.75).normalize().multiplyScalar(fit));
  if (name === "top") position = c.clone().add(new THREE.Vector3(0, -0.02, 1).multiplyScalar(fit));
  if (name === "aisle") {
    // Eye level at the west end of the cold aisle between rows A and B, looking down it.
    const a = ownerBox("/World/Hall/Rows/Row_A/Rack_A01"), b = ownerBox("/World/Hall/Rows/Row_B/Rack_B01");
    const y = (a.getCenter(new THREE.Vector3()).y + b.getCenter(new THREE.Vector3()).y) / 2;
    position = new THREE.Vector3(a.min.x - 1.6, y, 1.65);
    target = new THREE.Vector3(a.min.x + 5, y, 1.25);
  }
  if (animate) flyTo(target, 0, position);
  else { camera.position.copy(position); controls.target.copy(target); controls.update(); }
}

// Picking --------------------------------------------------------------------------

// The selectable prim under the cursor. Unowned parts (pipes, glass, floor) are skipped,
// so you can click through containment glass onto the rack behind it.
function pickOwner(ev) {
  ray.setFromCamera(ndcOf(ev), camera);
  for (const hit of ray.intersectObjects(batches.filter((b) => b.visible), false)) {
    const owner = hit.object.userData.owners[hit.instanceId];
    if (owner) return owner;
  }
  return null;
}

function setupPicking() {
  const el = renderer.domElement;
  const tip = $("tip");
  let down = null;
  let hoverQueued = false;
  let lastMove = null;

  el.addEventListener("pointerdown", (ev) => { down = [ev.clientX, ev.clientY]; });
  el.addEventListener("pointerup", (ev) => {
    if (!down || Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) > 5 || ev.button !== 0) return;
    const owner = pickOwner(ev);
    if (owner) select(owner);
  });
  el.addEventListener("pointermove", (ev) => {
    lastMove = ev;
    if (hoverQueued || ev.buttons) return;
    hoverQueued = true;
    requestAnimationFrame(() => {
      hoverQueued = false;
      const owner = pickOwner(lastMove);
      el.style.cursor = owner ? "pointer" : "";
      if (!owner) { tip.hidden = true; return; }
      const r = el.getBoundingClientRect();
      tip.style.left = `${lastMove.clientX - r.left}px`;
      tip.style.top = `${lastMove.clientY - r.top}px`;
      tip.innerHTML = `<b>${esc(objects[owner].name)}</b> · ${esc(summary(owner))}`;
      tip.hidden = false;
    });
  });
  el.addEventListener("pointerleave", () => { tip.hidden = true; });
}

function setHighlight(path) {
  if (highlight) { scene.remove(highlight); highlight = null; }
  if (!path) return;
  const box = ownerBox(path).expandByScalar(0.06);
  highlight = new THREE.Box3Helper(box, new THREE.Color(css("--accent")));
  highlight.material.toneMapped = false;
  scene.add(highlight);
}

// Camera toolbar and view options over the 3D view.
function buildViewbar() {
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("zoom-in").addEventListener("click", () => zoomBy(0.7));
  $("zoom-out").addEventListener("click", () => zoomBy(1 / 0.7));
  $("focus").addEventListener("click", () => selected && focusOn(selected));

  const overhead = $("overhead");
  overhead.checked = pref("overhead", "1") === "1";
  const applyOverhead = () => {
    batches.forEach((b) => { if (b.userData.overhead) b.visible = overhead.checked; });
    savePref("overhead", overhead.checked ? "1" : "0");
  };
  overhead.addEventListener("change", applyOverhead);
  applyOverhead();

  const renderScroll = () => {
    document.querySelectorAll("[data-scroll]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.scroll === nav.scroll));
    $("stage-hint").innerHTML = nav.scroll === "pan"
      ? "<b>Drag</b> orbit · <b>Right-drag</b> or <b>two-finger scroll</b> pan · <b>Pinch</b> or <b>⌘ scroll</b> zoom to cursor · <b>WASD</b> move · <b>Q E</b> turn · <b>Double-click</b> fly to"
      : "<b>Drag</b> orbit · <b>Right-drag</b> pan · <b>Scroll</b> or <b>pinch</b> zoom to cursor · <b>WASD</b> move · <b>Q E</b> turn · <b>Double-click</b> fly to";
  };
  document.querySelectorAll("[data-scroll]").forEach((b) => b.addEventListener("click", () => {
    nav.scroll = b.dataset.scroll;
    savePref("scroll", nav.scroll);
    renderScroll();
  }));
  renderScroll();
}

// Data helpers ----------------------------------------------------------------------

function primary(path) {
  const o = objects[path], v = state?.values[path] || {};
  if (o.kind === "rack") return v.returnTempC;
  if (o.kind === "cdu") return v.supplyTempC;
  if (o.kind === "leaf") return v.servedPct;
  if (o.kind === "spine") return v.utilPct;
  return v.loadKW;
}

function summary(path) {
  const o = objects[path], v = state?.values[path] || {};
  if (state?.offline.includes(path)) return "offline";
  if (o.kind === "rack") {
    const net = v.netStatus && v.netStatus !== "ok" && v.netStatus !== "offline" ? ` · network ${v.netStatus}` : "";
    return `${fmt(v.returnTempC)} °C · ${fmt(v.powerKW, 0)} kW · ${v.status ?? "–"}${net}`;
  }
  if (o.kind === "leaf") return `${v.status ?? "design"} · ${v.linksUp ?? "–"}/${v.linksTotal} uplinks · ${fmt(v.servedPct, 0)}% of demand met`;
  if (o.kind === "spine") return `${v.status ?? "design"} · ${v.linksUp ?? "–"}/${v.linksTotal} links · ${fmt(v.utilPct, 0)}% load`;
  if (o.kind === "cdu") return `supply ${fmt(v.supplyTempC)} °C · ${fmt(v.loadPct, 0)}% load`;
  return v.loadKW != null ? `${fmt(v.loadKW / 1000, 2)} MW of ${fmt(v.capacityKW / 1000, 1)} MW` : "design values";
}

function statusOf(path) {
  const o = objects[path], v = state?.values[path] || {};
  if (state?.offline.includes(path)) return ["offline", "--shutdown"];
  if (o.kind === "rack") return [v.status || "–", STATUS_VAR[v.status] || "--muted"];
  if (isSwitch(path)) return [v.status || "design", NET_VAR[v.status] || "--muted"];
  if (v.online === false) return ["offline", "--shutdown"];
  const muted = state?.muted.includes("telemetry");
  return [muted ? "design" : "online", muted ? "--muted" : "--ok"];
}

function recordHistory() {
  for (const path of Object.keys(objects)) {
    const v = primary(path);
    if (v == null) continue;
    const h = (history[path] ||= []);
    h.push(v);
    if (h.length > HISTORY) h.shift();
  }
}

// Left panel ----------------------------------------------------------------------------

function buildControls() {
  $("fail-list").innerHTML = FAILABLE.map(([path, name, note]) =>
    `<button class="fail" type="button" data-path="${path}"><span class="what"><b class="mono">${name}</b><span>${note}</span></span><span class="state">Online</span></button>`).join("");
  $("fail-list").addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".fail");
    if (btn) apply(await api.control({ action: "toggle", path: btn.dataset.path }));
  });
  $("surge").addEventListener("change", async (ev) => apply(await api.control({ action: "surge", on: ev.target.checked })));
  $("reset").addEventListener("click", async () => apply(await api.control({ action: "reset" })));
  $("live-layer").addEventListener("change", async (ev) => apply(await api.control({ action: "mute", layer: "telemetry", on: !ev.target.checked })));
  $("net-layer").addEventListener("change", async (ev) => apply(await api.control({ action: "mute", layer: "network", on: !ev.target.checked })));
  document.querySelectorAll("[data-layer]").forEach((b) => b.addEventListener("click", async () => {
    const { usda } = await api.get(`/api/live.usda?layer=${b.dataset.layer}`);
    $("dialog-title").textContent = `live/${b.dataset.layer}.usda (in memory, right now)`;
    $("dialog-body").textContent = usda;
    $("usda-dialog").showModal();
  }));
  $("change").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-change]");
    if (btn) apply(await api.control({ action: btn.dataset.change }));
  });
  $("dialog-close").addEventListener("click", () => $("usda-dialog").close());

  $("modes").innerHTML = MODES.map(([id, name]) => `<button type="button" data-mode="${id}" aria-pressed="${id === mode}">${name}</button>`).join("");
  $("modes").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    mode = btn.dataset.mode;
    document.querySelectorAll("#modes button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === mode));
    renderLegend();
    recolor();
  });
  renderLegend();
}

function renderControls() {
  document.querySelectorAll(".fail").forEach((btn) => {
    const off = state.offline.includes(btn.dataset.path);
    btn.classList.toggle("off", off);
    btn.querySelector(".state").textContent = off ? "Offline" : "Online";
  });
  $("surge").checked = state.surge;
  $("live-layer").checked = !state.muted.includes("telemetry");
  $("net-layer").checked = !state.muted.includes("network");
  $("muted-badge").hidden = !state.muted.length;
  $("muted-badge").textContent = `Muted: ${state.muted.map((m) => `live/${m}.usda`).join(", ")} · showing the layers underneath`;
  renderChange();
}

// The proposed network change. Re-rendered only when it changes, so the buttons keep focus.
let changeKey = "";
function renderChange() {
  const { proposal: p, pushed, dryRun: d } = state.fabric;
  const key = `${pushed}|${d?.t}`;
  if (key === changeKey) return;
  changeKey = key;
  const verdict = !d ? "" : d.safe
    ? `<div class="verdict"><b>Simulated result: safe to push</b><span class="delta">Job ${d.before}% → ${d.after}%</span></div>`
    : `<div class="verdict bad"><b>Simulated result: don't push</b><span class="delta">Job ${fmt(d.before, 0)}% → ${fmt(d.after, 0)}%</span>
        <span>Row ${esc(short(p.leaf).slice(-1))} would get ${d.rowShare}% of the bandwidth it needs.
          ${d.allUp ? "Every uplink stays up, so link monitoring would still show green." : `Only ${esc(d.linksUp)} uplinks are up right now, and the change makes it much worse.`}</span>
        <span class="why">Why: ${esc(p.cause)}</span></div>`;
  $("change").innerHTML = `<div class="ticket"><span class="id">${esc(p.ticket)} · waiting to deploy</span><b>${esc(p.title)}</b><span class="hint">${esc(p.detail)}</span></div>
    ${verdict}
    ${pushed ? `<span class="pushed">Live on ${esc(short(p.leaf))} in production.</span>` : ""}
    <div class="btn-row">
      <button class="btn primary" type="button" data-change="dryrun">Dry run in simulation</button>
      ${pushed ? `<button class="btn" type="button" data-change="rollback">Roll back</button>`
        : `<button class="btn danger" type="button" data-change="push">Push to production</button>`}
    </div>`;
}

function renderKPIs() {
  const w = state.values["/World"] || {};
  const set = (id, html, cls = "") => { const el = $(id); el.querySelector("strong").innerHTML = html; el.className = `kpi ${cls}`; };
  set("k-it", `${fmt(w.itLoadKW / 1000, 2)}<small>MW</small>`);
  set("k-pue", fmt(w.pue, 2));
  const cdus = Object.keys(objects).filter((p) => objects[p].kind === "cdu").map((p) => state.values[p]?.supplyTempC ?? 0);
  const hottest = Math.max(...cdus);
  set("k-cdu", `${fmt(hottest)}<small>°C</small>`, hottest >= 45 ? "bad" : hottest >= 36 ? "warn" : "");
  const racks = Object.keys(objects).filter((p) => objects[p].kind === "rack").map((p) => state.values[p]?.status);
  const bad = racks.filter((s) => s === "throttled" || s === "shutdown").length;
  const warn = racks.filter((s) => s === "warn").length;
  set("k-risk", `${bad + warn}<small>of ${racks.length}</small>`, bad ? "bad" : warn ? "warn" : "");
  const job = w.jobSpeedPct;
  set("k-job", `${fmt(job, 0)}<small>% speed · ${w.jobRacks ?? "–"} racks</small>`, job < 70 ? "bad" : job < 97 ? "warn" : "");
  set("k-clock", clock(state.t));
}

function renderEvents() {
  $("events").innerHTML = state.events.slice(0, 14).map((e) =>
    `<li><time>${clock(e.t)}</time><span class="${e.level}">${esc(e.text)}</span></li>`).join("");
}

// Inspector ------------------------------------------------------------------------------

function select(path) {
  selected = path;
  setHighlight(path);
  $("focus").disabled = !path;
  renderInspector();
  renderFabric();
}

function introHTML() {
  return `<div class="intro">
    <div><div class="label">Teardown</div><h2>How this twin works</h2></div>
    <ol class="pipeline">
      <li><span class="n">1</span><div><b>assets/nvl72_rack.usda, assets/cdu.usda</b><span>Hand-written equipment. One file per asset, with a cooling variant on the rack.</span></div></li>
      <li><span class="n">2</span><div><b>build_factory.py → layout.usda</b><span>80 references to the rack asset, wired together with poweredBy and cooledBy relationships.</span></div></li>
      <li><span class="n">3</span><div><b>sim.py + server.py → live/telemetry.usda</b><span>Every second the physics steps forward and the results are written as overs in the live layer.</span></div></li>
      <li><span class="n">4</span><div><b>fabric.py → live/network.usda</b><span>The network twin, the job NVIDIA DSX Air does. It follows the uplink relationships and writes link state and training-job speed to its own layer.</span></div></li>
      <li><span class="n">5</span><div><b>web/ (this page)</b><span>Reads only the composed stage. It never talks to either simulation directly.</span></div></li>
    </ol>
    <div><div class="label" style="margin-bottom:8px">Try this</div>
    <ol class="try">
      <li><a data-try="compose">Open Rack_A01's composition</a> to see three layers stacked into one rack.</li>
      <li><a data-try="cdu">Fail CDU_2</a> and watch rows C and D heat up, throttle, then recover when you bring it back.</li>
      <li><a data-try="n1">Turn on the surge, then fail Chiller_1.</a> One chiller copes at normal load but not at peak. That's why sites buy N+1.</li>
      <li><a data-try="mute">Mute the live layer.</a> Every value falls back to layout.usda. Proof the UI only reads USD.</li>
      <li><a data-try="air">Inspect Rack_H01</a>, which uses the air variant of the same asset and tops out at 40 kW.</li>
      <li><a data-try="fabric">Open the Fabric view and fail Spine_1.</a> Nothing loses power, but every leaf loses half its uplinks and the whole job slows.</li>
      <li><a data-try="config">Dry run NET-2041</a> before pushing it. Then push it anyway, and look at Leaf_F in the hall: physically, nothing is wrong.</li>
    </ol></div>
    <p class="explain">Full walkthrough with exercises: <b class="mono">sample_twin/TEARDOWN.md</b></p>
  </div>`;
}

async function runTry(kind) {
  if (kind === "compose") { tab = "composition"; select("/World/Hall/Rows/Row_A/Rack_A01"); focusOn(selected); }
  if (kind === "cdu") { apply(await api.control({ action: "toggle", path: "/World/Hall/Cooling/CDU_2" })); mode = "temp"; select("/World/Hall/Cooling/CDU_2"); }
  if (kind === "n1") { await api.control({ action: "surge", on: true }); apply(await api.control({ action: "toggle", path: "/World/Plant/Chiller_1" })); }
  if (kind === "mute") apply(await api.control({ action: "mute", layer: "telemetry", on: true }));
  if (kind === "air") { tab = "composition"; select("/World/Hall/Rows/Row_H/Rack_H01"); focusOn(selected); }
  if (kind === "fabric") { setSpace("fabric"); apply(await api.control({ action: "toggle", path: "/World/GraySpace/Network/Spine_1" })); select("/World/GraySpace/Network/Spine_1"); }
  if (kind === "config") { apply(await api.control({ action: "dryrun" })); tab = "live"; select("/World/Hall/Network/Leaf_F"); }
  document.querySelectorAll("#modes button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === mode));
  renderLegend();
  recolor();
}

function renderInspector() {
  const el = $("inspector");
  if (!selected) {
    el.innerHTML = introHTML();
    el.querySelectorAll("[data-try]").forEach((a) => a.addEventListener("click", () => runTry(a.dataset.try)));
    return;
  }
  const o = objects[selected];
  el.innerHTML = `<div class="insp-head">
      <span class="label">${esc(KIND_LABEL[o.kind] || o.kind)}</span>
      <h2>${esc(o.name)}</h2>
      <div class="path">${esc(selected)}</div>
      <div class="insp-row"><span class="pill" id="pill"></span>
        <span style="display:flex;gap:6px"><button class="btn" id="zoom-to" type="button" title="Fly the camera to it (F)">Zoom to</button><button class="btn" id="toggle-off" type="button"></button><button class="btn" id="close" type="button">Close</button></span></div>
    </div>
    <div class="tabs" role="tablist">
      ${[["live", "Live data"], ["composition", "Composition"], ["topology", "Topology"]].map(([id, name]) =>
        `<button role="tab" type="button" data-tab="${id}" aria-selected="${id === tab}">${name}</button>`).join("")}
    </div>
    <div class="pane" id="pane"></div>`;
  $("close").addEventListener("click", () => select(null));
  $("zoom-to").addEventListener("click", () => focusOn(selected));
  $("toggle-off").addEventListener("click", async () => apply(await api.control({ action: "toggle", path: selected })));
  el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    tab = b.dataset.tab;
    el.querySelectorAll("[data-tab]").forEach((x) => x.setAttribute("aria-selected", x.dataset.tab === tab));
    renderPane();
  }));
  updateHead();
  renderPane();
}

function updateHead() {
  if (!selected || !$("pill")) return;
  const [text, colorVar] = statusOf(selected);
  const pill = $("pill");
  pill.textContent = text;
  pill.style.background = css(colorVar);
  $("toggle-off").textContent = state?.offline.includes(selected) ? "Bring online" : "Take offline";
}

function sourceChip(source) {
  const cls = source.startsWith("live/network") ? "net" : source.startsWith("live") ? "live" : source.startsWith("layout") ? "layout" : "asset";
  return `<span class="src ${cls}">${esc(source)}</span>`;
}

function metricsFor(path, v) {
  const kind = objects[path].kind;
  const m = (label, value, unit) => `<div><span class="label">${label}</span><strong>${value}<small>${unit}</small></strong></div>`;
  if (kind === "rack") return m("Return", fmt(v.returnTempC), "°C") + m("Power", fmt(v.powerKW, 0), "kW") + m("GPU util", v.utilization != null ? fmt(v.utilization * 100, 0) : "–", "%");
  if (kind === "cdu") return m("Supply", fmt(v.supplyTempC), "°C") + m("Heat load", fmt(v.heatLoadKW / 1000, 2), "MW") + m("Load", fmt(v.loadPct, 0), "%");
  if (kind === "chiller") return m("Heat load", fmt(v.loadKW / 1000, 2), "MW") + m("Capacity", fmt(v.capacityKW / 1000, 1), "MW") + m("COP", fmt(v.cop, 1), "");
  if (kind === "leaf") return m("Uplinks", `${v.linksUp ?? "–"}/${v.linksTotal ?? "–"}`, "") + m("Port load", fmt(v.utilPct, 0), "%") + m("Demand met", fmt(v.servedPct, 0), "%");
  if (kind === "spine") return m("Links", `${v.linksUp ?? "–"}/${v.linksTotal ?? "–"}`, "") + m("Traffic", fmt(v.trafficGbps / 1000, 1), "Tb/s") + m("Port load", fmt(v.utilPct, 0), "%");
  return m("Load", fmt(v.loadKW / 1000, 2), "MW") + m("Capacity", fmt(v.capacityKW / 1000, 1), "MW") + m("Load", v.loadKW != null ? fmt((100 * v.loadKW) / v.capacityKW, 0) : "–", "%");
}

async function renderPane() {
  const pane = $("pane");
  if (!pane) return;
  const token = ++paneToken;
  const path = selected;
  if (tab === "topology") { pane.innerHTML = topologyHTML(path); bindLinks(pane); return; }

  const prim = await api.get(`/api/prim?path=${encodeURIComponent(path)}`);
  if (token !== paneToken || path !== selected) return;

  if (tab === "live") {
    const v = state?.values[path] || {};
    const sparkLabel = { rack: "Coolant return, last 2 minutes", cdu: "Supply temperature, last 2 minutes",
      leaf: "Bandwidth demand met (%), last 2 minutes", spine: "Port load (%), last 2 minutes" }[objects[path].kind] || "Load, last 2 minutes";
    pane.innerHTML = `<div class="metrics">${metricsFor(path, v)}</div>
      <div><div class="label" style="margin-bottom:6px">${sparkLabel}</div><canvas class="spark" id="spark"></canvas></div>
      <div><div class="label" style="margin-bottom:6px">Every facility attribute, and the layer it comes from</div>
      <table>${prim.attrs.map((a) => `<tr><td class="k">${esc(a.name.replace("facility:", ""))}</td><td class="v">${esc(typeof a.value === "number" ? +a.value.toFixed(2) : a.value)}</td><td class="s">${sourceChip(a.source)}</td></tr>`).join("")}</table></div>
      <p class="explain">The strongest layer wins: <b>live/network.usda</b>, then <b>live/telemetry.usda</b>, then <b>layout.usda</b>, then the <b>asset</b>. Each simulator writes only to its own layer. Mute one and watch the chips change.</p>`;
    drawSpark(path);
  } else {
    const variants = Object.entries(prim.variants);
    pane.innerHTML = `
      <p class="explain">USD builds this prim by stacking opinions from several places. Below are the <b>composition arcs</b>, strongest first, then the exact text each layer contributes.</p>
      <div class="chips">${prim.instance ? `<a>instanceable</a>` : ""}${prim.kind ? `<a>kind = ${esc(prim.kind)}</a>` : ""}${variants.map(([k, s]) => `<a>variant ${esc(k)} = ${esc(s)}</a>`).join("")}</div>
      <ul class="arcs">${prim.arcs.map((a, i) => `<li><span class="n">${i + 1}</span><span class="t">${esc(a.type)}</span><span class="l">${esc(a.layer)} <span style="color:var(--muted)">${esc(a.path)}</span></span></li>`).join("")}</ul>
      ${prim.stack.map((s) => `<div class="layer-card"><div class="cap">${sourceChip(s.layer)}<span class="mono" style="font-size:11.5px;color:var(--muted)">${esc(s.path)}</span></div><pre>${esc(s.usda)}</pre></div>`).join("")}
      <button class="btn" id="refresh" type="button">Refresh</button>`;
    $("refresh").addEventListener("click", renderPane);
  }
}

function drawSpark(path) {
  const canvas = $("spark");
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const data = history[path] || [];
  if (data.length < 2) return;
  const isRack = objects[path].kind === "rack";
  let lo = Math.min(...data), hi = Math.max(...data);
  if (isRack) { lo = Math.min(lo, 30); hi = Math.max(hi, 52); }
  const pad = (hi - lo) * 0.1 || 1;
  lo -= pad; hi += pad;
  const x = (i) => 6 + (i / (HISTORY - 1)) * (w - 12);
  const y = (v) => h - 6 - ((v - lo) / (hi - lo)) * (h - 12);
  if (isRack) {
    for (const [t, c] of [[45, css("--warn")], [50, css("--throttled")]]) {
      ctx.strokeStyle = c; ctx.globalAlpha = 0.5; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(6, y(t)); ctx.lineTo(w - 6, y(t)); ctx.stroke();
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  const offset = HISTORY - data.length;
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i + offset), y(v)) : ctx.moveTo(x(i + offset), y(v))));
  ctx.strokeStyle = css("--accent"); ctx.lineWidth = 1.5; ctx.stroke();
  ctx.lineTo(x(data.length - 1 + offset), h); ctx.lineTo(x(offset), h); ctx.closePath();
  ctx.globalAlpha = 0.12; ctx.fillStyle = css("--accent"); ctx.fill(); ctx.globalAlpha = 1;
  const last = data[data.length - 1];
  ctx.fillStyle = css("--ink");
  ctx.beginPath(); ctx.arc(x(data.length - 1 + offset), y(last), 3, 0, Math.PI * 2); ctx.fill();
  ctx.font = `11px ${css("--mono")}`; ctx.textAlign = "right";
  ctx.fillText(fmt(last), w - 10, 14);
}

function topologyHTML(path) {
  const link = (p) => `<a data-go="${p}"><span>${esc(short(p))}</span><span style="color:var(--muted)">${esc(summary(p))}</span></a>`;
  const upstream = (p, rel) => {
    const out = [p];
    let cur = p;
    while (objects[cur]?.rels[rel]?.length) {
      const targets = objects[cur].rels[rel];
      out.push(targets);
      cur = targets[0];
    }
    return out;
  };
  const chainHTML = (rel, title) => {
    const parts = upstream(path, rel);
    if (parts.length < 2) return "";
    return `<div><div class="label" style="margin-bottom:6px">${title}</div><div class="chain">${parts.map((part, i) =>
      i === 0 ? link(part) : `<div class="arrow">↑ facility:${rel}</div>${part.map(link).join("")}`).join("")}</div></div>`;
  };
  const down = feeds[path] || [];
  const group = (rel) => down.filter((d) => d.rel === rel).map((d) => d.path);
  const chips = (paths, title) => (paths.length ? `<div><div class="label" style="margin-bottom:6px">${title} (${paths.length})</div><div class="chips">${paths.map((p) => `<a data-go="${p}">${esc(short(p))}</a>`).join("")}</div></div>` : "");
  return `<p class="explain">These links are <b>USD relationships</b> authored in layout.usda. The simulations read them to work out what loses power, cooling or network when something fails.</p>
    ${chainHTML("poweredBy", "Power path")}
    ${chainHTML("cooledBy", "Cooling path")}
    ${chainHTML("uplink", "Network path")}
    ${chips(group("poweredBy"), "Powers")}
    ${chips(group("cooledBy"), "Cools")}
    ${chips(group("uplink"), "Connects")}`;
}

function bindLinks(root) {
  root.querySelectorAll("[data-go]").forEach((a) => a.addEventListener("click", () => select(a.dataset.go)));
}

// Fabric view ------------------------------------------------------------------------------
// The logical twin: the same USD prims as the hall, drawn the way the network sees them.
// This is the picture DSX Air works with. Both views select the same prim paths, so picking a
// switch here and pressing H shows you where it physically stands.

let space = "hall";
const fabricBox = {};   // prim path -> {x, y, w, h} in the SVG's coordinates

function ofKind(kind) { return Object.keys(objects).filter((p) => objects[p].kind === kind).sort(); }

function buildFabric() {
  const W = 1000;
  const spines = ofKind("spine"), leaves = ofKind("leaf");
  spines.forEach((p, i) => { fabricBox[p] = { x: W / 2 + (i - (spines.length - 1) / 2) * 320 - 85, y: 20, w: 170, h: 50 }; });
  const step = W / leaves.length;
  leaves.forEach((p, i) => {
    const cx = step * (i + 0.5);
    fabricBox[p] = { x: cx - 59, y: 210, w: 118, h: 50 };
    (feeds[p] || []).filter((d) => d.rel === "uplink").map((d) => d.path).sort()
      .forEach((rp, j) => { fabricBox[rp] = { x: cx - 48 + (j % 5) * 19.5, y: 312 + Math.floor(j / 5) * 19.5, w: 16, h: 16 }; });
  });

  const el = $("fabric");
  // pointerdown, not click: the SVG is redrawn every second, and a click that straddles a redraw is lost.
  el.addEventListener("pointerdown", (ev) => { const n = ev.target.closest("[data-path]"); if (n && ev.button === 0) select(n.dataset.path); });
  el.addEventListener("dblclick", (ev) => {
    const n = ev.target.closest("[data-path]");
    if (n) { select(n.dataset.path); setSpace("hall"); focusOn(n.dataset.path); }
  });
  el.addEventListener("mousemove", (ev) => {
    const tip = $("tip"), n = ev.target.closest("[data-path]");
    if (!n) { tip.hidden = true; return; }
    const r = el.getBoundingClientRect();
    tip.style.left = `${ev.clientX - r.left}px`;
    tip.style.top = `${ev.clientY - r.top}px`;
    tip.innerHTML = `<b>${esc(objects[n.dataset.path].name)}</b> · ${esc(summary(n.dataset.path))}`;
    tip.hidden = false;
  });
  el.addEventListener("mouseleave", () => { $("tip").hidden = true; });
  document.querySelectorAll("[data-space]").forEach((b) => b.addEventListener("click", () => setSpace(b.dataset.space)));
  setSpace("hall");
}

function setSpace(name) {
  space = name;
  document.querySelectorAll("[data-space]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.space === space));
  document.querySelector(".stage").classList.toggle("fabric-on", space === "fabric");
  $("fabric").hidden = space !== "fabric";
  $("tip").hidden = true;
  renderFabric();
}

function renderFabric() {
  if (space !== "fabric" || !state) return;
  const v = (p) => state.values[p] || {};
  const B = fabricBox;
  const down = (s) => s === "no power" || s === "maintenance";
  const netColor = (s) => css(NET_VAR[s] || "--muted");
  const ring = (p, color, width = 1.5) => (p === selected ? `stroke="${css("--accent")}" stroke-width="3"` : `stroke="${color}" stroke-width="${width}"`);
  let links = "", nodes = "";

  // Leaf to spine: one line per bundle of 40 links. Width shows load, colour shows health.
  for (const lp of ofKind("leaf")) {
    const lv = v(lp), a = B[lp];
    for (const sp of objects[lp].rels.uplink || []) {
      const b = B[sp], up = lv.status && !down(lv.status) && !down(v(sp).status);
      const color = !lv.status ? css("--muted") : !up ? css("--shutdown") : lv.servedPct < 98 ? css("--warn") : css("--accent");
      const width = up ? 1.2 + 4 * Math.min(1, (lv.utilPct || 0) / 100) : 1.5;
      links += `<line x1="${a.x + a.w / 2}" y1="${a.y}" x2="${b.x + b.w / 2}" y2="${b.y + b.h}" stroke="${color}" stroke-width="${width}"
        ${up ? "" : 'stroke-dasharray="6 5"'} opacity="${up ? 0.85 : 0.6}"/>`;
    }
    links += `<line x1="${a.x + a.w / 2}" y1="${a.y + a.h}" x2="${a.x + a.w / 2}" y2="${a.y + a.h + 46}" stroke="${netColor(lv.status)}" stroke-width="1.5" opacity=".7"/>`;
  }

  const switchNode = (p, sub) => {
    const b = B[p], s = v(p).status;
    return `<g data-path="${p}"><rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="${css("--panel-2")}" ${ring(p, netColor(s), 2)}/>
      <circle cx="${b.x + 12}" cy="${b.y + 17}" r="4" fill="${netColor(s)}"/>
      <text x="${b.x + 22}" y="${b.y + 21}">${esc(short(p))}</text>
      <text class="sub" x="${b.x + 10}" y="${b.y + 39}">${esc(sub)}</text></g>`;
  };
  for (const p of ofKind("spine")) {
    const sv = v(p);
    nodes += switchNode(p, sv.linksUp == null ? "design" : `${sv.linksUp}/${sv.linksTotal} links · ${fmt(sv.utilPct, 0)}%`);
  }
  for (const p of ofKind("leaf")) {
    const lv = v(p);
    nodes += switchNode(p, lv.linksUp == null ? "design" : `${lv.linksUp}/${lv.linksTotal} · ${fmt(lv.servedPct, 0)}%`);
  }

  // Racks: fill is network status. A faded rack is healthy but not back in the job yet.
  // An orange outline means its GPUs are throttling, which slows the job just like a bad link.
  for (const p of ofKind("rack")) {
    const b = B[p], rv = v(p);
    if (!b) continue;
    const waiting = rv.inJob === false && (rv.netStatus === "ok" || rv.netStatus === "degraded");
    const outline = rv.status === "throttled" ? ring(p, css("--throttled"), 2) : ring(p, "none", 0);
    nodes += `<rect data-path="${p}" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="${netColor(rv.netStatus)}"
      fill-opacity="${waiting ? 0.3 : 0.9}" ${outline}/>`;
  }
  for (const lp of ofKind("leaf")) {
    const a = B[lp];
    nodes += `<text class="row" x="${a.x + a.w / 2}" y="${a.y + a.h + 112}" text-anchor="middle">ROW ${esc(short(lp).slice(-1))}</text>`;
  }

  const w = v("/World");
  const job = w.jobSpeedPct ?? 100;
  const jobColor = job < 70 ? css("--shutdown") : job < 97 ? css("--warn") : css("--ok");
  const bottleneck = w.jobBottleneck && w.jobBottleneck !== "none" ? ` · slowest: ${w.jobBottleneck}` : "";
  nodes += `<text class="row" x="0" y="440">TRAINING JOB</text>
    <rect x="0" y="450" width="1000" height="10" fill="${css("--panel-2")}"/>
    <rect x="0" y="450" width="${10 * Math.max(0, Math.min(100, job))}" height="10" fill="${jobColor}"/>
    <text x="0" y="482">${fmt(job, 0)}% speed · ${w.jobRacks ?? "–"} of ${ofKind("rack").length} racks${esc(bottleneck)}</text>`;

  const key = (c, l) => `<span><i style="background:${c}"></i>${l}</span>`;
  $("fabric").innerHTML = `<div class="fabric-head">
      <div><div class="label">Logical twin</div><h3>Network fabric</h3>
        <p>The same USD prims as the hall, drawn the way the network sees them. This is the view NVIDIA DSX Air simulates. Click to inspect, double-click to see it in the hall.</p></div>
      <div class="legend"><div class="keys">${["ok", "degraded", "isolated", "offline", "maintenance"].map((k) => key(css(NET_VAR[k]), k)).join("")}</div>
        <div class="keys"><span><i style="background:none;outline:2px solid ${css("--throttled")};outline-offset:-2px"></i>GPUs throttling</span><span><i style="background:${css("--ok")};opacity:.3"></i>waiting to rejoin job</span></div>
        <span>Spine: links up · port load. Leaf: uplinks up · share of demand met.</span></div>
    </div>
    <svg viewBox="-10 0 1020 495" preserveAspectRatio="xMidYMin meet" role="img" aria-label="Leaf-spine network fabric">${links}${nodes}</svg>`;
}

// Loop ------------------------------------------------------------------------------------

function apply(next) {
  if (!next || next.error) return;
  state = next;
  recordHistory();
  recolor();
  renderControls();
  renderKPIs();
  renderEvents();
  renderFabric();
  if (selected) {
    updateHead();
    if (tab === "live") renderPane();
    else if (tab === "topology") { const pane = $("pane"); if (pane) { pane.innerHTML = topologyHTML(selected); bindLinks(pane); } }
  }
}

async function poll() {
  try { apply(await api.get("/api/state")); }
  catch { $("events").innerHTML = `<li><time>--:--</time><span class="alert">Lost the server. Is server.py still running?</span></li>`; }
  setTimeout(poll, 1000);
}

async function init() {
  const data = await api.get("/api/scene");
  objects = data.objects;
  for (const [path, o] of Object.entries(objects)) {
    for (const [rel, targets] of Object.entries(o.rels)) for (const t of targets) (feeds[t] ||= []).push({ rel, path });
  }
  await document.fonts.ready;
  setup3D(data);
  buildViewbar();
  buildFabric();
  buildControls();
  renderInspector();
  poll();
}

init();
