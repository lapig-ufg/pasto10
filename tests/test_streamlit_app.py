"""Tests of the Pasto10 Streamlit entrypoint with ``streamlit.testing.v1.AppTest``.

No LLM, Earth Engine or network: the Semente script is executed up to the chat
input and the session store is a throwaway SQLite database.
"""

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from app.test_properties import load_test_properties

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "app" / "streamlit_app.py")
TIMEOUT = 60


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Runs the app from a temp dir (users_db.json) with SQLite and the project manifest."""
    monkeypatch.setenv("SEMENTE_MANIFEST", str(ROOT / "semente.yaml"))
    monkeypatch.setenv("DATABASE_TYPE", "sqlite")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.chdir(tmp_path)


def _labels(elements):
    return [element.label for element in elements]


def test_login_screen_is_english_and_loads_without_exception():
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    assert not at.exception
    assert _labels(at.button) == ["Create", "Continue anonymously"]
    assert [t.value for t in at.title] == ["Sign in"]


def test_login_lists_stored_users_by_nickname(tmp_path):
    (tmp_path / "users_db.json").write_text(json.dumps([{"id": "u1", "nickname": "Maria"}]))
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    assert at.selectbox[0].options == ["Maria"]


def _logged_in():
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.session_state["logged_in"] = True
    at.session_state["session_id"] = f"test-{uuid.uuid4()}"
    at.session_state["user_name"] = "Tester"
    return at.run()


def test_chat_screen_defaults():
    at = _logged_in()
    assert not at.exception
    assert at.session_state["debug_mode_enabled"] is False
    assert "Enviar Localizacao da Propriedade" not in _labels(at.button)
    assert "Use this property" in _labels(at.button)
    assert not at.sidebar.error
    assert "Sign out / Switch user" in _labels(at.button)
    assert at.chat_input[0].placeholder == "Ask about pastures..."
    assert [t.value for t in at.title] == ["Hello, Tester", "Settings"]
    assert _labels(at.toggle) == ["Debug Mode"]


def test_picker_lists_the_15_properties():
    at = _logged_in()
    picker = next(s for s in at.selectbox if s.key == "pasto10_test_property")
    assert len(picker.options) == 15 == len(load_test_properties())


def _click_property(property_id):
    """Logs in, selects a test property, clicks "Use this property" and returns (at, session_id, feature)."""
    at = _logged_in()
    session_id = at.session_state["session_id"]
    next(s for s in at.selectbox if s.key == "pasto10_test_property").select(property_id)
    next(b for b in at.button if b.key == "pasto10_use_property").click()
    at.run()
    assert not at.exception

    from semente.database.session_store import store

    state = store.get(session_id, session_id)["session_state"]
    assert state["terms_accepted"] is True
    assert state["user_persona"]["name"] and state["user_persona"]["role"]
    assert state["registration_state"] is None
    features = state["all_properties"]["features"]
    assert len(features) == 1
    meta = {m["key"]: m["value"] for m in features[0]["metadata"]}
    return at, meta


@pytest.mark.parametrize("property_id", ["goias", "city_control", "desert_control"])
def test_use_property_registers_a_buffer_circle(property_id):
    at, meta = _click_property(property_id)
    item = next(p for p in load_test_properties() if p["id"] == property_id)
    assert meta["center"] and meta["radius"] == item["radius_m"]
    assert meta["name"] == item["name"]
    assert not {"origin", "registry_id", "source"} & set(meta)
    text = at.success[0].value
    assert item["name"] in text and "circular area" in text and " ha" in text


def test_use_property_twice_does_not_duplicate():
    at = _logged_in()
    button = next(b for b in at.button if b.key == "pasto10_use_property")
    button.click()
    at.run()
    next(b for b in at.button if b.key == "pasto10_use_property").click()
    at.run()
    assert at.info and "already registered" in at.info[0].value


def test_use_property_falls_back_to_a_chat_prompt(monkeypatch):
    class FakeWorkflow:
        def run(self, **kwargs):
            self.input = kwargs["input"]
            return SimpleNamespace(content="ok", images=[], videos=None, audio=None, files=None)

        def get_session_state(self, **kwargs):
            return {}

    fake = FakeWorkflow()

    def boom(item, session_id):
        raise RuntimeError("store down")

    monkeypatch.setattr("app.test_properties.register_test_property", boom)
    monkeypatch.setattr("semente.core.harness.get_harness", lambda *a, **k: fake)
    at = _logged_in()
    next(b for b in at.button if b.key == "pasto10_use_property").click()
    at.run()
    assert not at.exception
    assert fake.input.startswith("Register my test property 'Goiás'")
    assert "_pending_prompt" not in at.session_state


def test_generic_error_answer_is_shown_in_english(monkeypatch):
    class NoContentWorkflow:
        def run(self, **kwargs):
            return SimpleNamespace(images=[], videos=None, audio=None, files=None)

    monkeypatch.setattr("semente.core.harness.get_harness", lambda *a, **k: NoContentWorkflow())
    at = _logged_in()
    at.chat_input[0].set_value("hello").run()
    assert not at.exception
    assert [m["content"] for m in at.session_state["messages"]][-1] == "Error"
    assert any("Sorry, I could not produce an answer" in m.value for m in at.markdown)


def test_translate_handles_labels_prefixes_and_non_strings():
    from app.frontend import translate

    assert translate("Log In Anonymously") == "Continue anonymously"
    assert translate("Type your message...") == "Ask about pastures..."
    assert translate("Error") == "Sorry, I could not produce an answer. Please try again."
    assert translate("Download boletim.pdf") == "Download boletim.pdf"
    assert translate("**User:** Anonymous Visitor") == "**User:** Anonymous visitor"
    assert translate("unknown text") == "unknown text"
    assert translate(None) is None


def test_picker_labels_do_not_repeat_the_country():
    at = _logged_in()
    picker = next(s for s in at.selectbox if s.key == "pasto10_test_property")
    labels = list(picker.options)
    assert "Kenya" in labels and "Kenya \u2014 Kenya" not in labels
    assert "Pampas \u2014 Uruguay" in labels


def test_registration_message_persists_until_the_selector_changes():
    at = _logged_in()
    next(b for b in at.button if b.key == "pasto10_use_property").click()
    at.run()
    assert "circular area" in at.success[0].value
    at.run()
    assert at.success and "circular area" in at.success[0].value
    next(s for s in at.selectbox if s.key == "pasto10_test_property").select("kansas")
    at.run()
    assert not at.success


def test_test_tools_are_off_without_an_explicit_development_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("PASTO10_TEST_PICKER", raising=False)
    at = _logged_in()
    assert not at.exception
    assert "Use this property" not in _labels(at.button)
    assert not at.toggle
    assert at.session_state["debug_mode_enabled"] is False

    from app.test_properties import register_test_property

    with pytest.raises(PermissionError):
        register_test_property(load_test_properties()[0], "any-session")


def test_picker_can_be_opted_into_outside_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("PASTO10_TEST_PICKER", "1")
    at = _logged_in()
    assert "Use this property" in _labels(at.button)
    assert not at.toggle


def test_agent_turn_uses_the_session_id_as_user_id(monkeypatch):
    calls = []

    class Recorder:
        def run(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="ok", images=[], videos=None, audio=None, files=None)

        def get_session_state(self, **kwargs):
            return {}

    monkeypatch.setattr("semente.core.harness.get_harness", lambda *a, **k: Recorder())
    at = _logged_in()
    at.chat_input[0].set_value("hello").run()
    assert calls[0]["user_id"] == calls[0]["session_id"] == at.session_state["session_id"]


def test_chat_input_returns_the_typed_text_and_the_pending_prompt_once(monkeypatch):
    import streamlit as st

    from app.frontend import _chat_input

    state = {}
    monkeypatch.setattr(st, "session_state", state)
    assert _chat_input(lambda *args, **kwargs: "register -15.5, -47.5")("Ask") == "register -15.5, -47.5"
    assert _chat_input(lambda *args, **kwargs: None)("Ask") is None
    state["_pending_prompt"] = "hello"
    assert _chat_input(lambda *args, **kwargs: None)("Ask") == "hello"
    assert "_pending_prompt" not in state


def test_front_end_has_no_geojson_upload():
    from app import frontend

    assert not hasattr(frontend, "_file_uploader") and not hasattr(frontend, "_inspect_geojson")


def test_session_locks_are_per_session_and_do_not_block_each_other():
    import threading

    from app.test_properties import session_lock

    assert session_lock("a") is session_lock("a")
    assert session_lock("a") is not session_lock("b")
    result = []
    with session_lock("a"):
        thread = threading.Thread(target=lambda: result.append(session_lock("b").acquire(timeout=2)))
        thread.start()
        thread.join()
    assert result == [True]


def _semente_webapp_source():
    import semente.interfaces.streamlit as package

    return (Path(package.__file__).parent / "streamlit_webapp.py").read_text(encoding="utf-8")


def test_translated_labels_still_exist_in_the_installed_semente_webapp():
    from app.frontend import _HIDDEN_BUTTONS, LABELS, PREFIXES, SUBSTRINGS

    source = _semente_webapp_source()
    missing = [text for text in [*LABELS, *_HIDDEN_BUTTONS] if f'"{text}"' not in source]
    missing += [old for old, _ in [*PREFIXES, *SUBSTRINGS] if old.split()[0] not in source]
    assert not missing, f"Semente changed these UI strings; update app/frontend.py: {missing}"


def test_login_user_selectbox_still_stores_nickname_in_semente():
    source = _semente_webapp_source()
    assert '"nickname"' in source and "format_func" in source


def test_uv_lock_pins_semente_to_a_git_commit():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'source = \{ git = "https://github\.com/lapig-ufg/semente\.git#[0-9a-f]{40}" \}', lock)
