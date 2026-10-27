# Hall A twin

A small digital twin of an AI factory hall, built with OpenUSD (`usd-core`) and shown in a
three.js web viewer. See RUNNING.md for setup and TEARDOWN.md for the guided walkthrough.

## Run it

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python server.py      # serves http://localhost:8765 and opens a browser

- Needs Python 3.10+. No GPU, Omniverse or Kit is needed.
- The server rebuilds `factory.usda` and `layout.usda` from `build_factory.py` and `assets/` on
  every start, so restart it after edits. Ctrl+C stops it.
- The browser loads three.js and fonts from CDNs, so the viewer needs internet access.
- `live/*.usda` are live layers the server rewrites while it runs.

## Static build (what https://hall-a-twin.vercel.app serves)

    .venv/bin/python static_build.py && npx vercel deploy dist --prod

- `static_build.py` bakes the stage to `dist/data/*.json`; `web/static.js` runs `static_twin.py`
  (server.py's `Twin` without USD) plus the unchanged `sim.py` and `fabric.py` under Pyodide.
- `static_twin.py` must keep returning exactly what server.py returns. If you change `Twin.state`,
  `Twin.prim`, `Twin.control` or the live-layer format in server.py, change it there too.
