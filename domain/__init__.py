"""Pasto10 domain — the DomainSpec that Semente assembles into the app.

Exposes ``domain_spec``: the area registration tools and the dynamic
tool/instruction callables (``domain.agent``) driven by the registration
state machine.

Tool modules resolve their descriptions from ``domain/prompts/tools.yml`` at
import time. The Semente app applies the manifest's ``prompts_dir`` before
importing the domain; ``SEMENTE_PROMPTS_DIR`` is defaulted here so that a
bare ``import domain`` (scripts, REPL) finds the same prompts. An explicit
env var or ``set_prompts_dir`` call still takes precedence.
"""

import os
from pathlib import Path

os.environ.setdefault("SEMENTE_PROMPTS_DIR", str(Path(__file__).resolve().parent / "prompts"))

from semente import DomainSpec  # noqa: E402

from domain.agent import get_instructions, get_tools  # noqa: E402

domain_spec = DomainSpec(
    name="Pasto10",
    tools=get_tools,
    instructions=get_instructions,
)
