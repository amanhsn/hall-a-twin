# Running the Hall A twin

A small, real digital twin of an AI factory hall: 80 GB200 NVL72-class racks, cooling, power and a
leaf-spine network, all described in OpenUSD and simulated live in your browser.

It runs on any Mac, Windows or Linux machine. You don't need a GPU or Omniverse.

**Just want to look?** Open https://hall-a-twin.vercel.app. It's the same twin running entirely in your
browser, with nothing to install. The steps below are for running it on your own machine, which you'll
want if you're going to edit it or follow TEARDOWN.md.

## Option A: let Claude run it (easiest)

1. Install [Claude Code](https://claude.com/claude-code) if you don't have it.
2. In a terminal:

   ```
   git clone https://github.com/amanhsn/hall-a-twin.git
   cd hall-a-twin
   claude
   ```

3. Type **"run this"**. Claude reads `CLAUDE.md`, sets up Python and starts the server.
4. Open **http://localhost:8765** if it doesn't open on its own.

You can then ask Claude things like "walk me through TEARDOWN.md" or "what happens if I fail UPS_2?".

## Option B: run it yourself

You need **Python 3.10 or newer** (`python3 --version` to check) and **git**.

```
git clone https://github.com/amanhsn/hall-a-twin.git
cd hall-a-twin
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead.

The app opens at **http://localhost:8765**. Press **Ctrl+C** in the terminal to stop it.

No git? Use **Code → Download ZIP** on the GitHub page, unzip it, and run the same commands from
inside the unzipped folder.

## What to do once it's open

Work through the "Try this" list in the right-hand panel, then read `TEARDOWN.md`. It explains how
the twin is built and how each part maps to NVIDIA Omniverse.

## Publishing your own copy (free, no server)

The hosted link is a static build: no Python server, so any free static host works.

```
.venv/bin/python static_build.py        # writes dist/
npx vercel deploy dist --prod           # or drag dist/ onto Netlify, GitHub Pages, etc.
```

`static_build.py` opens the USD stage with the live layers muted and bakes the scene, the wiring and
every prim's teardown into `dist/data/`. In the browser, `web/static.js` loads Pyodide (Python compiled
to WebAssembly) and runs `static_twin.py`, which runs the same `sim.py` and `fabric.py` once a second.
Each visitor gets their own simulation. Rebuild after any change to the assets or the sims.

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named pxr` | Run the `pip install -r requirements.txt` step inside the `.venv`. |
| `Address already in use` | Something else is on port 8765. Stop it, or stop an older copy of the server. |
| Page loads but the 3D view is blank | The viewer loads three.js from the internet. Check your connection. |
| `pip` can't find `usd-core` | Your Python is too old or too new for the available wheels. Try Python 3.12. |
