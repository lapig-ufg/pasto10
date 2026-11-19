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
    - ``st.file_uploader`` also accepts geospatial files (shapefile .zip/.rar,
      KMZ, KML and GeoJSON); they are passed to Semente's input step, which
      converts them to GeoJSON for the registration tool.
    - The "Debug Mode" toggle is shown only with ``APP_ENV=development`` set explicitly.
    - ``Harness.run`` holds the per-session lock, so direct registrations never race an agent turn of
      the same session.
    - Generic "Error" answers are replaced by a friendlier English message.
"""

import json
import re

import streamlit as st

from app.test_properties import is_development, session_lock

_FLAG = "_pasto10_installed"
_set_page_config = st.set_page_config
# Semente 0.4 removed the fixed-coordinates location button; kept empty so a
# future release can be hidden again without touching the install loop.
_HIDDEN_BUTTONS: set[str] = set()
# Formats Semente's geo intake converts (shapefile .zip/.rar, KMZ, KML, GeoJSON).
_EXTRA_UPLOAD_TYPES = ["geojson", "json", "zip", "rar", "kmz", "kml"]
_GEO_EXTENSIONS = tuple(f".{extension}" for extension in _EXTRA_UPLOAD_TYPES)
_GEOFILE_KEY = "_pending_geofile"

LABELS = {
    "Login": "Sign in",
    "Log In": "Sign in",
    "Empty": "No saved users",
    "🆕 New": "New user",
    "User identification": "Your name or nickname",
    "Log In Anonymously": "Continue anonymously",
    "Log Out / Switch User": "Sign out / Switch user",
    "Upload images/audio (png, jpg, mp3, etc)": "Upload images, audio or a shapefile/KML/GeoJSON boundary",
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


def _file_uploader(original):
    """Wraps ``st.file_uploader``: accepts geospatial files and forwards them to Semente.

    Side effects:
        Stores ``{"name", "format"}`` of the first accepted geospatial upload in
        ``st.session_state["_pending_geofile"]``; the chat input wrapper adds a short
        note to the message. The accepted file stays in the returned list, so Semente's
        input step converts it (shapefile .zip/.rar, KMZ, KML or GeoJSON) and injects
        the resulting GeoJSON into the registration tool. Oversized files are rejected
        with a warning, never truncated. Semente replaces the uploader after each
        answer, which clears the pending upload.
    """
    translating = _translating(original)

    def wrapper(label, *args, **kwargs):
        from domain.services.property_sources.geofile_source import max_geofile_bytes

        types = kwargs.get("type")
        if types is not None:
            kwargs["type"] = list(types) + [t for t in _EXTRA_UPLOAD_TYPES if t not in types]
        files = translating(label, *args, **kwargs)
        kept, accepted = [], []
        for uploaded in files or []:
            if not uploaded.name.lower().endswith(_GEO_EXTENSIONS):
                kept.append(uploaded)
                continue
            raw = uploaded.getvalue()
            name = safe_filename(uploaded.name)
            if len(raw) > max_geofile_bytes():
                st.warning(
                    f"{name}: this file is too large ({len(raw):,} bytes; the limit is "
                    f"{max_geofile_bytes():,}) and was ignored."
                )
                continue
            if uploaded.name.lower().endswith((".geojson", ".json")):
                problem = _inspect_geojson(raw)
                if problem:
                    st.warning(f"{name}: {problem}")
                    continue
            if accepted:
                st.warning(f"{name}: only one geospatial file is used per message; it was ignored.")
                continue
            accepted.append({"name": name, "format": uploaded.name.rsplit(".", 1)[-1].lower()})
            kept.append(uploaded)
        if accepted:
            st.session_state[_GEOFILE_KEY] = accepted[0]
        else:
            st.session_state.pop(_GEOFILE_KEY, None)
        return kept

    wrapper.__wrapped__ = original
    return wrapper


def safe_filename(name) -> str:
    """Sanitizes an uploaded file name for the chat note and the warnings.

    Args:
        name: The name given by the browser (untrusted).

    Returns:
        str: Only ``[A-Za-z0-9._-]`` (anything else becomes ``_``), at most 60 characters,
        ``"upload.geojson"`` when nothing usable is left.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or ""))[:60].strip("._")
    return cleaned or "upload.geojson"


def _inspect_geojson(raw: bytes):
    """Validates an uploaded GeoJSON file with the cheap geofile checks.

    Reuses ``MAX_CONVERTED_BYTES``, ``raw_vertex_count`` (iterative, bounded depth, finite
    lon/lat, rings and vertex limits) and the NaN/Infinity rejection of
    ``user_polygon_source``, with the geofile vertex limit (a real property file is much
    larger than a pasted polygon). Never raises: any failure, including a ``RecursionError``
    from deeply nested JSON, becomes a problem text.

    Args:
        raw (bytes): The file content.

    Returns:
        str | None: A user-facing reason (None when the file can be sent to the assistant).
    """
    from domain.services.property_sources import geofile_source as geofiles
    from domain.services.property_sources import user_polygon_source as source

    try:
        if len(raw) > geofiles.MAX_CONVERTED_BYTES:
            return f"this file is too large ({len(raw):,} bytes; the limit is {geofiles.MAX_CONVERTED_BYTES:,}) and was ignored."
        document = json.loads(raw.decode("utf-8"), parse_constant=source._reject_constant)
        geometries = source._geometries(document) if isinstance(document, dict) else []
        vertices = source.raw_vertex_count(geometries, geofiles.max_geofile_vertices())
        if vertices > geofiles.max_geofile_vertices():
            return f"this polygon has too many vertices (the limit is {geofiles.max_geofile_vertices():,}) and was ignored. Simplify it and try again."
        if not any(isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon") for g in geometries):
            return "no polygon was found in this file and it was ignored."
        return None
    except source.InvalidPolygon as error:
        return f"this file cannot be used as a polygon ({error.kind.replace('_', ' ')}) and was ignored."
    except Exception:
        return "this file is not valid JSON/GeoJSON and was ignored."


def _chat_input(original):
    """Wraps ``st.chat_input``: translates the placeholder and injects pending text.

    Returns:
        The injected ``st.session_state["_pending_prompt"]`` (consumed once) when set,
        otherwise what the user typed, followed by a one-line note when a geospatial file
        is attached (the content itself never goes into the message).
    """
    translating = _translating(original)

    def wrapper(*args, **kwargs):
        typed = translating(*args, **kwargs)
        pending = st.session_state.pop("_pending_prompt", None)
        if pending:
            return pending
        upload = st.session_state.get(_GEOFILE_KEY)
        if typed and upload:
            return f"{typed}\n\n[Geospatial file attached: {upload['name']}]"
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
    """Wraps ``Harness.run``: per-session lock.

    ``Harness.run`` is synchronous (it returns the final output; ``stream=True`` is ignored by
    Semente), so holding the lock for the call covers the whole agent turn. The geospatial
    files themselves are forwarded by Semente's web app and converted by its input step.

    Side effects:
        Holds ``session_lock(session_id)``.
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
    st.file_uploader = _file_uploader(st.file_uploader)
    st.chat_input = _chat_input(st.chat_input)
    st.empty = _empty(st.empty)
    st.markdown = _markdown(st.markdown)
    st.sidebar.title = _translating(st.sidebar.title)
    from semente.core.harness import Harness

    Harness.run = _locked_run(Harness.run)
    setattr(st, _FLAG, True)
