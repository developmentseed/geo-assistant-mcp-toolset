# CLAUDE.md

Architecture, deployment and usage are documented in README.md — read it
first. This file holds only what an agent cannot derive from it.

## This repo owns no runtime code

`mcp_runtime`, `mcp_cli`, `mcp_agent`, `mcp_agent_api` and `mcp_toolset` come
from the `mcp-toolsets-runtime` PyPI package, pinned in the root
`pyproject.toml`. Never
add a module under those names here, and never patch runtime behaviour locally —
fix it in
[mcp-toolsets-runtime](https://github.com/developmentseed/mcp-toolsets-runtime),
release, then bump the pin. What this repo owns is `toolsets/`, `charts/`, the
`Dockerfile`, the workflows, `tests/test_contract.py` and `agent_app.py` (a
thin composition layer over `mcp_agent_api.app.create_app`'s documented
`build=` seam — not a runtime patch, since it only calls the package's own
public functions with this project's system prompt).

## Commands

- `uv sync` once, then `./scripts/lint`, `./scripts/test`, `./scripts/format`.
- New toolset: `uv run mcp-toolset new <name>` (from the runtime) — never
  hand-roll the layout. Add `--with-ui` for a toolset with a React view (see
  the runtime's CONSUMING.md §3).
- Remove a toolset: `./scripts/remove-toolset <name>`.
- Build toolset UIs: `./scripts/build-views` (needs node). Built view bundles
  live at `<package>/views/*.html`, are git-ignored, and must exist before
  `mcp-serve` or `build_server` aborts — the Dockerfile's node stage, the CI
  `ui` job, and this script rebuild them.
- Local chat: `uv run uvicorn agent_app:app --port 8765` (from the repo root,
  so `.env` is found), with `mcp-serve-local` running and
  `MCP_URL=http://localhost:8000/` (index root, not `/mcp`); open
  http://localhost:8765. `agent_app:app` (not `mcp_agent_api.app:app`) is
  this repo's own thin wrapper: it adds a data-grounding rule to the system
  prompt and mounts the runtime's bundled web client with this agent's
  title and example questions (`UI`). There is no vendored frontend — a
  change to the chat page's structure belongs upstream in the runtime's
  `js/agent-ui`.
- Only the toolsets and the index deploy. The hosted Chainlit chat was
  removed with runtime 0.10; there is no hosted agent API yet.

## Safety

- Never read `.env` — it contains real API keys.
- Never run `kubectl` or `helm` against a locally configured context: the
  deployment cluster is reached only via CI (or a kubeconfig the user
  manages outside this repo). Give the user commands to run themselves.

## Conventions CI enforces but nothing else documents

- Dependency ranges are bounded `<next-major,>=current` — check PyPI for
  the current version when adding one.
- Test filenames must be unique across the whole workspace: mypy and
  pytest run once over `tests/` and `toolsets/` together.
- Tools that do I/O are `async def`; sync tools are for pure computation
  only (the runtime executes them in a thread pool).
- `tests/` holds only what is about *this repo's* toolsets — `test_contract.py`
  sweeps `toolsets/` against the runtime's gates. Tests for runtime behaviour
  belong upstream, not here.

## Deployment semantics that bite

- Changes under `.github/` trigger no builds or deploys — after fixing a
  workflow, run it via workflow_dispatch.
- Shared paths (`charts/`, `Dockerfile`, `uv.lock`, root `pyproject.toml`)
  rebuild and redeploy ALL toolsets; only `toolsets/<name>/` changes are scoped
  to one service. A runtime version bump lands in `uv.lock`, so it redeploys
  everything — which is what you want.
- Merging a toolset directory deletion uninstalls the live service — the
  deploy workflow reconciles releases against `toolsets/`.
