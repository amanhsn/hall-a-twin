"""Build a copy of the twin that needs no server, for free static hosts like Vercel.

    .venv/bin/python static_build.py      writes dist/
    vercel deploy dist --prod             or any static host

USD is still the source. This opens the same stage server.py does, mutes the live layers so
every value it reads is a design value, and bakes what the browser needs into dist/data/.
In the browser, web/static.js loads Pyodide (Python compiled to WebAssembly) and runs
static_twin.py, which runs the unchanged sim.py and fabric.py.
"""

import json
import shutil

from server import HERE, Twin

DIST = HERE / "dist"


def main():
    twin = Twin()
    for layer in twin.layers.values():
        twin.stage.MuteLayer(layer.identifier)
    paths = list(twin.objects) + ["/World"]
    data = {"topology": twin.sim.topo,
            "design": twin.state()["values"],
            "prims": {path: twin.prim(path) for path in paths},
            "docs": {name: layer.documentation for name, layer in twin.layers.items()}}

    shutil.rmtree(DIST, ignore_errors=True)
    shutil.copytree(HERE / "web", DIST)
    for name in ("sim.py", "fabric.py", "static_twin.py"):
        shutil.copy(HERE / name, DIST / name)
    (DIST / "data").mkdir()
    (DIST / "data" / "scene.json").write_text(json.dumps(twin.scene, separators=(",", ":")))
    (DIST / "data" / "twin.json").write_text(json.dumps(data, separators=(",", ":")))

    index = DIST / "index.html"
    app = '<script type="module" src="app.js"></script>'
    index.write_text(index.read_text().replace(app, f"<script>window.TWIN_STATIC = true</script>\n{app}"))
    print(f"Static twin written to {DIST.relative_to(HERE.parent)}/. Preview it with:\n"
          f"  python3 -m http.server -d {DIST} 8000")


if __name__ == "__main__":
    main()
