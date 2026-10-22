"""Tests of the animal-unit skill (domain/skills/livestock/ua-calculator) and its wiring.

``load_skills`` swallows errors silently, so these tests assert that the skill
really is loaded by the domain spec.
"""

import re
from pathlib import Path

import pytest
from semente import Calculator
from semente.backends.toolkit import StateContext

from domain import domain_spec
from domain.agent import get_tools

SKILLS_DIR = Path(__file__).resolve().parents[1] / "domain" / "skills"
FORBIDDEN = re.compile(r"lapig|nelore|brasil|brazil|embrapa", re.IGNORECASE)


def _skill():
    return domain_spec.skills._skills.get("ua-calculator")


def test_domain_spec_loads_the_ua_calculator_skill():
    assert domain_spec.skills is not None
    skill = _skill()
    assert skill is not None and skill.description and skill.instructions
    assert Path(skill.source_path).is_absolute()


def test_skill_text_has_the_formulas_and_the_period_rule():
    text = _skill().instructions

    for expected in (
        "450 kg", "total live weight (kg) / 450", "total AU / pasture area (ha)", "2.5%", "365 days",
        "8.2 t DM/AU/year", "annual forage (t DM/year) / 8.2", "365 / days covered", "NEVER multiply a month by 12",
        "get_biomass_total", "one binary operation per call", "order of magnitude",
    ):
        assert expected in text, expected


def test_skill_has_no_country_or_institution_specific_content():
    for path in SKILLS_DIR.rglob("*"):
        if path.is_file():
            assert not FORBIDDEN.search(path.read_text(encoding="utf-8")), path


def test_default_state_has_the_calculator_but_pending_and_final_do_not():
    def has_calculator(state):
        return any(isinstance(tool, Calculator) for tool in get_tools(StateContext(session_state=state)))

    assert has_calculator({})
    assert not has_calculator({"registration_state": "pending"})
    assert not has_calculator({"registration_state": "final"})


def test_calculator_excludes_the_unneeded_functions():
    calculator = next(tool for tool in get_tools(StateContext(session_state={})) if isinstance(tool, Calculator))

    assert "is_prime" not in calculator.functions and "factorial" not in calculator.functions
    assert {"add", "subtract", "multiply", "divide"} <= set(calculator.functions)
