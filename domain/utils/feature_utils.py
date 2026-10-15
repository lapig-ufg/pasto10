from domain.schemas.feature import RegisteredFeatures


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
