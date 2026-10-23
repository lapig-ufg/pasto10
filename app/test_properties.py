"""Fixed test properties and one-click registration for the Streamlit sidebar.

The 15 properties live in ``domain/data/test_properties.json`` (shared with the
end-to-end tests through ``tests/fixtures/global_properties.py``). Registering
one writes the same session state the registration tools would produce, so the
agent sees the area in its next message.
"""

import json
import os
import threading
from pathlib import Path

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def session_lock(session_id: str) -> threading.RLock:
    """Returns the lock of one session (created on first use, under a short global guard).

    Held while a direct registration or an uploaded GeoJSON writes the session store and while
    the workflow runs that session's turn (see ``app.frontend``), so those never interleave for
    the same session while different sessions never block each other. Locks are kept for the life
    of the process (one small object per session): this front end is a demo meant for a few
    users in one Streamlit process; several processes on one database are not protected.

    Args:
        session_id (str): The Streamlit session id (also the user id).

    Returns:
        threading.RLock: The reentrant lock of that session.
    """
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(session_id, threading.RLock())


_DATA_FILE = Path(__file__).resolve().parents[1] / "domain" / "data" / "test_properties.json"


def load_test_properties() -> list[dict]:
    """Loads the fixed test properties.

    Returns:
        list[dict]: One dict per property (``id``, ``name``, ``country``, ``continent``,
        ``latitude``, ``longitude``, ``radius_m``, ``timezone``, ``profile``, ``kind``, ``expect``),
        in file order.
    """
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


def is_development() -> bool:
    """True only when ``APP_ENV`` is explicitly ``development``.

    A missing or any other value is NOT development: Semente itself defaults to
    development when the variable is absent, but the test tools of this front end
    (sidebar picker, automatic terms acceptance, debug toggle) must be opted into.
    """
    return os.getenv("APP_ENV", "").strip().lower() == "development"


def picker_enabled() -> bool:
    """True when the sidebar test picker (direct session writes, automatic terms acceptance) may be used.

    It requires ``APP_ENV=development`` explicitly, or ``PASTO10_TEST_PICKER=1`` as an
    explicit opt-in in other environments.
    """
    return is_development() or os.getenv("PASTO10_TEST_PICKER", "") == "1"


def fallback_prompt(item: dict) -> str:
    """Chat message that makes the agent register a test property by itself.

    Args:
        item (dict): A test property.

    Returns:
        str: The message used when the session cannot be written directly.
    """
    return (
        f"Register my test property '{item['name']}' at latitude {item['latitude']}, "
        f"longitude {item['longitude']} with a {item['radius_m']} m radius and confirm it."
    )


def register_test_property(item: dict, session_id: str) -> dict | None:
    """Registers a test property directly in the persisted session of a user.

    Registers the synthetic demo registry parcel that contains the point of the property
    (the first of ``DemoRegistrySource.find_by_point``, built with ``PropertyCandidate.to_feature``,
    the same feature the ``select_candidate`` flow stores). When there is no parcel (the two
    negative controls) it falls back to the buffer circle. The feature is named
    ``"<property name> - <registry id>"`` (or just the property name for the circle) and is not
    added twice.

    Args:
        item (dict): A test property (see ``load_test_properties``).
        session_id (str): The Streamlit session id. The Semente web app runs the workflow with
            ``user_id == session_id == st.session_state.session_id``, so it is also the store's user id.

    Returns:
        dict | None: ``name``, ``area_ha``, ``registry_id`` (None for the circle) and ``demo``
        (True for a registry parcel) of the registered property, or None when it was already registered.

    Raises:
        PermissionError: When the test picker is not enabled (``picker_enabled``).
        Exception: Anything raised while building the feature or reading/writing the session
        store; the caller falls back to ``fallback_prompt``.

    Side effects:
        Creates the session if needed, or updates it in the SQLite/Postgres store:
        appends to ``all_properties``, resets ``registration_state`` and
        ``candidate_properties`` (a pending registration is dropped) and sets ``terms_accepted`` so
        the onboarding step is skipped. The read-modify-write runs under the session lock.
    """
    from semente.configs.prompts import set_prompts_dir
    from semente.database.session_store import store

    set_prompts_dir(_DATA_FILE.parents[1] / "prompts")  # what the workflow does when it is built

    from domain.schemas.feature import FeatureMetadata
    from domain.services.geospatial.geometry import build_buffered_area
    from domain.services.property_sources.demo_registry_source import DemoRegistrySource
    from domain.utils.feature_utils import get_registered_features, set_registered_features

    parcels = DemoRegistrySource().find_by_point(item["latitude"], item["longitude"])
    registry_id = parcels[0].candidate_id if parcels else None
    if parcels:
        feature = parcels[0].to_feature()
        name = f"{item['name']} - {registry_id}"
    else:
        feature = build_buffered_area(item["latitude"], item["longitude"], item["radius_m"])
        name = item["name"]

    if not picker_enabled():
        raise PermissionError("The test picker is disabled (set APP_ENV=development or PASTO10_TEST_PICKER=1).")

    metadata = [m for m in feature.metadata if m.key != "name"]
    metadata.append(FeatureMetadata(key="name", value=name))
    feature = feature.model_copy(update={"metadata": metadata})

    with session_lock(session_id):
        loaded = store.get(session_id, session_id) or {"session_state": {}, "runs": []}
        state = loaded["session_state"]
        registered = get_registered_features(state)
        if any(f.name == name for f in registered.features):
            return None
        registered.features.append(feature)
        set_registered_features(state, registered)
        state["registration_state"] = None
        state["candidate_properties"] = []
        state["terms_accepted"] = True
        store.save(session_id, session_id, session_state=state, runs=loaded["runs"])
    return {"name": name, "area_ha": feature.total_area, "registry_id": registry_id, "demo": bool(parcels)}
