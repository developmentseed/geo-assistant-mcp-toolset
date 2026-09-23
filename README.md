# geo-assistant

A chat assistant for geographic questions. Ask it about a place, the things
around that place, what an area looks like from the air, or a data question
that needs SQL. It finds the answer with tools, shows the result as a map, an
image, a table or a chart, and tells you when it cannot confirm something.

This repo is the [geo-assistant](https://github.com/developmentseed/geo-assistant)
workflow, rebuilt as [MCP](https://modelcontextprotocol.io) tools on the
[mcp-toolsets](#built-on-mcp-toolsets) runtime.

## What it can do

- **Find a place and what is near it.** *"Find the Golden Gate Bridge and show
  me cafes within 1 km."* The assistant looks up the place in
  [Overture Maps](https://overturemaps.org), draws a search area around it,
  and lists the points of interest inside. The result shows on a map.
- **Look at aerial imagery.** *"Get NAIP imagery around the Golden Gate Bridge
  and describe what you see."* The assistant gets
  [NAIP](https://planetarycomputer.microsoft.com/dataset/naip) aerial photos
  (USA only, about 1 m per pixel), shows them, and a vision model describes
  them.
- **Answer data questions with SQL.** *"Chart the 10 most populated places in
  France."* The assistant writes read-only SQL for
  [DuckDB](https://duckdb.org) and shows the rows as a table or a chart. It
  can read:
  - Natural Earth countries and populated places,
  - Overture Maps places,
  - Zarr arrays: global sea surface temperature (NASA MUR) and USA air
    temperature (NOAA HRRR),
  - any public `https://` or `s3://` Parquet, CSV or Zarr URL that you give it.
- **Stay grounded.** The assistant answers data questions only from tool
  results. If no tool confirms a fact, it tells you so. It does not guess from
  its training data.

**Limits.** NAIP covers the USA only. A place lookup reads Overture Maps
directly from the cloud, so the first answer can take a minute or more. The
assistant cannot write data or read files on your computer.

## Questions to try

The tools are small and general, so the assistant can combine them in many
ways. Some examples:

**Places and areas** (Overture Maps, shown on a map)

- *"Find Times Square and list the hotels within 500 m."*
- *"Find the Eiffel Tower. Which museums are within 2 km?"*
- *"Find Time Out Market in Lisbon, then show restaurants within 300 m."*

**Aerial imagery** (NAIP, USA only)

- *"Get NAIP imagery of Central Park and describe the land cover."*
- *"Find the Hoover Dam, get imagery within 1 km, and tell me if the
  reservoir is visible."*

**Country and city data** (Natural Earth, shown as a table or chart)

- *"Which 5 African countries have the largest population?"*
- *"Chart GDP per person by continent."*
- *"Which countries have the most cities with more than 1 million people?"*

**Your own data** (any public URL)

- *"Read https://raw.githubusercontent.com/datasets/airport-codes/main/data/airport-codes.csv
  and count the large airports in each country."*
- *"What columns does https://duckdb.org/data/holdings.parquet have?"*

**Scientific arrays** (Zarr)

- *"What variables are in the MUR sea surface temperature store?"*
- *"Take a sample of 5000 HRRR temperature values and give the mean in
  Celsius."*

**Grounding**

- *"How many people live in Atlantis?"* The assistant tells you that no tool
  can confirm this. It does not invent a number.

## How it works

```
 browser ──▶ agent API (agent_app.py) ──▶ MCP toolsets ──────────▶ data
 chat page   chat model + system prompt   duckdb-analyst  (SQL)    Overture, Natural Earth, Zarr, URLs
                                          naip-imagery    (images) Planetary Computer, Ollama vision model
```

1. You type a question in the chat page.
2. The agent API sends it to a chat model (Mistral by default) with a list of
   tools.
3. The model calls tools. Each toolset is a separate MCP server.
4. The tools send back a short text for the model and structured data for the
   page. The page shows the data as a map, an image, a table or a chart.

Large values, for example a search-area polygon or an image, do not go
through the model. The tools pass them to each other through
[session state](#how-the-tools-share-data).

## Run it on your computer

### Before you start

You need:

- [uv](https://docs.astral.sh/uv/) (Python 3.12 or 3.13),
- [Node.js](https://nodejs.org) 22.12 or later, to build the views (map,
  image, table and chart),
- an API key for a chat model. The repo installs the Mistral provider. For
  another provider, see the comments in `.example.env`.
- (optional) [Ollama](https://ollama.com), to describe aerial images. The
  default vision model runs in Ollama's cloud, so you do not need a GPU.

### Set up (one time)

```sh
uv sync                        # install Python dependencies into .venv
cp .example.env .env           # then set PROVIDER_MODEL and PROVIDER_API_KEY
./scripts/build-views          # build the map, image, table and chart views
ollama signin && ollama pull gemma4:cloud   # optional: image descriptions
```

### Start

Run each command in its own terminal, from the repo root. Start them in this
order, because the agent API finds the tools when it starts.

```sh
uv run mcp-serve-local                      # 1. the tools, on :8000
uv run uvicorn agent_app:app --port 8765    # 2. the agent API and chat page, on :8765
```

Open <http://localhost:8765>. The start page shows example questions as
buttons. Select one to start.

### If something goes wrong

| Problem | Cause and fix |
| --- | --- |
| `mcp-serve-local` does not start and names a missing view | Run `./scripts/build-views`. |
| The agent API stops at startup | `PROVIDER_MODEL` or `PROVIDER_API_KEY` is not set in `.env`. |
| The assistant has no tools | Set `MCP_URL=http://localhost:8000/` (the index root, not `.../mcp`), and start `mcp-serve-local` first. |
| Image descriptions fail | Ollama is not running, or you did not run `ollama signin`. To use a local model, set `OLLAMA_IMAGE_MODEL`. |
| Place lookups fail with "No files found" | Overture deletes old releases. Set `OVERTURE_RELEASE` to a current release from [the release list](https://docs.overturemaps.org/release/latest/). |
| A place lookup is slow | This is expected. Overture is read from the cloud on each query. |

## How the tools share data

A tool that makes a large value, for example `get_search_area`, stores it in
**session state** under a key. The model sees only the key:

```
Search area created: 1.0 km around Time Out Market Lisboa.  [state updated: duckdb-analyst/get_search_area/search_area]
```

The model then gives that key to the next tool:

```
places_within_area(category="cafe", area="@state:duckdb-analyst/get_search_area/search_area")
```

The runtime replaces the key with the real value before the tool runs.
Parameters tagged `NotAuthored` accept only a key, never a value that the
model wrote. Thus the model cannot invent a polygon, and a 100 KB image never
goes into the model's context.

| Tool (toolset) | Reads (parameter) | Writes (state key) |
| --- | --- | --- |
| `get_place` (duckdb-analyst) | — | `duckdb-analyst/get_place/place` |
| `get_search_area` (duckdb-analyst) | `place` | `duckdb-analyst/get_search_area/search_area` |
| `places_within_area` (duckdb-analyst) | `area` | `duckdb-analyst/places_within_area/places` |
| `fetch_naip_image` (naip-imagery) | `area` | `naip-imagery/fetch_naip_image/naip_image` |
| `interpret_image` (naip-imagery) | `image` | — |

The chat page has a session-state panel that shows these values. For the full
mechanism, see the runtime's [SESSION-STATE.md][session-state].

## The tools

| Toolset | Tool | What it does |
| --- | --- | --- |
| `duckdb-analyst` | `list_sources` | Lists the data sources that `query` and `chart` can read, with column descriptions. |
| | `query` | Runs one read-only SQL `SELECT` and returns the rows. The page shows a **table**. |
| | `chart` | Runs a query and puts the rows into a Vega-Lite spec. The page shows a **chart**. |
| | `get_place` | Finds one place in Overture Maps by name, inside a bounding box. The page shows a **map**. |
| | `get_search_area` | Draws a circle of a given radius (km) around that place. **Map.** |
| | `places_within_area` | Lists Overture places of one category inside the search area. **Map.** |
| `naip-imagery` | `fetch_naip_image` | Gets NAIP aerial imagery for the search area from Microsoft Planetary Computer. The page shows the **image**. |
| | `interpret_image` | Describes that image with a vision model served by Ollama. |

The DuckDB connection is locked down: SQL is read-only, and the tools cannot
read or write local files. See the module docstrings in
`toolsets/duckdb-analyst/src/duckdb_analyst/security.py` and `connection.py`.

### Call a tool without a model

Use `mcp-cli` to test a tool directly, while `mcp-serve-local` runs:

```sh
uv run mcp-cli list --url http://localhost:8000/duckdb-analyst/mcp
uv run mcp-cli call get_place \
  --url http://localhost:8000/duckdb-analyst/mcp \
  place_name="Golden Gate Bridge" search_bbox='[-122.6,37.6,-122.3,37.9]'
uv run mcp-cli repl --url http://localhost:8000/naip-imagery/mcp
```

## Find your way around

```
agent_app.py                  the assistant: system prompt, chat page title and example questions
toolsets/
  duckdb-analyst/
    src/duckdb_analyst/
      tools.py                list_sources, query, chart
      geo_tools.py            get_place, get_search_area, places_within_area
      connection.py           the DuckDB connection and its data sources
      security.py             the SQL checks
    ui/                       map, table and chart views
    tests/
  naip-imagery/               the same layout: fetch_naip_image, interpret_image, image view
tests/test_contract.py        checks every toolset against the runtime's rules
scripts/                      lint, test, format, build-views, remove-toolset
Dockerfile, charts/, .github/ build and deploy each toolset (see Deployment)
```

### Where to make a change

| To do this | Change this |
| --- | --- |
| Change how the assistant behaves | `GROUNDING_PROMPT` in `agent_app.py` |
| Change the chat page title or example questions | `UI` in `agent_app.py` |
| Add a data source for SQL | `connection.py`: add a view, and describe its columns for `list_sources` |
| Add a tool | Write it in the toolset's `tools.py` and add it to `TOOLS` |
| Add a toolset | `uv run mcp-toolset new <name>` (add `--with-ui` for a view) |
| Remove a toolset | `./scripts/remove-toolset <name>` |
| Use a different chat model | `PROVIDER_MODEL` in `.env` |

A tool that does I/O is `async def`. A sync tool does only computation. Each
tool returns a `ToolResult` or a `ToolError`. The runtime checks this at
startup, and `tests/test_contract.py` checks it in CI. For the rules, see the
runtime's [CONSUMING.md][consuming] §2.

## Development

```sh
./scripts/format        # ruff autofix and format
./scripts/lint          # ruff, and mypy over tests/ and toolsets/
./scripts/test          # pytest; arguments go to pytest, e.g. ./scripts/test -k naip
./scripts/build-views   # rebuild the views after you change a toolset's ui/
```

Some tests read live public data (Overture, Zarr stores). They fail when that
data source is not available.

## Built on mcp-toolsets

This repo contains the geo-assistant tools and nothing else. The server, the
agent, the chat page and the CLI come from one PyPI package,
[**mcp-toolsets-runtime**](https://github.com/developmentseed/mcp-toolsets-runtime)
[![PyPI](https://img.shields.io/pypi/v/mcp-toolsets-runtime?label=mcp-toolsets-runtime)](https://pypi.org/project/mcp-toolsets-runtime/).
The repo layout and the deploy workflows come from the
[**mcp-toolsets**](https://github.com/developmentseed/mcp-toolsets) template.

| Runtime module | What it does here |
| --- | --- |
| `mcp_runtime` | Serves each toolset as an MCP server (`mcp-serve`, `mcp-serve-local`) and the index of all toolsets (`mcp-index`). |
| `mcp_agent`, `mcp_agent_api` | The agent, its HTTP API and the chat page that `agent_app.py` serves. |
| `mcp_state` | Session state between tools. |
| `mcp_cli` | `mcp-cli`, to call tools by hand. |
| `mcp_toolset` | `mcp-toolset new`, to make a new toolset. |

Each toolset is a standard MCP server, so any MCP client can use it. The views
are standard [MCP Apps][ext-apps], so Claude, ChatGPT and other MCP Apps hosts
also show these views.

Do not change runtime behaviour in this repo. Fix it in the runtime, release
it, then update the version here:

```sh
uv lock --upgrade-package mcp-toolsets-runtime
```

Runtime documentation:

- [CONSUMING.md][consuming]: how to write a toolset, add a view, and serve
  the agent over HTTP.
- [SESSION-STATE.md][session-state]: how tools share large values.

[consuming]: https://github.com/developmentseed/mcp-toolsets-runtime/blob/main/docs/CONSUMING.md
[session-state]: https://github.com/developmentseed/mcp-toolsets-runtime/blob/main/docs/SESSION-STATE.md
[ext-apps]: https://github.com/modelcontextprotocol/ext-apps

## Deployment

Each toolset deploys to Kubernetes as its own service.

- **ci.yml** (pull requests and `main`): lint, tests, `helm lint`, and a
  Docker build (no push) of each changed toolset.
- **deploy.yml** (`main`): builds and pushes the image of each changed
  toolset, then runs `helm upgrade --install`. A change to a shared file
  (`charts/`, `Dockerfile`, `uv.lock`, root `pyproject.toml`) deploys all
  toolsets. A runtime update changes `uv.lock`, so it deploys all toolsets.
  When you delete a toolset directory and merge, the workflow uninstalls
  that service.

Deploys do not run until the `KUBE_CONFIG` secret and the `MCP_NAMESPACE`
variable are set. With the `MCP_INGRESS_HOST` secret, all toolsets share one
domain:

```
https://<host>/                   index: JSON list of each toolset and its tools
https://<host>/docs               the same list, in Swagger UI
https://<host>/<toolset>/mcp      MCP endpoint
https://<host>/<toolset>/health   liveness, with the toolset's tool names
```

Only the toolsets deploy. The agent API and chat page run locally for now.
To build one image locally:
`docker build --build-arg TOOLSET=duckdb-analyst .`

<details>
<summary>Set up a cluster from the beginning</summary>

You need a Kubernetes cluster (v1.24 or later) that can pull from `ghcr.io`.
`./scripts/bootstrap` sets the `MCP_NAMESPACE` variable and puts your
namespace into the `__MCP_NAMESPACE__` placeholders below.

1. **Namespace and a deploy service account.** The `KUBE_CONFIG` secret holds
   its kubeconfig. Do not use cluster-admin.

   ```sh
   kubectl create namespace __MCP_NAMESPACE__
   kubectl -n __MCP_NAMESPACE__ create serviceaccount deployer
   kubectl -n __MCP_NAMESPACE__ create role deployer --verb='*' \
     --resource=deployments.apps,services,secrets,serviceaccounts,ingresses.networking.k8s.io,roles.rbac.authorization.k8s.io,rolebindings.rbac.authorization.k8s.io
   kubectl -n __MCP_NAMESPACE__ create rolebinding deployer \
     --role=deployer --serviceaccount=__MCP_NAMESPACE__:deployer
   ```

   `KUBE_CONFIG` is a full kubeconfig file with a deployer token. GitHub's
   runners must be able to reach the cluster. The token expires (here, after
   about 90 days):

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

2. **GHCR pull secret.** Only if the repo is private, because then its images
   are private too:

   ```sh
   kubectl -n __MCP_NAMESPACE__ create secret docker-registry ghcr-pull \
     --docker-server=ghcr.io \
     --docker-username=<github-username> \
     --docker-password=<token-with-read:packages>
   ```

3. **ingress-nginx and cert-manager.** The chart's ingress defaults need both:

   ```sh
   helm upgrade --install ingress-nginx ingress-nginx \
     --repo https://kubernetes.github.io/ingress-nginx \
     --namespace ingress-nginx --create-namespace

   helm upgrade --install cert-manager cert-manager \
     --repo https://charts.jetstack.io \
     --namespace cert-manager --create-namespace \
     --set crds.enabled=true
   ```

4. **DNS and a ClusterIssuer.** Point an A or CNAME record for your hostname
   at the ingress controller's load balancer
   (`kubectl -n ingress-nginx get svc ingress-nginx-controller`). Set your
   email in `k8s/letsencrypt-clusterissuer.yaml`, then run
   `kubectl apply -f k8s/letsencrypt-clusterissuer.yaml`. cert-manager then
   issues and renews the `__MCP_NAMESPACE__-tls` certificate that all
   ingresses share.

5. **Shared domain.** `gh secret set MCP_INGRESS_HOST --body <the-hostname>`.
   Without it, the services are ClusterIP only:

   ```sh
   kubectl -n __MCP_NAMESPACE__ port-forward svc/mcp-duckdb-analyst 8000:8000
   uv run mcp-cli list
   ```

</details>
