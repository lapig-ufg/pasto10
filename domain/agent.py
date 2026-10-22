"""Pasto10 domain — dynamic tool selection and instructions.

The domain-specific half of the single agent: the area registration state
machine (default / pending / final, driven by
``session_state["registration_state"]``) and the tools available in each
state. The neutral shell (model, persona/context helpers) lives in
``semente.agents.build_agent``.
"""

from semente import Context as RunContext
from semente.logging import log_debug
from semente.agents.build_agent import context_blocks, persona_text
from semente.configs.prompts import get_agent_config

from domain.schemas.feature import Feature
from domain.tools.property_tools import (
    cancel_registration,
    complete_registration,
    confirm_registration,
    start_buffer_registration_by_coordinate,
)
from domain.tools.biomass_tools import generate_biomass_map, get_biomass_total
from domain.tools.pasture_tools import generate_pasture_map, get_pasture_area
from domain.utils.feature_utils import get_registered_features, summarize_feature

_agent_config = get_agent_config("single_agent")


def _candidate_features(session_state) -> list[Feature]:
    """Returns the pending candidates stored in the session as Feature models."""
    return [Feature.model_validate(prop) for prop in session_state.get("candidate_properties") or []]


def get_tools(run_context: RunContext) -> list:
    """Selects the tools available to the agent for the current registration state.

    - ``"pending"``: the user must confirm or reject the candidate area
      (``confirm_registration``, ``cancel_registration``).
    - ``"final"``: the user must name the confirmed area
      (``complete_registration``, ``cancel_registration``).
    - otherwise (default): a new area can be registered and registered areas analyzed
      (``start_buffer_registration_by_coordinate``, ``generate_pasture_map``,
      ``get_pasture_area``, ``generate_biomass_map``, ``get_biomass_total``).

    Args:
        run_context (RunContext): The run context holding ``session_state``.

    Returns:
        list: The tools for this turn.
    """
    session_state = run_context.session_state or {}
    registration_state = session_state.get("registration_state")

    if registration_state == "pending":
        return [confirm_registration, cancel_registration]
    if registration_state == "final":
        return [complete_registration, cancel_registration]
    return [
        start_buffer_registration_by_coordinate,
        generate_pasture_map,
        get_pasture_area,
        generate_biomass_map,
        get_biomass_total,
    ]


def _registrations_text(session_state) -> str:
    """Builds the body of the ``<registrations>`` block of the default state.

    One line per registered feature, ``"- {name}: {summary}"``, where the
    name is the user-chosen display name (``feature_type_fallback`` when
    unnamed) and the summary comes from ``summarize_feature``. No feature
    ids, neutral type keys or type tags are included, since the model could
    echo them to the user.

    Args:
        session_state (dict): The run's session state.

    Returns:
        str: The lines of registered areas, or the ``registrations_empty``
        text when nothing is registered.
    """
    features = get_registered_features(session_state).features
    if not features:
        return _agent_config["registrations_empty"].strip()
    fallback_name = _agent_config["feature_type_fallback"].strip()
    return "\n".join(f"- {feature.name or fallback_name}: {summarize_feature(feature)}" for feature in features)


def _feature_type_text(candidate_properties: list[Feature]) -> str:
    """Returns the display name of the pending candidates' feature type.

    The neutral ``feature_type`` key of the first candidate is mapped to a
    display name via ``feature_type_names``; when no candidate exists (or the
    type is unknown), ``feature_type_fallback`` is used instead.
    """
    feature_type_names = _agent_config["feature_type_names"]
    if candidate_properties:
        feature_type = candidate_properties[0].feature_type
        if feature_type in feature_type_names:
            return str(feature_type_names[feature_type]).strip()
    return _agent_config["feature_type_fallback"].strip()


def get_instructions(run_context: RunContext) -> str:
    """Builds the agent instructions for the current registration state.

    - ``"pending"``: context blocks + ``instructions_pending_single`` with the
      candidate summary (``candidate_summary`` template, see
      ``summarize_feature``) and its feature type name.
    - ``"final"``: context blocks + ``instructions_final`` (candidate
      summary, or ``final_candidate_fallback`` when missing).
    - otherwise: context blocks + ``<user-persona>`` + ``<registrations>``
      (registered areas or ``registrations_empty``) + ``instructions_default``.

    Every state ends with the ``output_contract`` text. All texts come from
    ``domain/prompts/agents.yml`` (``single_agent``).

    Args:
        run_context (RunContext): The run context holding ``session_state``.

    Returns:
        str: The full instruction text.
    """
    log_debug("GET INSTRUCTIONS (single_agent)")
    session_state = run_context.session_state or {}
    registration_state = session_state.get("registration_state")
    ctx_blocks = context_blocks(session_state)

    if registration_state == "pending":
        candidates = _candidate_features(session_state)
        candidate_text = summarize_feature(candidates[0]) if candidates else _agent_config["final_candidate_fallback"].strip()
        state_text = _agent_config["instructions_pending_single"].strip().format(
            candidate_text=candidate_text, feature_type=_feature_type_text(candidates),
        )
        instructions = f"{ctx_blocks}\n\n{state_text}".strip()

    elif registration_state == "final":
        candidates = _candidate_features(session_state)
        candidate_text = summarize_feature(candidates[0]) if candidates else _agent_config["final_candidate_fallback"].strip()
        state_text = _agent_config["instructions_final"].strip().format(
            candidate_text=candidate_text, feature_type=_feature_type_text(candidates),
        )
        instructions = f"{ctx_blocks}\n\n{state_text}".strip()

    else:
        persona = persona_text(session_state)
        registrations_text = _registrations_text(session_state)
        instructions = "\n\n".join(
            part for part in (
                ctx_blocks,
                f"<user-persona>\n{persona}\n</user-persona>",
                f"<registrations>\n{registrations_text}\n</registrations>",
                _agent_config["instructions_default"].strip(),
            ) if part
        )

    return instructions + "\n\n" + _agent_config["output_contract"].strip()
