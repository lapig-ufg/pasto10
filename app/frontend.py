"""Adapts the Semente Streamlit chat to Pasto10 without modifying Semente.

The Semente web app is a monolithic script with no stable extension points, so
Pasto10 runs it unchanged and wraps the ``st.*`` functions it calls. ``install``
is idempotent: Streamlit re-executes the entrypoint on every rerun, but this
module (and the wrapped functions) lives for the whole process.

Semente ships an English UI since 0.4.0, so the wrappers now only customise the
few strings that Pasto10 words differently.

Wrapped behaviours:
    - Pasto10 wording for the chat labels, titles and error placeholders.
    - ``st.chat_input`` returns ``st.session_state["_pending_prompt"]`` once, which lets
      other parts of the front end inject a message into the conversation.
    - ``st.selectbox`` over stored users shows the ``nickname`` that Semente saves
      (Semente reads a key that does not exist and lists everybody as "User").
    - The "Debug Mode" toggle is shown only with ``APP_ENV=development`` set explicitly.
    - ``Harness.run`` holds the per-session lock, so direct registrations never race an agent turn of
      the same session.
    - Generic "Error" answers are replaced by a friendlier English message.
"""

import streamlit as st

from app.test_properties import is_development, session_lock

_FLAG = "_pasto10_installed"
_set_page_config = st.set_page_config
# Semente 0.4 removed the fixed-coordinates location button; kept empty so a
# future release can be hidden again without touching the install loop.
_HIDDEN_BUTTONS: set[str] = set()
_MEDIA_TYPES = {"png", "jpg", "jpeg", "webp", "wav", "mp3", "mp4"}

# The Portuguese upload key is the exact label of Semente 0.4.1 (lookup key only; the output is English).
LABELS = {
    "Login": "Sign in",
    "Log In": "Sign in",
    "Empty": "No saved users",
    "🆕 New": "New user",
    "User identification": "Your name or nickname",
    "Log In Anonymously": "Continue anonymously",
    "Log Out / Switch User": "Sign out / Switch user",
    "Envie imagens/áudio/mapas (png, jpg, mp3, zip, kmz, kml, geojson, etc)": "Upload images or audio",
    "Type your message...": "Ask about pastures...",
    "Analyzing data and generating response...": "Analyzing data and generating the answer...",
    "Error": "Sorry, I could not produce an answer. Please try again.",
}
SUBSTRINGS = (
    ("Anonymous Visitor", "Anonymous visitor"),
)
PREFIXES = ()


def translate(text):
    """Translates a Semente UI string to the Pasto10 English wording.

    Args:
        text: The original string (non-strings are returned untouched).

    Returns:
        The translated string: exact match in ``LABELS``, else the first matching
        prefix in ``PREFIXES``, then the substrings in ``SUBSTRINGS``.
    """
    if not isinstance(text, str):
        return text
    if text in LABELS:
        return LABELS[text]
    for old, new in PREFIXES:
        if text.startswith(old):
            text = new + text[len(old):]
            break
    for old, new in SUBSTRINGS:
        text = text.replace(old, new)
    return text


