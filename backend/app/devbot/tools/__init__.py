"""
DevBot's tools: what the model may call to read the systems, investigate across them,
and propose changes for the person to confirm.

Each tool is a plain function over a ToolContext (the person, their tokens) and a dict
of arguments, returning a small JSON-able dict -- no FastAPI in here -- so the same
functions could later be served as an MCP server without being rewritten.

Only the tools that fit a question are offered to the model (``specs_for``): every
tool's description is sent with every request and paid for against the person's
per-minute token allowance, so offering all of them for a question about one system
would spend a thousand tokens saying nothing.
"""

from __future__ import annotations

from .base import REGISTRY, Tool, ToolContext, run, specs_for  # noqa: F401

# Each module registers its tools when imported. One line per system, so a system
# DevBot can read is a system listed here.
from . import ado, artifactory, confluence, sonar  # noqa: F401,E402

# The investigations read across the systems above; registered after them.
from . import investigate  # noqa: F401,E402

# Changes the person reviews and confirms in the widgets' dialog; never made by DevBot.
from . import actions  # noqa: F401,E402
