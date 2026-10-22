"""Tests of the registration state machine (domain/agent.py): tools and
instructions per ``registration_state`` (default / pending / final)."""

import pytest
from semente import Calculator
from semente.backends.toolkit import StateContext
from semente.configs.prompts import get_agent_config

from domain import domain_spec
from domain.agent import get_instructions, get_tools
from domain.services.geospatial.geometry import build_buffered_area
from domain.utils.feature_utils import set_registered_features, summarize_feature
from domain.schemas.feature import FeatureMetadata, RegisteredFeatures

WEATHER_TOOLS = {
    "get_monthly_precipitation_forecast", "get_daily_precipitation_forecast", "get_rain_season_onset_forecast",
    "get_dry_season_onset_forecast", "get_temperature_forecast",
}
CALCULATOR_TOOLS = {"add", "subtract", "multiply", "divide", "exponentiate", "square_root"}

_agent_config = get_agent_config("single_agent")
OUTPUT_CONTRACT = _agent_config["output_contract"].strip()


def _context(**session_state) -> StateContext:
    return StateContext(session_state=session_state)


def _tool_names(run_context) -> set[str]:
    names = set()
    for tool in get_tools(run_context):
        names.update(tool.functions if isinstance(tool, Calculator) else [tool.name])
    return names


def _candidate():
    return build_buffered_area(-15.5, -47.5, 300)


def test_domain_spec_wires_dynamic_callables():
    assert domain_spec.name == "Pasto10"
    assert domain_spec.tools is get_tools
    assert domain_spec.instructions is get_instructions


@pytest.mark.parametrize(
    "registration_state, expected",
    [
        (None, {
            "start_buffer_registration_by_coordinate", "generate_pasture_map", "get_pasture_area",
            "generate_biomass_map", "get_biomass_total", "generate_biomass_video", *WEATHER_TOOLS, "generate_area_bulletin", *CALCULATOR_TOOLS,
        }),
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

    assert "Location: -15.5, -47.5 · Radius: 300 m · Area: 28.27 ha" in instructions
    assert "circular area" in instructions
    assert "buffer area" not in instructions
    for internal in (candidate.feature_id, "buffer_area", "Feature ID"):
        assert internal not in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)


def test_feature_type_display_name_is_circular_area():
    assert _agent_config["feature_type_names"]["buffer_area"] == "circular area"


def test_pending_instructions_handle_radius_or_location_change():
    pending = _agent_config["instructions_pending_single"]

    assert "different radius" in pending and "location" in pending
    assert "call `cancel_registration`, then repeat the new value back to the user" in pending
    assert "ask them to confirm" in pending


def test_default_instructions_call_tool_directly_and_convert_dms():
    default = _agent_config["instructions_default"]

    assert "explicitly asks to register an area" in default
    assert "right away" in default
    assert "|D| + M/60 + S/3600" in default
    assert "negative for S/W (or when D is already negative)" in default
    assert "Minutes may also come as decimals (e.g. 15°30.5'S)." in default
    assert "Examples: 15°30'0\"S 47°30'0\"W -> -15.5, -47.5; -15°30'0\" -> -15.5." in default


def test_start_tool_description_converts_dms_with_absolute_degrees():
    from semente.configs.prompts import get_tool_description

    description = get_tool_description("property_tools", "start_buffer_registration_by_coordinate")

    assert "|D|" in description and "negative for S/W (or when D is already negative)" in description
    assert "Examples: 15°30'0\"S 47°30'0\"W -> -15.5, -47.5;" in description
    assert "-15°30'0\" -> -15.5." in description


def test_default_instructions_hide_feature_ids_and_type_keys():
    default = _agent_config["instructions_default"]

    assert "Never show feature IDs" in default
    assert "buffer_area" in default
    assert "refer to areas by name, location, radius and size (ha)" in default


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
    assert "- North Paddock: " in instructions
    assert summarize_feature(feature) in instructions
    assert _agent_config["registrations_empty"].strip() not in instructions
    assert instructions.endswith(OUTPUT_CONTRACT)


def _registrations_block(instructions: str) -> str:
    return instructions.split("<registrations>\n", 1)[1].split("\n</registrations>", 1)[0]


def test_default_instructions_registrations_hide_ids_and_type_keys():
    feature = _candidate()
    feature.metadata.append(FeatureMetadata(key="name", value="Pasto Norte"))
    session_state = {}
    set_registered_features(session_state, RegisteredFeatures(features=[feature]))

    instructions = get_instructions(_context(**session_state))
    block = _registrations_block(instructions)

    assert block == f"- Pasto Norte: {summarize_feature(feature)}"
    assert "Pasto Norte" in instructions
    assert "Radius: 300 m" in instructions
    assert feature.id not in instructions
    assert "Feature ID" not in instructions
    assert "<buffer_area>" not in instructions
    # instructions_default mentions "buffer_area" in its "never show" rule, so check the block itself.
    assert "buffer_area" not in block


def test_default_instructions_unnamed_registration_uses_type_fallback():
    feature = _candidate()
    session_state = {}
    set_registered_features(session_state, RegisteredFeatures(features=[feature]))

    block = _registrations_block(get_instructions(_context(**session_state)))

    assert block == f"- {_agent_config['feature_type_fallback'].strip()}: {summarize_feature(feature)}"


def test_default_instructions_describe_pasture_tools_and_guard_against_invented_figures():
    default = _agent_config["instructions_default"]

    assert "generate_pasture_map" in default and "get_pasture_area" in default
    assert "If no tool has returned data, do not present map-based figures or results." in default
    for brazil_term in ("MapBiomas", "SICAR", "CAR "):
        assert brazil_term not in default


def test_default_instructions_describe_biomass_tools_and_semantics():
    default = _agent_config["instructions_default"]

    assert "generate_biomass_map" in default and "get_biomass_total" in default
    assert "NOT standing biomass and NOT available forage" in default
    assert "Never compare annual figures with monthly ones" in default
    assert "do not present biomass figures or maps" in default
    for brazil_term in ("MapBiomas", "SICAR", "Embrapa"):
        assert brazil_term not in default


def test_biomass_rule_allows_stocking_capacity_only_through_the_skill():
    default = _agent_config["instructions_default"]

    assert "EXCEPT by following the `ua-calculator` skill" in default
    assert "never by multiplying a month by 12" in default
