"""``mcp_agent_api``'s app, with a grounding rule layered onto the system prompt.

``uvicorn mcp_agent_api.app:app`` (the module the README used to point at
directly) builds the agent with the runtime's default prompt, which only says
to use tools "whenever they can ground your answer; otherwise answer
directly" — not strong enough to stop the model answering a data question
(a place's identity or location, a count, what an image shows) from its own
training data when no tool call actually confirmed it. ``create_app`` documents
``build=`` for exactly this: a caller-supplied async factory that builds the
agent its own way (see ``mcp_agent_api.app``'s docstring). This module is that
factory, wired with ``GROUNDING_PROMPT`` and ``DATA_PROMPT`` composed the way
:func:`mcp_agent.main.with_session_state` documents — the host's own
instructions first, :data:`mcp_state.SESSION_STATE_PROMPT` last, since that
fragment is what the state middleware depends on the model having read.

A custom prompt also switches off what ``build_agent`` adds to its default
one: the runtime's ``BASE_PROMPT``, and ``INTERRUPT_GATE_PROMPT`` when the
``interrupt`` tool is wired in (it is by default). Both are added back here,
since an agent that has the ``interrupt`` tool but no instructions for it
asks its questions in plain text instead.

It also serves the runtime's bundled web client at ``/``, configured with
``UI`` below rather than the ``MCP_AGENT_UI_*`` environment variables: the
title and example questions describe this agent, so they belong in the repo.

Run with ``uv run uvicorn agent_app:app --port 8765`` in place of
``uvicorn mcp_agent_api.app:app`` — see the README.
"""

from mcp_agent.main import (
    BASE_PROMPT,
    SESSION_STATE_PROMPT,
    AgentSettings,
    Checkpointing,
    InterruptGateSettings,
    build_agent,
    with_interrupt_gate_prompt,
)
from mcp_agent_api.app import Built, Builder, create_app
from mcp_agent_api.ui import UiConfig, mount_ui

#: Kept separate from the runtime's ``BASE_PROMPT`` so the two compose
#: without duplicating "use your tools" — this is specifically about *data*
#: questions, the case the default prompt leaves the model free to guess on.
GROUNDING_PROMPT = (
    "Answer a data question — a place's identity or location, a count, a "
    "measurement, what an image shows, or any other fact about the real "
    "world — only from what your tools returned in this conversation. If "
    "you have not called the tool that would confirm it, or a tool came "
    "back empty or inconclusive, say so plainly and either call the right "
    "tool or tell the user you cannot confirm it. Never fill the gap with "
    "training data or a plausible-sounding guess."
)

#: How to get an answer out of data, which the grounding rule above does not
#: say. Its first sentence is true only because duckdb-analyst's ``query``
#: and ``chart`` messages carry the schema, the row count and a truncation
#: flag; the rest follows from how session state works: every result is
#: captured whatever its size, it cannot be queried with SQL, and a second
#: query in one turn replaces the first one's rows beyond recovery.
DATA_PROMPT = (
    "Working with data: a query's tool message gives its columns and types, "
    "its row count and whether the row cap truncated it, and the rows "
    "themselves when they are small; otherwise the rows are in session state. "
    "Answer counts, totals, rankings and filters with SQL (COUNT, SUM, GROUP "
    "BY, ORDER BY ... LIMIT), not by counting or reading through rows. The row "
    "count of a truncated result, or of a place list that says more exist, is "
    "not the total. Session state cannot be queried with SQL: to filter or "
    "aggregate a result again, run a new query. Use inspect_state to read "
    "specific values of a result you already have, not to aggregate it. A new "
    "query replaces the previous query's rows, so read what you need from one "
    "result before you run the next in the same turn, or answer both in one "
    "SQL statement. Call list_sources before you write SQL against a source "
    "whose columns you have not seen in this conversation."
)

SYSTEM_PROMPT = with_interrupt_gate_prompt(
    f"{BASE_PROMPT}\n\n{GROUNDING_PROMPT}\n\n{DATA_PROMPT}\n\n{SESSION_STATE_PROMPT}",
    InterruptGateSettings().mcp_agent_interrupt_gate,
)

#: The web client's header and opening screen. Each example is a button that
#: sends the question, and each one exercises a different tool chain.
UI = UiConfig(
    title="geo-assistant",
    tagline="Places, maps, aerial imagery and spatial SQL",
    examples=(
        "Find the Golden Gate Bridge and show me cafes within 1 km.",
        "Get NAIP imagery around the Golden Gate Bridge and describe what you see.",
        "Chart the 10 most populated places in France.",
        "What data sources can you query?",
    ),
)


def _build(checkpointing: Checkpointing) -> Builder:
    async def build() -> Built:
        settings = AgentSettings()
        return await build_agent(
            settings.mcp_url,
            settings.provider_model,
            settings.provider_api_key,
            checkpointer=await checkpointing.saver(),
            system_prompt=SYSTEM_PROMPT,
        )

    return build


# Our own checkpointer, built lazily on first use (see Checkpointing.saver).
# create_app(build=...) still constructs its own Checkpointing internally, but
# never opens a saver on it when a custom `build` is supplied — inert, per its
# own docstring ("checkpoint is not consulted ... nothing here asks for a
# saver unless the default factory does").
_checkpointing = Checkpointing()
app = create_app(build=_build(_checkpointing), ui=False)
mount_ui(app, config=UI)
