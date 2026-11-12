"""Feature utilities — session_state persistence and user-facing summaries.

``all_properties`` in ``session_state`` holds the serialized
``RegisteredFeatures``; read and write it only through
``get_registered_features`` / ``set_registered_features``.
``summarize_feature`` renders the ``single_agent.candidate_summary`` template
of ``domain/prompts/agents.yml`` (no internal ids or type keys).
"""

from semente.configs.prompts import get_agent_config

from domain.schemas.feature import Feature, RegisteredFeatures


def get_registered_features(session_state) -> RegisteredFeatures:
    """Reads the registered features from the session state.

    ``all_properties`` stores the serialized form of a RegisteredFeatures
    model (plain dict) so the engine can persist the session as JSON; this
    accessor rebuilds the typed model on every read.

    Args:
        session_state (dict): The run's session state.

    Returns:
        RegisteredFeatures: The registered features (empty when none).
    """
    value = session_state.get("all_properties")
    if value is None:
        return RegisteredFeatures()
    return RegisteredFeatures.model_validate(value)


def set_registered_features(session_state, registered: RegisteredFeatures) -> None:
    """Writes the registered features back to the session state.

    Args:
        session_state (dict): The run's session state.
        registered (RegisteredFeatures): The full set of registered features.

    Side effects:
        Overwrites ``session_state["all_properties"]`` with the serialized
        (plain dict) form of ``registered``.
    """
    session_state["all_properties"] = registered.model_dump()


def _safe_summary(feature: Feature, config: dict) -> str:
    """Builds a minimal summary without internal ids or type keys.

    Uses the display name from ``feature_type_names`` (or
    ``feature_type_fallback``) plus the total area in hectares, when known.

    Args:
        feature (Feature): The feature to summarize.
        config (dict): The ``single_agent`` prompt config (may be empty).

    Returns:
        str: E.g. ``"circular area · 28.27 ha"``.
    """
    type_names = config.get("feature_type_names") or {}
    display_name = type_names.get(feature.feature_type) or config.get("feature_type_fallback") or "area"
    display_name = str(display_name).strip()
    if feature.total_area is None:
        return display_name
    return f"{display_name} · {feature.total_area} ha"


def summarize_feature(feature: Feature) -> str:
    """Builds the user-facing one-line summary of a buffer area.

    Fills ``single_agent.candidate_summary`` (``{latitude}``, ``{longitude}``,
    ``{radius}``, ``{area}``) from the ``center`` and ``radius`` metadata and
    the total area. Used for the pending/final instructions and the tool
    results, so the model never sees internal ids or type keys there.

    Args:
        feature (Feature): The feature to summarize.

    Returns:
        str: The summary. When the feature lacks a parseable ``center`` or a
        ``radius`` (non-buffer feature types), has no ``total_area``, or the
        ``candidate_summary`` template is missing or cannot be filled
        (unknown placeholder or malformed braces), a safe fallback (type
        display name + area in ha, when known) is returned instead; it never
        contains the feature id or the neutral type key and never raises
        on template errors.
    """
    config = get_agent_config("single_agent") or {}
    center = feature.get_metadata("center")
    radius = feature.get_metadata("radius")
    template = config.get("candidate_summary")
    if center is None or radius is None or feature.total_area is None or not template:
        return _safe_summary(feature, config)
    try:
        latitude, longitude = (part.strip() for part in str(center).split(","))
        return str(template).strip().format(
            latitude=latitude, longitude=longitude, radius=radius, area=feature.total_area,
        )
    except (KeyError, ValueError, IndexError):
        return _safe_summary(feature, config)


def find_registered_feature(session_state, area_name: str | None = None) -> Feature | None:
    """Finds a registered feature by its display name.

    Matching is case-insensitive and ignores surrounding spaces: an exact
    name wins; otherwise a name that is contained in exactly one registered
    name matches. Without a name the most recently registered feature is
    returned. Read-only.

    Args:
        session_state (dict): The run's session state.
        area_name (str | None): Display name chosen by the user, or None for
            the latest registered area.

    Returns:
        Feature | None: The matching feature, or None when nothing is
        registered or no (unique) registered area matches the name.
    """
    features = get_registered_features(session_state).features
    if not features:
        return None
    wanted = (area_name or "").strip().casefold()
    if not wanted:
        return features[-1]

    exact = [feature for feature in features if (feature.name or "").strip().casefold() == wanted]
    if exact:
        return exact[-1]
    partial = [feature for feature in features if wanted in (feature.name or "").casefold()]
    return partial[0] if len(partial) == 1 else None
