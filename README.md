# geo-assistant-mcp-toolset

The [geo-assistant](https://github.com/developmentseed/geo-assistant) workflow
rebuilt as MCP toolsets: Overture Maps place lookup, geodesic buffering and
spatial SQL over a locked-down DuckDB connection, plus NAIP aerial imagery
interpreted by a locally served vision model — with geometries and images
flowing between tools through session state (never through the model). Two
toolsets: `toolsets/duckdb-analyst/`, whose `geo_tools.py` is the ported
place → search-area → places-within-area chain, and `toolsets/naip-imagery/`,
which continues that chain — it fetches NAIP imagery from Microsoft Planetary
Computer over the same search area and describes it with an Ollama vision
model (`OLLAMA_BASE_URL`/`OLLAMA_IMAGE_MODEL`, default
`localhost:11434`/`gemma4:cloud`). The default is an Ollama **cloud**
model, so the localhost demo needs no GPU: `ollama signin`, then
`ollama pull gemma4:cloud`. Any locally pulled vision model works
via `OLLAMA_IMAGE_MODEL`.

This is an instance of the
[mcp-toolsets](https://github.com/developmentseed/mcp-toolsets) template — a
monorepo of **toolsets** — small packages of
[LangChain](https://python.langchain.com) tools — each auto-deployed as its own
[MCP](https://modelcontextprotocol.io) service on Kubernetes. Toolset
implementors write a single Python module; a shared runtime, one parameterized
Dockerfile and one generic Helm chart handle everything else. The template docs
below are kept as-is; where they mention the shipped example toolsets (`hello`,
`credential-demo`, `stac-explorer`), read `duckdb-analyst` — the examples were
not carried into this instance.

The runtime is not in this repo. It is
[**mcp-toolsets-runtime**](https://github.com/developmentseed/mcp-toolsets-runtime),
installed from PyPI like any other dependency — which makes this repo the
worked example of consuming it: what a toolset exports, how views are built and
served, and how the whole thing deploys. See
[The runtime dependency](#the-runtime-dependency).

```
toolsets/<name>/tools.py  ──▶  ghcr.io/<owner>/<repo>/mcp-<name>  ──▶  k8s Service mcp-<name>
   (LangChain @tool fns)        (Dockerfile --build-arg TOOLSET=...)     (charts/mcp-toolset)
```

Terminology: a **tool** is a single LangChain `@tool` function; a **toolset**
is a directory under `toolsets/` exporting a `TOOLS` list, deployed as one MCP
server.

## Use this template

This repository is a GitHub template — click **Use this template** to create
your own. The image registry path is derived from your repo name automatically;
the only thing to set is the Kubernetes namespace:

1. **Bootstrap once — do this first.** `./scripts/bootstrap` is the intended
   first step after creating your repo. It does three things:

   - sets the **`MCP_NAMESPACE`** repo Actions variable (the namespace your
     toolsets deploy into) via `gh` — **deploys are skipped until this is set**;
   - **rewrites the `__MCP_NAMESPACE__` placeholders** in `README.md` and
     `CLAUDE.md` in place, so the cluster-setup commands below become
     copy-pasteable for your namespace;
   - optionally removes the shipped example toolsets.

   ```sh
   ./scripts/bootstrap            # prompts for a namespace + which examples to keep
   # or non-interactively:
   ./scripts/bootstrap my-namespace --keep-examples
   ```

   Re-running is safe. The namespace lives only in the repo variable, not a
   committed file, so it never carries over to repos generated from your
   instance. If `gh` isn't set up when you bootstrap, the script prints the one
   command to set the variable yourself
   (`gh variable set MCP_NAMESPACE --body <namespace>`); the `__MCP_NAMESPACE__`
   placeholders then stay literal until you re-run bootstrap or edit them by hand.

2. **Develop** — `uv sync`, then add a toolset (`uv run mcp-toolset new`) or play
   with the shipped `hello` example (see [Quickstart](#quickstart)).

3. **Deploy when ready** — set the `KUBE_CONFIG` secret (see
   [Kubernetes cluster setup](#kubernetes-cluster-setup)). Until you do, CI runs
   lint/tests/build on every push but **skips the deploy** — a fresh instance is
   green out of the box, with no cluster required.

The repo ships two example toolsets you can keep, copy or delete: `hello` (the
smallest thing that deploys) and `credential-demo` (the
[per-user credentials](#per-user-credentials) pattern).

## The runtime dependency

[![PyPI](https://img.shields.io/pypi/v/mcp-toolsets-runtime?label=mcp-toolsets-runtime)](https://pypi.org/project/mcp-toolsets-runtime/)

Everything that isn't a toolset comes from one PyPI package,
[`mcp-toolsets-runtime`](https://github.com/developmentseed/mcp-toolsets-runtime),
bounded in the root `pyproject.toml` and pinned exactly by `uv.lock`. Below is
what you reach for from *this* repo and when; the package's own README is the
authority on everything it exposes, and stays current when this doesn't:

| Module | What you use it for here |
| --- | --- |
| `mcp_runtime` | **Required.** Serves a toolset's `TOOLS` as a stateless streamable-HTTP MCP server with a `/health` route for k8s probes, and its `VIEWS` as `ui://` resources — `mcp-serve` for one toolset, `mcp-serve-local` for all of them at once. Also runs the directory service (`mcp-index`). |
| `mcp_cli` | **Development inner loop.** Typer/rich client (`mcp-cli`) to list and call tools on a running service. |
| `mcp_toolset` | **Scaffolding.** `mcp-toolset new [--with-ui] <name>` writes a conforming toolset into `toolsets/` and registers it in the workspace. |
| `mcp_agent` | **Optional example chat** (`mcp-agent` / `mcp-agent-web`) that discovers every server behind an index URL and drives their tools. `mcp-agent-web` needs the `[web]` extra; `[agent]` alone gives `build_agent`, `run_turn` and the host helpers without Chainlit, for a frontend of your own. Drop the extra from the pin if you want neither. |
| `mcp_state` | **In use here, both modes.** It keeps large tool values out of the model's context. Every `ToolResult` data key is declared in the tool's `_meta` and captured into session state by the bundled agent, whether or not you tag anything. The geo tools also tag keys and parameters with a `Kind`, so geometries pass between them through state — see below. |

[session-state]: https://github.com/developmentseed/mcp-toolsets-runtime/blob/main/docs/SESSION-STATE.md

Upgrade with `uv lock --upgrade-package mcp-toolsets-runtime`; because `uv.lock`
is a shared build input, merging the bump rebuilds and redeploys every toolset.
Fix runtime behaviour upstream and release it — never patch it here, since
nothing local would survive the next `uv sync`.

This repo owns `toolsets/*` — one directory per toolset, each becoming an MCP
service — `charts/*`, the `Dockerfile`, the workflows, and
`tests/test_contract.py`.

### Session state, and what tagging a `Kind` adds

Nothing has to opt in for the baseline: `query` advertises its `rows` key in
its `_meta`, and driving it from the bundled agent moves the result rows into
session state instead of the transcript. Best endeavours is the default, so an
untagged tool already gets the context saving.

What a `Kind` tag adds is *resolution between tools*. Untagged, a value is
addressable only by its key, and the model has to pass it along — as an
`@state:<key>` handle rather than the value itself, but still by hand. Tagged,
the consuming tool's parameter is filled from state by kind, which means the
producer and the consumer can live in different toolsets on different servers,
and the parameter leaves the model's schema entirely — it can't be hallucinated
because it is never offered.

The ported geo-assistant state flow is the worked example: kind-tagged hops
spanning two toolsets — `duckdb-analyst`'s `geo_tools.py` and
`naip-imagery` — which is exactly the cross-server case the kinds exist for.

| Tool (toolset) | Consumes (parameter) | Publishes (result key) |
| --- | --- | --- |
| `get_place` (duckdb-analyst) | — | `place` · `geojson.PlaceFeature` |
| `get_search_area` (duckdb-analyst) | `place` · `geojson.PlaceFeature` | `search_area` · `geojson.AreaOfInterest` |
| `places_within_area` (duckdb-analyst) | `area` · `geojson.AreaOfInterest` | `places` · untagged |
| `fetch_naip_image` (naip-imagery) | `area` · `geojson.AreaOfInterest` | `naip_image` · `image.JpegBase64` |
| `interpret_image` (naip-imagery) | `image` · `image.JpegBase64` | — |

`geojson.AreaOfInterest` comes from the runtime's `mcp_runtime.kinds`
vocabulary. `geojson.PlaceFeature` (in `geo_tools.py`) and `image.JpegBase64`
(in `naip-imagery`) are minted locally, because the vocabulary has no kinds
for them yet — kinds are plain strings, so producer and consumer agreeing on
the text is the whole contract (worth upstreaming by PR). The image hop is
where the state flow pays most: a 512px JPEG is ~100KB of base64 that never
enters the chat model's context on its way to the vision model. The SQL tools stay untagged on purpose: `chart` takes SQL
and re-runs it rather than consuming `query`'s captured `rows` from state, so
no parameter here resolves them by kind.

Tagging also buys *traceability*, which is the part that only exists because the
parameter is hidden. A filled parameter is absent from the tool call the model
produced, so runtime 0.2.1 records a **receipt** on the tool message naming the
key, the kind and the publishing tool. The chat's tool step shows it where the
argument would have been, and the model is told in one line:

```
[state used: area ← duckdb-analyst/search_area, published by get_search_area]
```

Receipts are recorded on the handle path too. The model is still told nothing —
it wrote `@state:<key>` itself, so the key is already in the arguments — but
since runtime 0.4.2 the chat's tool step annotates the handle with what the key
resolved to, which the bare string does not say:

```
request: @state:duckdb-analyst/rows · untyped · 1000 item(s) · from query
```

The `@state:<key>` handle form is only offered on `object` and `array`
parameters. Untagged keys like `query`'s `rows` stay addressable this way, but
nothing in this repo consumes them: the structured parameters that do exist are
either kind-filled (the geo hops above) or model-authored (`chart`'s `spec`).

Two consequences worth knowing before you read a deployment. `/health` and the
index report `state.produces` as the list of distinct **kinds** — for
`duckdb-analyst` that is `geojson.AreaOfInterest` and `geojson.PlaceFeature` —
so untagged keys (`query`'s `rows`, `chart`'s `spec`) never appear there; their
per-key declarations are in each tool's MCP `_meta`. And the guarantee is about what
reaches the *model*, not about what leaves the process: LangChain hands every
tool call the whole agent state, so a tracing backend wired to the chat records
stored payloads on every subsequent call. That is upstream behaviour, unrelated
to whether you use session state at all, but it is the wrong thing to discover
after turning tracing on.

Keeping values out of the context is client-side work, so external hosts do none
of it — served to Claude.ai or ChatGPT, these toolsets behave like any other, and
tool returns still have to be a sensible size on their own. The full contract,
with sequence diagrams and a runnable demo, is in the runtime's
[SESSION-STATE.md][session-state].

## Quickstart

```sh
uv sync                # runtime from PyPI + every toolset, into one .venv
./scripts/test         # run all tests
./scripts/lint         # ruff + mypy (./scripts/format to autofix)

# Serve every toolset in one process, with the index at /
uv run mcp-serve-local

# ...or a single toolset on its own, the way production runs it
TOOLSET=hello uv run mcp-serve

# Talk to them from another shell
uv run mcp-cli list --url http://localhost:8000/hello/mcp
uv run mcp-cli call hello name=dev --url http://localhost:8000/hello/mcp
uv run mcp-cli repl --url http://localhost:8000/hello/mcp
uv run mcp-cli call whoami \
  --url http://localhost:8000/credential-demo/mcp -H "X-Demo-Token: s3cret"
```

`mcp-serve-local` mounts each toolset at `/<name>/mcp` and serves the index
document at `/` — the same URL shape the shared domain has in production, so an
`mcp-cli`, an `mcp-agent` or an MCP Inspector session can be pointed at it
unchanged. Use it for the inner loop; reach for `mcp-serve` when you want a
toolset isolated exactly as its own pod runs it (one process, one toolset,
`/mcp` at the root, `PORT` to move it).

`mcp-cli` defaults to `http://localhost:8000/mcp`, which is where a bare
`mcp-serve` puts a toolset — hence the explicit `--url` above. Toolsets are also
importable directly (e.g. `from hello.tools import TOOLS`) for in-process use in
tests, notebooks or an agent repo.

## Adding a toolset

No Docker, Kubernetes or MCP knowledge needed — write ordinary LangChain
tools and merge.

1. Scaffold with the runtime's generator (registers the package in the uv
   workspace too):

   ```sh
   uv run mcp-toolset new my-toolset
   ```

2. Write your tools in `toolsets/my-toolset/src/my_toolset/tools.py`:

   ```python
   from typing import Any, NotRequired

   from langchain_core.tools import tool

   from mcp_runtime.tool_result import ToolError, ToolResult

   class DoSomethingResult(ToolResult):
       """Matches for the query, each with an 'id' and a 'score'."""

       matches: NotRequired[list[dict[str, Any]]]

   @tool
   def do_something(query: str, limit: int = 10) -> DoSomethingResult | ToolError:
       """One-line description — docstrings and type hints ARE the MCP schema."""
       ...
       return DoSomethingResult(message=f"Found {len(matches)} match(es).", matches=matches)

   TOOLS = [do_something]
   ```

   `TOOLS` is the only required export. Non-empty docstrings and the
   [ToolResult return contract](#typed-tool-returns) are enforced by a
   contract test. If a tool does I/O (HTTP, database), write it as
   `async def` — `@tool` supports coroutines natively; sync tools are fine
   for pure computation (the runtime runs them in a thread pool). If a tool
   needs the *user's* credentials, read them from the request headers — see
   [Per-user credentials](#per-user-credentials). The shipped `hello` toolset
   is a minimal starting point you can copy.

3. Add tests in `toolsets/my-toolset/tests/test_my_toolset.py` and run
   `./scripts/test`.

4. (Optional) `toolsets/my-toolset/toolset.yaml` holds Helm value overrides —
   secrets to mount via `envFrom`, env vars, resources, replicas. See
   `charts/mcp-toolset/values.yaml` for the available keys.

5. Merge to `main`. CI builds `ghcr.io/<owner>/<repo>/mcp-my-toolset` and
   deploys the `mcp-my-toolset` service automatically.

Conventions: directory `toolsets/<name>` (kebab-case) → module
`<name_snake_case>.tools` → service `mcp-<name>`.

## Typed tool returns

Every tool returns one dict per call, in one of two shapes from
`mcp_runtime.tool_result`:

- **`ToolResult`** — success: a required str `message` (the human-readable
  answer a model or UI reads first) plus any data keys your tool declares.
- **`ToolError`** — a structured error: a short machine-readable `error`
  kind and a `detail` saying what happened or what to do next.

The runtime derives each tool's MCP `outputSchema` from its return
annotation, advertises it in `tools/list`, validates every result against it
before sending, and delivers results as typed `structuredContent` (alongside
the usual text block). A tool whose annotation doesn't follow the contract
**fails at startup** (`build_server` aborts, naming the tool) and fails the
contract test in CI — never silently at chat time.

How to annotate:

- Minimum: `-> ToolResult | ToolError` for tools whose message is the whole
  answer (drop the `ToolError` arm if the tool raises instead of returning
  errors — exceptions become MCP `isError` results, which skip schema
  validation).
- Recommended: one `ToolResult` subclass per tool, adding each data key as
  `NotRequired[...]`, annotated `-> MyResult | ToolError`. Give the subclass
  a one-line docstring — it becomes the schema's `description`. Nested
  payloads can be TypedDicts or pydantic models all the way down.
- Construct returns with TypedDict call syntax —
  `ToolResult(message=...)`, `ToolError(error="not_found", detail=...)` —
  mypy-checked, still a plain dict at runtime. `is_error()` (a `TypeIs`
  guard) narrows helper results typed `dict[str, Any] | ToolError` in both
  branches.

Rules and gotchas:

- Keys not declared in the annotation are silently dropped from
  `structuredContent` — the annotation is the complete list of keys a client
  can see, and mypy flags undeclared keys in return literals.
- Union arms must all be TypedDicts/pydantic models; bare `str`/`list`
  returns and `dict[str, Any]` are rejected at startup (FastMCP would wrap
  the former in `{"result": ...}`, changing your payload shape; the latter
  guarantees nothing). Put data under a named key instead.
- The annotation must be on the function `@tool` wraps; the runtime reads it
  via `tool.coroutine`/`tool.func`.

Verify locally: `TOOLSET=my-toolset uv run mcp-serve`, then `tools/list`
(via MCP Inspector or `mcp-cli`) shows each tool's `outputSchema`, and
`tools/call` responses carry `structuredContent`.


## Toolset UI views

A tool can ship a **view**: a small frontend component (a map, a gallery, a
chart) that an MCP Apps host — Claude, ChatGPT, or the bundled Chainlit agent —
renders in a sandboxed iframe and feeds the tool's `structuredContent`. The runtime stays pure-Python: a view is a build-time HTML
bundle served as an MCP resource; nothing new executes at call time. Views are
**progressive enhancement** — the tool's `message` and structured data still
stand alone in a plain client, so a view never changes what a tool returns.

Scaffold a toolset with an example view, then build it (needs node):

```sh
uv run mcp-toolset new --with-ui my-toolset
cd toolsets/my-toolset/ui && npm install && npm run build
```

### The contract

A toolset opts in with three things, validated at startup — a missing bundle,
or a view naming an unknown tool, aborts `build_server`:

1. **`VIEWS`** — a `{tool_name: view_id}` export in the tools module.
2. **A built bundle** at `<package>/views/<view_id>.html`, self-contained (all
   JS/CSS inlined). The shipped `ui/` builds these with Vite +
   `vite-plugin-singlefile`, one pass per view (`VIEW=<id> vite build`), writing
   into the package's `views/` dir. Built bundles are git-ignored; the
   Dockerfile's node stage and `./scripts/build-views` rebuild them.
3. **The host bridge** — [`@developmentseed/mcp-view`][mcp-view], the npm half of
   the runtime. It wraps the MCP Apps `ui/*` postMessage protocol in two
   functions, so a view never hand-rolls the wire format:

   ```ts
   import { onData, sendMessage } from "@developmentseed/mcp-view";

   onData<MyResult>((data) => render(data));  // the tool's structuredContent
   button.onclick = () => sendMessage("…");   // a user turn back into the chat
   ```

   Any framework works; only this seam is fixed. Add it to your `ui/`
   dependencies — it's a public package, so no registry auth here or in CI.

Given that, the runtime does two standard-MCP things: it serves each view as a
resource `ui://<toolset>/<view_id>` and stamps the owning tool's `_meta` with
that URI. Because that follows the [MCP Apps][ext-apps] standard, any MCP Apps
host renders the same bundle unchanged — Claude, ChatGPT, Goose, VS Code — and
so does the bundled Chainlit agent, whose `McpView.jsx` element implements the
host end of the identical protocol.

[mcp-view]: https://www.npmjs.com/package/@developmentseed/mcp-view
[ext-apps]: https://github.com/modelcontextprotocol/ext-apps

### Credentials never reach the iframe

A view can do exactly as much as what the tool put in its `ToolResult`: pass
**pre-signed or short-lived URLs** (tiles, thumbnails), never tokens. The
[per-user credential](#per-user-credentials) invariant is unchanged — secrets
ride the MCP transport as headers, never the conversation or the iframe. For an
authenticated data source, the tool mints a signed URL server-side and returns
it in the result.

### Interactions advance the chat

A view is an input device, not just a picture: an interaction calls
`sendMessage(...)`, which arrives back as a user message, so the model reads it
and calls the next tool. `toolsets/stac-explorer` is a worked example — a
collection gallery whose "Show on map" button drives a second tool that renders
the selected data on a map.

### Viewing them in the bundled Chainlit agent

External MCP Apps hosts need nothing from you beyond the contract above.
Chainlit isn't one out of the box, so the runtime ships the host-side element
that makes it one, and you install it into the app root once (it lands in the
git-ignored `public/elements/`):

```sh
uv run mcp-agent install-elements    # writes public/elements/McpView.jsx
```

Re-run it after a runtime upgrade to pick up the new element. Nothing is written
at runtime, so this works on a read-only filesystem; `mcp-agent-web` starts
without it but warns and won't render views.

## Removing a toolset

```sh
./scripts/remove-toolset my-toolset
```

Merge to `main`. Removal is GitOps like everything else: the deploy
workflow reconciles the cluster against `toolsets/`, uninstalling any
`mcp-<name>` release whose directory no longer exists — Deployment, Service
and Ingress with it; the index drops the entry automatically. Mind that
this means merging a deleted directory tears down the live service.

Not removed automatically: out-of-band Secrets the toolset listed in its
`toolset.yaml` (`kubectl -n __MCP_NAMESPACE__ delete secret <name>`) and its
images in GHCR (delete the package from the repo settings if you care).

## Deployment

- **ci.yml** (PRs + main): lint, tests, `helm lint`, and a no-push Docker
  build of every image affected by the change. Always runs — no cluster needed.
- **deploy.yml** (main): detects changed toolsets (`scripts/changed-toolsets`)
  — changes to shared build inputs (`charts/`, `Dockerfile`, `uv.lock`, root
  `pyproject.toml`) rebuild *all* toolsets, which is how a runtime version bump
  reaches every service — then per toolset: build and push
  `ghcr.io/<owner>/<repo>/mcp-<name>:<sha>` and
  `helm upgrade --install mcp-<name> charts/mcp-toolset -n __MCP_NAMESPACE__`.
  A reconcile job also uninstalls releases whose `toolsets/<name>` directory
  is gone — see [Removing a toolset](#removing-a-toolset).
- **Deploy guard**: the cluster-touching jobs are skipped unless **both** the
  `KUBE_CONFIG` secret and the `MCP_NAMESPACE` variable are set, so a freshly
  instantiated template never fails CI trying to reach a cluster that doesn't
  exist yet — and never deploys into an unintended namespace.
- **Required secret**: `KUBE_CONFIG` — a kubeconfig with rights to manage the
  deploy namespace. Images push to GHCR with the built-in `GITHUB_TOKEN`.
- **Required variable**: `MCP_NAMESPACE` — the namespace every release deploys
  into, set by `./scripts/bootstrap`. As a repo variable it stays per-instance,
  so two repos sharing a cluster don't collide.
- **Optional secret**: `MCP_INGRESS_HOST` — a shared hostname. When set, every
  toolset also gets an Ingress on that host at `/<name>`, and an `mcp-index`
  service (the same `Dockerfile` built with `TOOLSET=index`, which installs the
  runtime alone; deployed via `charts/mcp-index`) serves a directory of all
  toolsets at the domain root — see
  [Kubernetes cluster setup](#kubernetes-cluster-setup). When unset, services
  stay ClusterIP-only and the only access is `kubectl port-forward` via
  cluster RBAC:

```sh
kubectl -n __MCP_NAMESPACE__ port-forward svc/mcp-hello 8000:8000
uv run mcp-cli list
```

Build an image locally with `docker build --build-arg TOOLSET=hello .`.

- **Optional secret**: `MCP_CHAT_HOST` — a hostname for the hosted chat UI (see
  [Hosted chat](#hosted-chat-bring-your-own-model)). When `MCP_INGRESS_HOST` is
  set, the deploy builds `Dockerfile.chat` and installs `charts/mcp-chat` on
  this host (default `chat.<MCP_INGRESS_HOST>`). It needs its own DNS record and
  a TLS cert (`<namespace>-chat-tls`, issued by cert-manager if configured).

## Hosted chat (bring your own model)

The runtime's `mcp_agent` Chainlit UI can also run as a public web app over the
deployed toolsets, at `chat.<shared-domain>`. It is **bring-your-own-model**:
the deployment holds no provider key. Each user opens ⚙ settings and enters a
`provider:model` and their own API key (and any per-toolset credential headers);
the key lives only in that browser session — never sent to the model, logged, or
stored server-side — so exposing the host exposes no server-held secret and the
model spend is the user's own. The image (`Dockerfile.chat`) bundles a set of
providers (`anthropic`, `openai`, `google-genai`, `mistralai`) so any of them
works without a rebuild; the workspace itself stays provider-agnostic.

It deploys automatically alongside the index when `MCP_INGRESS_HOST` is set (a
shared-code change or a `workflow_dispatch` run). There is no built-in auth —
BYOM removes the shared-key abuse risk, but put an auth proxy in front (or
enable Chainlit auth) if you need to restrict who can use it.

Conversations are checkpointed per thread, **in the pod's memory by default** —
so a restart, a redeploy or a scale-up past the chart's single replica loses
them. That is fine for demos and is why nothing extra is deployed for it. To
keep conversations, point `MCP_AGENT_CHECKPOINT` at a PostgreSQL URL and add the
runtime's `[checkpointing-postgres]` extra to the chat image. The same
per-thread state also carries what the toolsets published, so if you do adopt
`mcp_state`, where conversations live becomes a real decision rather than a
detail.

## Kubernetes cluster setup

The deploy workflow assumes an existing cluster. Minimum requirements: a
conformant cluster (v1.24+) with outbound access to `ghcr.io`, plus the
one-time setup below. `__MCP_NAMESPACE__` is the namespace you chose at bootstrap
(the `MCP_NAMESPACE` variable's value); substitute it in the commands.

1. **Namespace and a scoped deploy service account** — the kubeconfig behind
   the `KUBE_CONFIG` GitHub secret. Don't use cluster-admin:

   ```sh
   kubectl create namespace __MCP_NAMESPACE__
   kubectl -n __MCP_NAMESPACE__ create serviceaccount deployer
   kubectl -n __MCP_NAMESPACE__ create role deployer --verb='*' \
     --resource=deployments.apps,services,secrets,serviceaccounts,ingresses.networking.k8s.io,roles.rbac.authorization.k8s.io,rolebindings.rbac.authorization.k8s.io
   kubectl -n __MCP_NAMESPACE__ create rolebinding deployer \
     --role=deployer --serviceaccount=__MCP_NAMESPACE__:deployer
   ```

   (`secrets` is Helm's release storage; `serviceaccounts`/`roles`/
   `rolebindings` are needed to install `charts/mcp-index`.)

   `KUBE_CONFIG` is a complete kubeconfig file with a deployer token inside —
   not the token alone. The API server URL must be reachable from GitHub's
   runners, and the token expires (~90 days here), after which deploys fail
   until the secret is refreshed:

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

2. **GHCR pull secret** — if the repo is private, its images are too. Both
   charts reference a `ghcr-pull` Secret by default; create it from a GitHub
   personal access token (classic) with the `read:packages` scope:

   ```sh
   kubectl -n __MCP_NAMESPACE__ create secret docker-registry ghcr-pull \
     --docker-server=ghcr.io \
     --docker-username=<github-username> \
     --docker-password=<token-with-read:packages>
   ```

3. **ingress-nginx** — the charts' ingress defaults assume it:

   ```sh
   helm upgrade --install ingress-nginx ingress-nginx \
     --repo https://kubernetes.github.io/ingress-nginx \
     --namespace ingress-nginx --create-namespace
   ```

4. **cert-manager** — issues and renews the shared domain's certificate:

   ```sh
   helm upgrade --install cert-manager cert-manager \
     --repo https://charts.jetstack.io \
     --namespace cert-manager --create-namespace \
     --set crds.enabled=true
   ```

5. **DNS + a ClusterIssuer.** Point an A/CNAME record for your chosen
   hostname at the ingress controller's load balancer
   (`kubectl -n ingress-nginx get svc ingress-nginx-controller`), and tell
   cert-manager how to reach Let's Encrypt — the one resource it can't
   create for itself. Set your email in `k8s/letsencrypt-clusterissuer.yaml`
   (Let's Encrypt sends expiry warnings there), then:

   ```sh
   kubectl apply -f k8s/letsencrypt-clusterissuer.yaml
   ```

   Certificates are then automatic: the `mcp-index` Ingress is annotated
   `cert-manager.io/cluster-issuer: letsencrypt`, so cert-manager issues and
   renews the `__MCP_NAMESPACE__-tls` Secret that all the Ingresses share. If
   your issuer is named differently, override `ingress.clusterIssuer` in
   `charts/mcp-index`.

6. **Per-toolset Secrets**, created out-of-band (`kubectl create secret ...`),
   for any names a toolset lists under `secrets:` in its `toolset.yaml`.

Finally set the optional shared-domain secret (step 1 already pushed
`KUBE_CONFIG`):

```sh
gh secret set MCP_INGRESS_HOST --body <the-hostname>
```

### One domain for all toolsets

With `MCP_INGRESS_HOST` set (e.g. `mcp.example.com`), the domain serves:

```
https://<host>/                   # index: JSON directory of every toolset + its tools
https://<host>/docs               # the same directory, browsable (Swagger UI)
https://<host>/<toolset>/mcp      # MCP endpoint (prefix stripped by ingress)
https://<host>/<toolset>/health   # liveness, lists the toolset's tool names
```

Anyone you give the URL to can discover what's deployed from the root index —
`mcp-index` lists the toolset Services via the Kubernetes API and asks each
one's `/health` for its tool names, so it always reflects what is actually
running:

```sh
curl https://<host>/ | jq
uv run mcp-cli list --url https://<host>/hello/mcp
```

The index's `connections` key is shaped for
`langchain_mcp_adapters.client.MultiServerMCPClient`, so an agent can consume
every deployed toolset in three lines:

```python
import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

connections = httpx.get("https://<host>/").json()["connections"]
tools = await MultiServerMCPClient(connections).get_tools()
```

The runtime's `mcp-agent` does exactly that as an interactive chat. The model is
provider-agnostic and no provider ships by default: `PROVIDER_MODEL` is a
`provider:model` string passed to LangChain's `init_chat_model` and
`PROVIDER_API_KEY` is that provider's key. Pick a provider, install its package
(`uv add langchain-openai`), and set both — in the environment or a `.env`
file (copy `.example.env`):

```sh
uv add langchain-openai                          # one-time: install a provider
export PROVIDER_MODEL=openai:gpt-4o-mini PROVIDER_API_KEY=sk-...

uv run mcp-agent https://<host>/                # all deployed toolsets
uv run mcp-agent http://localhost:8000/         # or every toolset from mcp-serve-local
uv run mcp-agent http://localhost:8000/mcp      # or one local mcp-serve
uv run mcp-agent --model anthropic:claude-3-5-haiku-latest   # override the model
uv run mcp-agent                                # url + model from .env
```

An index URL and a single server URL are both accepted, so the local loop above
and the deployed domain are the same command with a different argument.

Any `init_chat_model` provider works (`openai:`, `anthropic:`, `mistralai:`,
…) — switching is a `PROVIDER_MODEL` change plus that provider's package. The
same agent is available as a Chainlit chat UI: `uv run mcp-agent-web` serves it
at `http://localhost:8080`. It is **bring-your-own-model** — set the model and
API key in ⚙ settings, or pre-fill them from the environment/.env
(`PROVIDER_MODEL`, `PROVIDER_API_KEY`); `MCP_URL` (which index or server to chat
with) and `CHAINLIT_PORT` also come from there. See
[Hosted chat](#hosted-chat-bring-your-own-model) to run it as a public web app.

Each Helm release owns its own Ingress for the same host and the controller
merges them, so the domain's routing table tracks deploys with no central
config to edit; the index's `/` path only catches what no toolset claims.

### Per-user credentials

Tools that act on a user's behalf (with credentials that differ per calling
user) must not bake secrets into the deployment — and must not take them as
tool arguments either, or the model sees them and they land in chat history
and traces. Instead the client sends them as HTTP headers on every MCP
call, and the tool reads them at call time:

```python
from mcp_runtime.credentials import credential_from_header

@tool
def whoami() -> WhoamiResult:
    """Report which account the calling user's credential belongs to."""
    token = credential_from_header("x-demo-token")
    ...

TOOLS = [whoami]
CREDENTIAL_HEADERS = ["x-demo-token"]  # advertised; validated by the contract test
```

The `CREDENTIAL_HEADERS` export is advertised in the toolset's `/health` and
in the index's `toolsets` entries, so clients know which toolset needs which
credential — and send each one *only* to the connections that declare it,
never to unrelated toolsets. `toolsets/credential-demo` is a working
(stubbed) example. Clients attach the header per connection — agents by
decorating the index's `connections` map, `mcp-cli` with `-H`:

```python
connections = httpx.get("https://<host>/").json()["connections"]
connections["credential-demo"]["headers"] = {"X-Demo-Token": user_token}
tools = await MultiServerMCPClient(connections).get_tools()
```

`mcp-agent` goes further, in the shape a multi-user deployment needs: the
agent is built **once** and credentials are supplied per call. Each
connection gets an httpx client factory that, at request time, injects the
calling user's headers — only those the toolset's advertised declaration
names (for a direct single-server URL the agent asks the endpoint's sibling
`/health` for its declaration):

```python
from mcp_agent.main import user_credentials

with user_credentials({"x-demo-token": the_users_token}):
    result = await agent.ainvoke(...)
```

The Chainlit UI builds a settings field (⚙ by the message box) for every
credential header the connected toolsets advertise and applies the values
per message — so one long-lived agent process serves many users, each with
their own credentials.

```sh
uv run mcp-cli call whoami \
  --url https://<host>/credential-demo/mcp -H "X-Demo-Token: $TOKEN"
```

The secret rides the transport (TLS-encrypted at the ingress), never the
conversation, and the service stays stateless: every call carries its own
credential, so one pod serves all users. A missing header raises a
`MissingCredentialError` whose message tells the caller how to supply it.
Test credential-using tools without a server via
`mcp_runtime.credentials.header_context`:

```python
with header_context({"x-demo-token": "secret"}):
    whoami.invoke({})
```

## Development

```sh
./scripts/format        # ruff autofix + format
./scripts/lint          # ruff checks + mypy over tests/ and toolsets/
./scripts/test          # pytest (args forwarded, e.g. ./scripts/test -k hello)
```

The root `pyproject.toml` defines the uv workspace (`toolsets/*`), the
`mcp-toolsets-runtime` pin, shared tool configuration and the dependency
groups; `uv.lock` pins the runtime and the whole workspace consistently, and is
what the images build from.

`tests/` holds only the toolset contract sweep — every directory under
`toolsets/` must import, export a non-empty `TOOLS`, and satisfy the same
`ToolResult` and docstring gates `build_server` applies at startup. Tests for
runtime behaviour live in
[mcp-toolsets-runtime](https://github.com/developmentseed/mcp-toolsets-runtime),
not here.