def _translating(original, *, kwargs_to_translate=("label", "body", "help", "placeholder", "text")):
    """Wraps an ``st`` function so its first positional text and label-like kwargs are translated."""

    def wrapper(*args, **kwargs):
        if args:
            args = (translate(args[0]),) + args[1:]
        for key in kwargs_to_translate:
            if key in kwargs:
                kwargs[key] = translate(kwargs[key])
        return original(*args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _button(original):
    """Wraps ``st.button``/``st.download_button``: translates labels and hides unwanted buttons."""
    translating = _translating(original)

    def wrapper(label, *args, **kwargs):
        if label in _HIDDEN_BUTTONS:
            return False
        return translating(label, *args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _selectbox(original):
    """Wraps ``st.selectbox``: translates labels and fixes the stored-user display name."""
    translating = _translating(original)

    def wrapper(label, options, *args, **kwargs):
        options = list(options)
        if options and isinstance(options[0], dict) and "nickname" in options[0]:
            kwargs["format_func"] = lambda user: user.get("nickname") or "User"
        return translating(label, options, *args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _media_uploader(original):
    """Wraps ``st.file_uploader``: translates the label and keeps only image and audio types.

    Semente 0.4.1 also accepts map files (zip, kmz, kml, geojson); Pasto10 registers areas
    from coordinates only, so the accepted types are narrowed back to media.
    """
    translating = _translating(original)

    def wrapper(*args, **kwargs):
        if "type" in kwargs:
            kwargs["type"] = [ext for ext in kwargs["type"] if ext in _MEDIA_TYPES]
        return translating(*args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _chat_input(original):
    """Wraps ``st.chat_input``: translates the placeholder and injects pending text.

    Returns:
        The injected ``st.session_state["_pending_prompt"]`` (consumed once) when set,
        otherwise what the user typed.
    """
    translating = _translating(original)

    def wrapper(*args, **kwargs):
        typed = translating(*args, **kwargs)
        pending = st.session_state.pop("_pending_prompt", None)
        if pending:
            return pending
        return typed

    wrapper.__wrapped__ = original
    return wrapper


def _toggle(original):
    """Wraps ``st.toggle``: the "Debug Mode" toggle exists only with ``APP_ENV=development`` set explicitly.

    Returns:
        False without rendering when the toggle is hidden, so Semente never renders its debug panel.
    """
    translating = _translating(original)

    def wrapper(label, *args, **kwargs):
        if label == "Debug Mode" and not is_development():
            return False
        return translating(label, *args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _locked_run(original):
    """Wraps ``Harness.run``: holds the per-session lock for the whole turn.

    ``Harness.run`` is synchronous (it returns the final output; ``stream=True`` is ignored by
    Semente), so holding the lock for the call covers the whole agent turn.

    Side effects:
        Holds ``session_lock(session_id)`` during the call.
    """

    def wrapper(self, *args, **kwargs):
        session_id = kwargs.get("session_id")
        with session_lock(session_id):
            return original(self, *args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def _empty(original):
    """Wraps ``st.empty`` so the placeholder shows the friendly generic-error answer."""

    def wrapper(*args, **kwargs):
        placeholder = original(*args, **kwargs)
        markdown = placeholder.markdown
        placeholder.markdown = lambda body, *a, **kw: markdown(
            translate(body) if body in ("Error", "Erro") else body, *a, **kw
        )
        return placeholder

    wrapper.__wrapped__ = original
    return wrapper


def _markdown(original):
    """Wraps ``st.markdown`` so stored generic-error and error-prefix messages keep the Pasto10 wording."""

    def wrapper(body, *args, **kwargs):
        if isinstance(body, str) and (
            body in ("Error", "Erro")
            or body.startswith(("Desculpe, ocorreu um erro: ", "Sorry, an error occurred: "))
        ):
            body = translate(body)
        return original(body, *args, **kwargs)

    wrapper.__wrapped__ = original
    return wrapper


def configure_page():
    """Sets the Pasto10 page config and turns later ``st.set_page_config`` calls into no-ops.

    Semente's script calls ``st.set_page_config(page_title="Semente")`` at import, which
    would raise (the config must come first and only once). Must be the first
    Streamlit call of the entrypoint; works on every rerun because the original
    function is kept at module level.
    """
    _set_page_config(page_title="Pasto10", page_icon="🌾")
    st.set_page_config = lambda *args, **kwargs: None


def install():
    """Installs all the wrappers on the ``streamlit`` module (idempotent).

    Side effects:
        Replaces attributes of the ``streamlit`` module and of ``st.sidebar``;
        wraps ``Harness.run``; sets ``debug_mode_enabled`` to False in the session (once) so the debug
        panel starts closed.
    """
    st.session_state.setdefault("debug_mode_enabled", False)
    if getattr(st, _FLAG, False):
        return
    for name in ("title", "subheader", "caption", "warning", "info", "text_input", "audio_input", "spinner", "success", "error"):
        setattr(st, name, _translating(getattr(st, name)))
    st.write = _translating(st.write)
    st.toggle = _toggle(st.toggle)
    st.button = _button(st.button)
    st.download_button = _button(st.download_button)
    st.selectbox = _selectbox(st.selectbox)
    st.file_uploader = _media_uploader(st.file_uploader)
    st.chat_input = _chat_input(st.chat_input)
    st.empty = _empty(st.empty)
    st.markdown = _markdown(st.markdown)
    st.sidebar.title = _translating(st.sidebar.title)
    from semente.core.harness import Harness

    Harness.run = _locked_run(Harness.run)
    setattr(st, _FLAG, True)
