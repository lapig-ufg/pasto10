"""Tests of the registration state machine (domain/agent.py): tools and
instructions per ``registration_state`` (default / pending / final)."""

import pytest
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_agent_config

from domain import domain_spec
from domain.agent import get_instructions, get_tools
from domain.services.geospatial.geometry import build_buffered_area
from domain.utils.feature_utils import set_registered_features
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures

_agent_config = get_agent_config("single_agent")
OUTPUT_CONTRACT = _agent_config["output_contract"].strip()


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _tool_names(run_context) -> set[str]:
    return {tool.name for tool in get_tools(run_context)}


def _candidate():
    return build_buffered_area(-15.5, -47.5, 200)


def test_domain_spec_wires_dynamic_callables():
    assert domain_spec.name == "Pasto10"
    assert domain_spec.tools is get_tools
    assert domain_spec.instructions is get_instructions


@pytest.mark.parametrize(
    "registration_state, expected",
    [
        (None, {"start_buffer_registration_by_coordinate"}),
        ("pending", {"confirm_registration", "cancel_registration"}),
        ("final", {"complete_registration", "cancel_registration"}),
    ],
)
def test_tools_per_state(registration_state, expected):
    run_context = _context(registration_state=registration_state)

    assert _tool_names(run_context) == expected


@pytest.mark.parametrize("registration_state", ["pending", "final"])
def test_pending_and_final_instructions_describe_candidate(registration_state):
    candidate = _candidate()
    run_context = _context(
        registration_state=registration_state,
        candidate_properties=[candidate.model_dump()],
    )

    instructions = get_instructions(run_context)

    assert str(candidate) in instructions
    assert "buffer area" in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)


def test_final_instructions_without_candidate_use_fallback():
    run_context = _context(registration_state="final", candidate_properties=None)

    instructions = get_instructions(run_context)

    assert _agent_config["final_candidate_fallback"].strip() in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)


def test_default_instructions_without_registrations():
    instructions = get_instructions(_context())

    assert _agent_config["registrations_empty"].strip() in instructions
    assert _agent_config["instructions_default"].strip() in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)


def test_default_instructions_list_registered_areas():
    feature = _candidate()
    feature.metadata.append(FeatureMetadata(key="name", value="North Paddock"))
    session_state = {}
    set_registered_features(session_state, RegisteredFeatures(features=[feature]))

    instructions = get_instructions(_context(**session_state))

    assert "<registrations>" in instructions
    assert "North Paddock" in instructions
    assert feature.feature_id in instructions
    assert _agent_config["registrations_empty"].strip() not in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)
