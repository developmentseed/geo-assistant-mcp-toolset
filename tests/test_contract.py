"""Contract test: every directory under toolsets/ must export valid TOOLS.

Doubles as a per-toolset import smoke test and enforces non-empty
descriptions (docstrings become the MCP schema, so they are part of the
contract) and the ToolResult return contract (annotations become the MCP
output schema, so every tool in every toolset must convert cleanly).

The sweep itself is a *consumer* concern: mcp-toolsets-runtime ships the gates
it applies (``to_fastmcp``, ``load_credential_headers``, and ``build_server``
behind them) but not the walk over this repo's toolsets/, which it has no way
to locate.
"""

import importlib
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from mcp_runtime.fastmcp_output import to_fastmcp
from mcp_runtime.server import load_credential_headers

TOOLSETS_DIR = Path(__file__).resolve().parents[1] / "toolsets"
TOOLSET_NAMES = sorted(
    path.name for path in TOOLSETS_DIR.iterdir() if (path / "pyproject.toml").is_file()
)


def test_toolsets_discovered():
    assert TOOLSET_NAMES


@pytest.mark.parametrize("toolset", TOOLSET_NAMES)
def test_toolset_contract(toolset):
    module_name = toolset.replace("-", "_") + ".tools"
    module = importlib.import_module(module_name)
    load_credential_headers(module_name)  # validates the optional export's shape
    tools = module.TOOLS
    assert isinstance(tools, list) and tools, f"{toolset}: TOOLS must be non-empty"
    for tool in tools:
        assert isinstance(tool, BaseTool), f"{toolset}: {tool!r} is not a BaseTool"
        assert tool.description and tool.description.strip(), (
            f"{toolset}: tool {tool.name!r} needs a non-empty docstring"
        )
        # The same ToolResult gate build_server applies at startup.
        converted = to_fastmcp(tool)
        assert converted.output_schema is not None
