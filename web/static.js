// The twin with no server, for static hosts. static_build.py baked the USD stage into data/*.json.
// This loads Pyodide (Python compiled to WebAssembly), runs static_twin.py, which runs the unchanged
// sim.py and fabric.py once a second, and answers the same /api calls server.py does.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const text = (url) => fetch(url).then((r) => r.text());

const events = document.getElementById("events");
if (events) events.innerHTML = `<li><time>--:--</time><span>Starting Python in your browser to run sim.py and fabric.py…</span></li>`;

const twin = (async () => {
  const [{ loadPyodide }, data, ...files] = await Promise.all([
    import(PYODIDE + "pyodide.mjs"), text("data/twin.json"),
    ...["sim.py", "fabric.py", "static_twin.py"].map(async (name) => [name, await text(name)]),
  ]);
  const py = await loadPyodide({ indexURL: PYODIDE });
  for (const [name, source] of files) py.FS.writeFile(`/home/pyodide/${name}`, source);
  py.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
  const twin = py.pyimport("static_twin").Twin(data);
  setInterval(() => twin.tick(), 1000);
  return twin;
})();

export const api = {
  async get(url) {
    const u = new URL(url, location.href);
    if (u.pathname === "/api/scene") return fetch("data/scene.json").then((r) => r.json());
    const t = await twin;
    if (u.pathname === "/api/state") return JSON.parse(t.state());
    if (u.pathname === "/api/prim") return JSON.parse(t.prim(u.searchParams.get("path") || "")) || { error: "No prim at that path" };
    if (u.pathname === "/api/live.usda") return JSON.parse(t.live_usda(u.searchParams.get("layer") || "telemetry"));
    return { error: "Not found" };
  },
  control: async (body) => JSON.parse((await twin).control(JSON.stringify(body))),
};
