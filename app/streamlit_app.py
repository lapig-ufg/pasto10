"""Pasto10 Streamlit entrypoint.

Run with ``streamlit run app/streamlit_app.py`` (see ``scripts/run_local.sh``). It
reuses the Semente chat unchanged (see ``app.frontend`` for how it is adapted) and
adds a sidebar picker that registers any of the 15 test properties in one click.
"""

import os
import runpy

import streamlit as st

import semente.interfaces.streamlit as semente_streamlit
from app import frontend

frontend.configure_page()
frontend.install()

from app.test_properties import (  # noqa: E402
    fallback_prompt,
    load_test_properties,
    picker_enabled,
    register_test_property,
)

_WEBAPP = os.path.join(os.path.dirname(semente_streamlit.__file__), "streamlit_webapp.py")
_MESSAGE_KEY = "_pasto10_register_message"


def _option_label(item: dict) -> str:
    """Selector label: ``Name - Country``, or just the name when it already is the country."""
    return item["name"] if item["name"] == item["country"] else f"{item['name']} \u2014 {item['country']}"


def _register_clicked(item: dict) -> None:
    """Registers the selected property and stores the feedback message for the picker.

    Side effects:
        Writes the session through ``register_test_property`` and keeps
        ``st.session_state["_pasto10_register_message"] = (property id, kind, text)`` so the
        message survives the reruns until the selector changes or the button is clicked again.
        On failure it queues ``st.session_state["_pending_prompt"]`` (a chat message asking
        the agent to register it) and shows a toast.
    """
    try:
        result = register_test_property(item, st.session_state["session_id"])
    except Exception as exc:
        st.toast(f"Could not register it directly ({exc}); asking the assistant instead.")
        st.session_state[_MESSAGE_KEY] = (item["id"], "info", "Asked the assistant to register it.")
        st.session_state["_pending_prompt"] = fallback_prompt(item)
        return
    if result is None:
        st.session_state[_MESSAGE_KEY] = (item["id"], "info", f"'{item['name']}' is already registered in this session.")
        return
    tail = "synthetic demo parcel." if result["demo"] else "no registry parcel here, using an approximate circle."
    text = f"Registered {result['name']} ({result['area_ha']:,.1f} ha) - {tail}"
    st.toast(text)
    st.session_state[_MESSAGE_KEY] = (item["id"], "success", text + " Ask for its pasture map or biomass.")


def render_test_property_picker() -> None:
    """Renders the test-property picker in the sidebar (logged-in users, test picker enabled only).

    The description sits in a fixed-height container so the button never moves. The
    "Use this property" button registers the selected property directly in the user's
    persisted session; see ``_register_clicked``.
    """
    properties = {item["id"]: item for item in load_test_properties()}
    with st.sidebar:
        st.subheader("Test properties")
        selected_id = st.selectbox(
            "Choose a test property:",
            list(properties),
            format_func=lambda key: _option_label(properties[key]),
            key="pasto10_test_property",
        )
        item = properties[selected_id]
        with st.container(height=150, border=False):
            st.caption(f"{item['profile']}  \n{item['latitude']}, {item['longitude']} - radius {item['radius_m']} m")
        if st.button("Use this property", key="pasto10_use_property"):
            _register_clicked(item)
        message = st.session_state.get(_MESSAGE_KEY)
        if message and message[0] == selected_id:
            getattr(st, message[1])(message[2])
        st.divider()


if st.session_state.get("logged_in") and picker_enabled():
    render_test_property_picker()

runpy.run_path(_WEBAPP, run_name="__main__")
