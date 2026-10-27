---
title: Hall A Twin
emoji: 🏭
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
short_description: OpenUSD digital twin of an AI factory hall
---

# Hall A twin

A small but real digital twin of an AI factory hall, built the way NVIDIA Omniverse twins are built:
OpenUSD assets and layers, live simulation layers, and a web viewer.

- **Run it:** see [RUNNING.md](RUNNING.md). With Claude Code, clone the repo, run `claude`, and say "run this".
- **Learn from it:** [TEARDOWN.md](TEARDOWN.md) is a guided walkthrough of the architecture.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/amanhsn/hall-a-twin)

The hosted version runs one shared simulation, so anyone who fails a UPS fails it for every visitor.
