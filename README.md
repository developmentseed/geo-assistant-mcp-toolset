# geo-assistant-mcp-toolset

The [geo-assistant](https://github.com/developmentseed/geo-assistant) workflow
rebuilt as MCP toolsets: Overture Maps place lookup, geodesic buffering and
spatial SQL over a locked-down DuckDB connection, plus NAIP aerial imagery
interpreted by a locally served vision model — with geometries and images
flowing between tools through session state (never through the model).

```
toolsets/<name>/tools.py  ──▶  ghcr.io/<owner>/<repo>/mcp-<name>  ──▶  k8s Service mcp-<name>
   (LangChain @tool fns)        (Dockerfile --build-arg TOOLSET=...)     (charts/mcp-toolset)
```

## The toolsets

| Toolset | Tool | Does |
| --- | --- | --- |
| `duckdb-analyst` | `list_sources` | Lists the pre-registered views (Natural Earth countries/places, Overture Maps places) `query`/`chart` can read, with column descriptions. |
| | `query` | Runs a read-only SQL `SELECT` against those views, or any public `https://`/`s3://` parquet/CSV, and returns rows as JSON. |
| | `chart` | Runs a query like `query` does, then fills the rows into a Vega-Lite spec you supply. |
| | `get_place` | Finds one place (POI) in Overture Maps by fuzzy name match within a bounding box. |
| | `get_search_area` | Buffers that place by a radius in km into an area of interest. |
| | `places_within_area` | Lists Overture places of one category inside an area of interest. |
| `naip-imagery` | `fetch_naip_image` | Fetches NAIP aerial imagery (USA only, ~1m resolution) over an area of interest from Microsoft Planetary Computer. |
| | `interpret_image` | Describes that image with an Ollama-served vision model, on request. |

The two toolsets chain together: `get_place` → `get_search_area` →
`places_within_area` / `fetch_naip_image` → `interpret_image`, with
geometries and the rendered image passed between tools through session state
rather than through the model — see
[How the tools compose](#how-the-tools-compose-session-state).

`interpret_image` talks to Ollama (`OLLAMA_BASE_URL`/`OLLAMA_IMAGE_MODEL`,
default `localhost:11434`/`gemma4:cloud`). The default is an Ollama
**cloud** model, so the localhost demo needs no GPU: `ollama signin`, then
`ollama pull gemma4:cloud`. Any locally pulled vision model works via
`OLLAMA_IMAGE_MODEL`.

## Run it

```sh
uv sync                # runtime from PyPI + both toolsets, into one .venv
```

Then bring up the full chat stack — MCP toolsets, then the agent API, then
the web client — each step in its own shell, left running:

1. **Configure and build once.** Copy `.example.env` to `.env` and set
   `PROVIDER_MODEL`, `PROVIDER_API_KEY` and `MCP_URL=http://localhost:8000/`
   (the index root — an existing `.env` that says `.../mcp` finds no
   toolsets against `mcp-serve-local`). Then `./scripts/build-views` — the
   map, table and chart views render from these bundles, and
   `mcp-serve-local` refuses to start without them.

2. **MCP: serve both toolsets.**

   ```sh
   uv run mcp-serve-local        # toolsets at /<name>/mcp, index at /, on :8000
   ```

3. **Agent API: the thing that calls the model and drives the tools.** From
   the repo root, so `.env` is found:

   ```sh
   uv run uvicorn mcp_agent_api.app:app --port 8765
   ```

4. **Web client: the chat UI.**

   ```sh
   cd web && npm ci && npm run dev   # :5173, proxies /api to the agent API
   ```

Open http://localhost:5173 and chat. A few prompts that exercise the tooling:

- *"Find the Golden Gate Bridge and show me cafes within 1km."* — chains
  `get_place` → `get_search_area` → `places_within_area`, rendered on the
  map view.
- *"Get NAIP imagery over that area from 2020 to 2023 and describe what's
  there."* — chains `fetch_naip_image` → `interpret_image`; needs Ollama
  running (`ollama signin && ollama pull gemma4:cloud`, or point
  `OLLAMA_IMAGE_MODEL` at a model you already have).
- *"What data sources can you query?"*, then *"Chart populated places in
  France by population."* — `list_sources` → `chart`, rendered as a table or
  chart view.

The agent API's routes and checkpointing caveats are in
[Chat over HTTP](#chat-over-http-the-agent-api-and-the-web-client) below.
Prefer calling the raw tools with no model in the loop? Use `mcp-cli` against
the server from step 2:

```sh
uv run mcp-cli list --url http://localhost:8000/duckdb-analyst/mcp
uv run mcp-cli call get_place \
  --url http://localhost:8000/duckdb-analyst/mcp \
  place_name="Golden Gate Bridge" search_bbox='[-122.6,37.6,-122.3,37.9]'
uv run mcp-cli repl --url http://localhost:8000/naip-imagery/mcp
```

`mcp-serve-local` mounts each toolset at `/<name>/mcp` and serves the index
document at `/` — the same URL shape the shared domain has in production, so
an `mcp-cli`, an `mcp-agent` or an MCP Inspector session can be pointed at it
unchanged. Toolsets are also importable directly (e.g.
`from duckdb_analyst.tools import TOOLS`) for in-process use in tests,
notebooks or an agent repo.

## Chat over HTTP: the agent API and the web client

`mcp_agent_api` is a FastAPI app (from the runtime) that streams each turn as
[AG-UI](https://docs.ag-ui.com/) SSE events. `web/` is the matching browser
client — a React app copied from the runtime's `examples/agui-events/web/`
(the runtime does not ship it as a package) and adapted for this repo.
Re-diff `web/` against that example on runtime bumps. The three-step run
order is [above](#run-it); a missing `PROVIDER_MODEL` stops the API at
startup by design, and the Vite dev server proxies `/api` to it so the
browser talks to one origin and no CORS configuration is needed.

The API has five routes:

| Route | Purpose |
| --- | --- |
| `POST /runs` | Run one turn; the response streams AG-UI events. |
| `GET /threads/{id}` | The transcript and state metadata of a thread. |
| `GET /threads/{id}/turns` | The turns of a thread, with per-turn state. |
| `GET /threads/{id}/state/{key}` | One state value; `?turn=N` reads it as of turn N. |
| `GET /views/{toolset}/{view}` | The HTML bundle of a `ui://` view. |

See the runtime's [CONSUMING.md][consuming] §"Serving the agent over HTTP" for
the full contract.

Two caveats. Threads are checkpointed in the API process's memory by default,
so a restart loses them (set `MCP_AGENT_CHECKPOINT` to a PostgreSQL URL to
keep them). And a thread id is the only credential on the read routes — treat
thread ids as secrets, and put auth in front of the API before exposing it.

[consuming]: https://github.com/developmentseed/mcp-toolsets-runtime/blob/main/docs/CONSUMING.md

There is also a legacy Chainlit chat (`uv run mcp-agent-web`, or
`uv run mcp-agent` for a terminal REPL) — see
[Legacy Chainlit chat](#legacy-chainlit-chat) at the bottom. Use the agent API
and `web/` above instead; Chainlit stays only until the hosted deployment
moves off it.

## How the tools compose (session state)

Large values — geometries, images — pass between tools through **session
state** instead of through the model's context: a producing tool tags a
result key with a `Kind`, and the runtime fills the consuming tool's
parameter from state by that kind. The parameter leaves the model's schema
entirely, so it can't be hallucinated because it is never offered. This is
the ported geo-assistant flow, spanning both toolsets:

| Tool (toolset) | Consumes (parameter) | Publishes (result key) |
| --- | --- | --- |
| `get_place` (duckdb-analyst) | — | `place` · `geojson.PlaceFeature` |
| `get_search_area` (duckdb-analyst) | `place` · `geojson.PlaceFeature` | `search_area` · `geojson.AreaOfInterest` |
| `places_within_area` (duckdb-analyst) | `area` · `geojson.AreaOfInterest` | `places` · untagged |
| `fetch_naip_image` (naip-imagery) | `area` · `geojson.AreaOfInterest` | `naip_image` · `image.JpegBase64` |
| `interpret_image` (naip-imagery) | `image` · `image.JpegBase64` | — |

`geojson.AreaOfInterest` comes from the runtime's `mcp_runtime.kinds`
vocabulary. `geojson.PlaceFeature` and `image.JpegBase64` are minted locally
(in `geo_tools.py` and `naip-imagery` respectively), because the vocabulary
has no kinds for them yet — kinds are plain strings, so producer and
consumer agreeing on the text is the whole contract. The image hop is where
this pays most: a 512px JPEG is ~100KB of base64 that never enters the chat
model's context on its way to the vision model.

The chat's tool step also shows a **receipt** where a filled parameter would
have been, naming the key, the kind and the publishing tool:

```
[state used: area ← duckdb-analyst/search_area, published by get_search_area]
```

Full mechanism (untagged keys, the `@state:<key>` handle form, the
`state.produces` health field, what a tracing backend still sees) is in the
runtime's [SESSION-STATE.md][session-state] — the table above is what's
actually wired up here.

[session-state]: https://github.com/developmentseed/mcp-toolsets-runtime/blob/main/docs/SESSION-STATE.md

## Toolset UI views

`duckdb-analyst`'s three geo tools (`get_place`, `get_search_area`,
`places_within_area`) share a **map** view; `query` gets a **table** view and
`chart` a **chart** view. A view is a small frontend component that an MCP
Apps host — Claude, ChatGPT, or the bundled chat clients here — renders in a
sandboxed iframe and feeds the tool's `structuredContent`; it's progressive
enhancement, so a plain client still gets the tool's `message` and data.

Build the bundles (needs node):

```sh
./scripts/build-views
```

Built bundles live at `<package>/views/*.html`, are git-ignored, and must
exist before `mcp-serve`/`mcp-serve-local` will start — the Dockerfile's node
stage and CI's `ui` job rebuild them too. For the full authoring contract
(adding a view to a new tool), see
[Adding a toolset UI view](#adding-a-toolset-ui-view) at the bottom.

## Deployment

- **ci.yml** (PRs + main): lint, tests, `helm lint`, and a no-push Docker
  build of every image affected by the change. Always runs — no cluster
  needed.
- **deploy.yml** (main): builds and pushes each changed toolset's image and
  `helm upgrade --install`s it. Changes to shared build inputs (`charts/`,
  `Dockerfile`, `uv.lock`, root `pyproject.toml`) rebuild *both* toolsets —
  how a runtime version bump reaches every service. Skipped entirely unless
  `KUBE_CONFIG` and `MCP_NAMESPACE` are set — see
  [Deploying this repo](#deploying-this-repo) at the bottom for what those
  are and how to set up a cluster from scratch.

## Development

```sh
./scripts/format        # ruff autofix + format
./scripts/lint          # ruff checks + mypy over tests/ and toolsets/
./scripts/test          # pytest (args forwarded, e.g. ./scripts/test -k naip)
```

`web/` (the chat client) and the toolset `ui/` directories need Node — Vite 7
wants ≥ 22.12 (or 20.19). `web/` is outside the Python workspace and outside
`scripts/lint`; `npm run build` in it typechecks and bundles.

`tests/` holds only the toolset contract sweep — every directory under
`toolsets/` must import, export a non-empty `TOOLS`, and satisfy the same
`ToolResult` and docstring gates `build_server` applies at startup.

---

## Where this comes from

This repo is an instance of the
[mcp-toolsets](https://github.com/developmentseed/mcp-toolsets) template — a
monorepo of **toolsets** (small packages of
[LangChain](https://python.langchain.com) tools) each auto-deployed as its
own [MCP](https://modelcontextprotocol.io) service on Kubernetes — with the
geo-assistant tools as its content. Everything that isn't a toolset comes
from one PyPI package,
[**mcp-toolsets-runtime**](https://github.com/developmentseed/mcp-toolsets-runtime)
[![PyPI](https://img.shields.io/pypi/v/mcp-toolsets-runtime?label=mcp-toolsets-runtime)](https://pypi.org/project/mcp-toolsets-runtime/),
bounded in the root `pyproject.toml` and pinned exactly by `uv.lock`. Never
add a module under `mcp_runtime`, `mcp_cli`, `mcp_agent`, `mcp_agent_api` or
`mcp_toolset` here, and never patch runtime behaviour locally — fix it
upstream, release, then bump the pin
(`uv lock --upgrade-package mcp-toolsets-runtime`; because `uv.lock` is a
shared build input, merging the bump rebuilds and redeploys everything). This
repo owns `toolsets/*`, `charts/*`, the `Dockerfile`, the workflows and
`tests/test_contract.py`.

| Module | What it gives you |
| --- | --- |
| `mcp_runtime` | Serves a toolset's `TOOLS` as a stateless streamable-HTTP MCP server (`mcp-serve` / `mcp-serve-local`) and its `VIEWS` as `ui://` resources. Also runs the directory service (`mcp-index`). |
| `mcp_cli` | Typer/rich client (`mcp-cli`) to list and call tools on a running service. |
| `mcp_toolset` | Scaffolding: `mcp-toolset new [--with-ui] <name>` writes a conforming toolset into `toolsets/`. |
| `mcp_agent` / `mcp_agent_api` | The example chat, as a Chainlit UI/REPL (legacy) or as an HTTP+AG-UI API (current — see [Chat over HTTP](#chat-over-http-the-agent-api-and-the-web-client)). |
| `mcp_state` | Keeps large tool values out of the model's context — see [How the tools compose](#how-the-tools-compose-session-state). |

The template's own docs (further below) still mention its shipped example
toolsets (`hello`, `credential-demo`, `stac-explorer`) — those were not
carried into this instance; read `duckdb-analyst`/`naip-imagery` in their
place.

### Adding a toolset

```sh
uv run mcp-toolset new my-toolset            # scaffolds toolsets/my-toolset, registers it in the uv workspace
```

Write `TOOLS` (a list of LangChain `@tool` functions) in
`toolsets/my-toolset/src/my_toolset/tools.py`, each returning
`mcp_runtime.tool_result.ToolResult | ToolError` — see
[Typed tool returns](#typed-tool-returns) below for the contract a contract
test enforces. Add tests, then merge to `main`; CI builds and deploys
`mcp-my-toolset` automatically. Async tools do I/O; sync tools are pure
computation (the runtime runs them in a thread pool).

```sh
./scripts/remove-toolset my-toolset          # inverse: merging the deletion tears down the live service
```

### Typed tool returns

Every tool returns one dict per call, in one of two shapes from
`mcp_runtime.tool_result`:

- **`ToolResult`** — success: a required str `message` plus any data keys
  your tool declares.
- **`ToolError`** — a structured error: a short machine-readable `error`
  kind and a `detail`.

The runtime derives each tool's MCP `outputSchema` from its return
annotation and validates every result against it before sending. A tool
whose annotation doesn't follow the contract **fails at startup**
(`build_server` aborts, naming the tool) and fails the contract test in CI.
Recommended: one `ToolResult` subclass per tool with each data key as
`NotRequired[...]`, a one-line docstring for the schema description, and
construct returns with TypedDict call syntax
(`ToolResult(message=...)`) — mypy-checked, a plain dict at runtime. Keys not
declared in the annotation are silently dropped from `structuredContent`, and
bare `str`/`list`/`dict[str, Any]` return types are rejected at startup.

### Adding a toolset UI view

Scaffold a toolset with an example view, then build it (needs node):

```sh
uv run mcp-toolset new --with-ui my-toolset
cd toolsets/my-toolset/ui && npm install && npm run build
```

A toolset opts in with three things, validated at startup: a `VIEWS`
export (`{tool_name: view_id}`), a built bundle at
`<package>/views/<view_id>.html` (self-contained; the shipped `ui/` builds
these with Vite + `vite-plugin-singlefile`), and the host bridge
[`@developmentseed/mcp-view`][mcp-view] in the view's own dependencies:

```ts
import { onData, sendMessage } from "@developmentseed/mcp-view";

onData<MyResult>((data) => render(data));  // the tool's structuredContent
button.onclick = () => sendMessage("…");   // a user turn back into the chat
```

The runtime serves each view as resource `ui://<toolset>/<view_id>` and
stamps the owning tool's `_meta` with that URI — standard
[MCP Apps][ext-apps], so any compliant host renders the same bundle
unchanged. A view can only act on what the tool put in its `ToolResult`
(pre-signed/short-lived URLs, never tokens), and an interaction's
`sendMessage(...)` arrives back as a user message, driving the model to call
the next tool — `toolsets/stac-explorer` in the upstream template is the
worked example of that pattern.

[mcp-view]: https://www.npmjs.com/package/@developmentseed/mcp-view
[ext-apps]: https://github.com/modelcontextprotocol/ext-apps

### Legacy Chainlit chat

```sh
uv run mcp-agent install-elements    # writes public/elements/McpView.jsx, needed for views to render
uv run mcp-agent-web                 # serves the Chainlit chat at :8080 (CHAINLIT_PORT)
uv run mcp-agent                     # or a terminal REPL
```

Bring-your-own-model like the API above: set `PROVIDER_MODEL` and
`PROVIDER_API_KEY` (env or `.env`), or enter them in Chainlit's ⚙ settings.
`install-elements` re-runs are needed after a runtime upgrade; nothing is
written at runtime, so it works on a read-only filesystem, and
`mcp-agent-web` starts without it but warns and won't render views. This
surface is being retired in favour of the agent API and `web/` client, both
locally and (eventually) in the hosted deployment.

### Per-user credentials

A pattern for tools that act on a user's behalf, demonstrated by the
template's (removed here) `credential-demo` toolset. Credentials never
become tool arguments (the model would see them); instead the client sends
them as HTTP headers and the tool reads them at call time:

```python
from mcp_runtime.credentials import credential_from_header

@tool
def whoami() -> WhoamiResult:
    """Report which account the calling user's credential belongs to."""
    token = credential_from_header("x-demo-token")
    ...

TOOLS = [whoami]
CREDENTIAL_HEADERS = ["x-demo-token"]  # advertised in /health and the index
```

`mcp-agent` supports this per-call, not just per-process — an httpx client
factory injects the calling user's headers at request time, scoped to
`user_credentials(...)`:

```python
from mcp_agent.main import user_credentials

with user_credentials({"x-demo-token": the_users_token}):
    result = await agent.ainvoke(...)
```

Neither toolset in this repo declares `CREDENTIAL_HEADERS` today; reach for
this if you add a tool that needs one.

### Deploying this repo

The repo is a GitHub template — **Use this template** to create your own
instance. `./scripts/bootstrap` is the intended first step after creating a
repo from it: it sets the `MCP_NAMESPACE` Actions variable (deploys are
skipped until it's set), substitutes `__MCP_NAMESPACE__` placeholders below
with your namespace, and optionally removes the shipped example toolsets.

```sh
./scripts/bootstrap            # prompts for a namespace + which examples to keep
./scripts/bootstrap my-namespace --keep-examples   # or non-interactively
```

Deploying (this repo or a new instance) needs a Kubernetes cluster
(v1.24+) with outbound access to `ghcr.io`:

1. **Namespace and a scoped deploy service account** — the kubeconfig
   behind the `KUBE_CONFIG` GitHub secret. Don't use cluster-admin:

   ```sh
   kubectl create namespace __MCP_NAMESPACE__
   kubectl -n __MCP_NAMESPACE__ create serviceaccount deployer
   kubectl -n __MCP_NAMESPACE__ create role deployer --verb='*' \
     --resource=deployments.apps,services,secrets,serviceaccounts,ingresses.networking.k8s.io,roles.rbac.authorization.k8s.io,rolebindings.rbac.authorization.k8s.io
   kubectl -n __MCP_NAMESPACE__ create rolebinding deployer \
     --role=deployer --serviceaccount=__MCP_NAMESPACE__:deployer
   ```

   `KUBE_CONFIG` is a complete kubeconfig file with a deployer token inside,
   reachable from GitHub's runners. The token expires (~90 days here):

   ```sh
   TOKEN=$(kubectl -n __MCP_NAMESPACE__ create token deployer --duration=2160h)
   SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
   CA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

   KC=--kubeconfig=deployer.kubeconfig
   kubectl config $KC set-cluster cluster --server="$SERVER"
   kubectl config $KC set clusters.cluster.certificate-authority-data "$CA"
   kubectl config $KC set-credentials deployer --token="$TOKEN"
   kubectl config $KC set-context deployer --cluster=cluster --user=deployer --namespace=__MCP_NAMESPACE__
   kubectl config $KC use-context deployer

   gh secret set KUBE_CONFIG < deployer.kubeconfig && rm deployer.kubeconfig
   ```

2. **GHCR pull secret** — if the repo is private, its images are too:

   ```sh
   kubectl -n __MCP_NAMESPACE__ create secret docker-registry ghcr-pull \
     --docker-server=ghcr.io \
     --docker-username=<github-username> \
     --docker-password=<token-with-read:packages>
   ```

3. **ingress-nginx** and **cert-manager** (the charts' ingress defaults
   assume both):

   ```sh
   helm upgrade --install ingress-nginx ingress-nginx \
     --repo https://kubernetes.github.io/ingress-nginx \
     --namespace ingress-nginx --create-namespace

   helm upgrade --install cert-manager cert-manager \
     --repo https://charts.jetstack.io \
     --namespace cert-manager --create-namespace \
     --set crds.enabled=true
   ```

4. **DNS + a ClusterIssuer** — point an A/CNAME record for your hostname at
   the ingress controller's load balancer
   (`kubectl -n ingress-nginx get svc ingress-nginx-controller`), set your
   email in `k8s/letsencrypt-clusterissuer.yaml`, then
   `kubectl apply -f k8s/letsencrypt-clusterissuer.yaml`. The `mcp-index`
   Ingress is annotated `cert-manager.io/cluster-issuer: letsencrypt`, so
   cert-manager issues and renews the `__MCP_NAMESPACE__-tls` Secret every
   Ingress shares.

Finally, set the optional shared-domain secret:

```sh
gh secret set MCP_INGRESS_HOST --body <the-hostname>
```

With `MCP_INGRESS_HOST` set, every toolset gets an Ingress on that host at
`/<name>` and an `mcp-index` service serves a directory of both toolsets at
the domain root:

```
https://<host>/                   # index: JSON directory of every toolset + its tools
https://<host>/docs               # the same directory, browsable (Swagger UI)
https://<host>/<toolset>/mcp      # MCP endpoint (prefix stripped by ingress)
https://<host>/<toolset>/health   # liveness, lists the toolset's tool names
```

```sh
curl https://<host>/ | jq
uv run mcp-cli list --url https://<host>/duckdb-analyst/mcp
```

Without `MCP_INGRESS_HOST`, services stay ClusterIP-only:

```sh
kubectl -n __MCP_NAMESPACE__ port-forward svc/mcp-duckdb-analyst 8000:8000
uv run mcp-cli list
```

An **`MCP_CHAT_HOST`** secret additionally deploys the hosted Chainlit chat
(`Dockerfile.chat`, `charts/mcp-chat`) at `chat.<MCP_INGRESS_HOST>` by
default — **bring-your-own-model**: the deployment holds no provider key,
each user enters their own `provider:model` and API key in ⚙ settings, kept
only in that browser session. Conversations checkpoint in the pod's memory by
default (lost on restart/redeploy/scale-up); point `MCP_AGENT_CHECKPOINT` at
a PostgreSQL URL and add the runtime's `[checkpointing-postgres]` extra to
`Dockerfile.chat` to keep them. This deployment still runs Chainlit; the
local-only agent API + `web/` client will eventually replace it here too.

Build an image locally with `docker build --build-arg TOOLSET=duckdb-analyst .`.
