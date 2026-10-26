"""Tests that keep the agent answering in the user's language (English by default).

The framework renders the user persona in Portuguese, which made the agent reply in Portuguese to English questions;
the domain renders it in English instead and every state ends with an explicit language rule.
"""

import re
from pathlib import Path

import pytest
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_agent_config

from domain.agent import _persona_text, get_instructions

PROMPTS = Path(__file__).resolve().parents[1] / "domain" / "prompts"
_agent_config = get_agent_config("single_agent")
PORTUGUESE_WORDS = re.compile(r"\b(você|voce|não|nao|propriedade|fazenda|pastagem|obrigado|olá|ainda|profissão|regionalidade)\b", re.IGNORECASE)
ALLOWED_ACCENTED = ("Goiás", "Técnico", "Aysén", "São", "Pingüino")


def _context(**state) -> StateContext:
    return StateContext(session_state=state)


def test_persona_is_rendered_in_english_even_with_the_framework_portuguese_defaults():
    text = _persona_text({"user_persona": {
        "name": "Ainda não conhecido", "role": "Produtor", "regionality": "Goiás",
        "communication_preferences": [{"key": "tom_de_voz", "description": "Prefers short answers"}],
    }})

    assert "Name: not known yet" in text and "Role: rancher / producer" in text and "Region: Goiás" in text
    assert "- tom de voz: Prefers short answers" in text
    assert not PORTUGUESE_WORDS.search(text.replace("Goiás", ""))


def test_persona_without_data_uses_the_english_fallback():
    assert _persona_text({}) == _agent_config["persona_fallback"].strip()


def test_persona_also_hides_the_current_framework_sentinel():
    text = _persona_text({"user_persona": {"name": "Not yet known", "role": "Produtor"}})

    assert "Name: not known yet" in text and "Role: rancher / producer" in text
    assert "Not yet known" not in text


def test_technician_role_has_an_english_meaning():
    assert "technical advisor" in _persona_text({"user_persona": {"role": "Técnico"}})


@pytest.mark.parametrize("state", [None, "pending", "final", "choosing"])
def test_every_state_ends_with_the_language_rule(state):
    instructions = get_instructions(_context(registration_state=state, user_persona={"name": "Ana", "role": "Produtor"}))

    assert "LANGUAGE: always answer in the language of the user's latest message (English by default)" in instructions
    assert "decimal point" in instructions and "YYYY-MM-DD" in instructions


def test_default_instructions_contain_no_portuguese_from_the_framework():
    instructions = get_instructions(_context(user_persona={"name": "Ana", "role": "Produtor", "regionality": "Ainda não conhecido"}))

    assert "Profissão" not in instructions and "Regionalidade" not in instructions and "Ainda não conhecido" not in instructions


@pytest.mark.parametrize("path", sorted(PROMPTS.glob("*.yml")), ids=lambda p: p.name)
def test_prompt_files_are_english(path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        scrubbed = line
        for word in ALLOWED_ACCENTED:
            scrubbed = scrubbed.replace(word, "")
        assert not PORTUGUESE_WORDS.search(scrubbed), f"{path.name}:{number}: {line.strip()[:80]}"
        assert not re.search(r"[ãõçâêô]", scrubbed), f"{path.name}:{number}: {line.strip()[:80]}"


def test_dry_matter_is_never_abbreviated_in_portuguese():
    for path in PROMPTS.glob("*.yml"):
        assert not re.search(r"\bt MS\b|\bMS/ha\b", path.read_text(encoding="utf-8"))
