"""Adapts the Semente Streamlit chat to Pasto10 without modifying Semente.

The Semente web app is a monolithic script with fixed Portuguese labels and no
extension points, so Pasto10 runs it unchanged and wraps the ``st.*`` functions
it calls. ``install`` is idempotent: Streamlit re-executes the entrypoint on
every rerun, but this module (and the wrapped functions) lives for the whole
process.

Wrapped behaviours:
    - Portuguese labels, titles, captions, messages and spinner texts become English.
    - The "send location" button (fixed coordinates in Brazil) is hidden.
    - ``st.chat_input`` returns ``st.session_state["_pending_prompt"]`` once, which lets
      other parts of the front end inject a message into the conversation.
    - ``st.selectbox`` over stored users shows the ``nickname`` that Semente saves
      (Semente reads a key that does not exist and lists everybody as "User").
    - ``st.file_uploader`` also accepts ``.geojson``/``.json``; their content is
      appended to the next chat message only (Semente only forwards images and audio).
    - The "Debug Mode" toggle is shown only with ``APP_ENV=development`` set explicitly.
    - ``Workflow.run`` holds the per-session lock, so direct registrations never race an agent turn of
      the same session, and hands an uploaded GeoJSON to the agent through the session state.
    - Generic "Erro" answers are replaced by an English message.
"""

import hashlib
import json
import re

import streamlit as st

from app.test_properties import is_development, session_lock

_FLAG = "_pasto10_installed"
_set_page_config = st.set_page_config
_HIDDEN_BUTTONS = {"Enviar Localizacao da Propriedade"}
_EXTRA_UPLOAD_TYPES = ["geojson", "json"]
_GEOJSON_KEY = "_pending_geojson"
_CHECKED_KEY = "_geojson_checked"
STATE_KEY = "uploaded_geojson"

LABELS = {
    "Login": "Sign in",
    "Historico": "History",
    "Escolha o usuário:": "Choose a user:",
    "Entrar": "Sign in",
    "Vazio": "No saved users",
    "🆕 Novo": "New user",
    "Identificação do usuário": "Your name or nickname",
    "Criar": "Create",
    "Por favor, digite um nome para salvar.": "Please type a name to save.",
    "Anonimo": "Anonymous",
    "Entrar Anonimamente": "Continue anonymously",
    "Configurações": "Settings",
    "Sair / Trocar Usuário": "Sign out / Switch user",
    "Envie imagens/áudio (png, jpg, mp3, etc)": "Upload images, audio or a GeoJSON polygon",
    "Gravar audio": "Record audio",
    "Pergunte sobre pastagem...": "Ask about pastures...",
    "Analisando dados e gerando resposta...": "Analyzing data and generating the answer...",
    "Erro": "Sorry, I could not produce an answer. Please try again.",
}
SUBSTRINGS = (
    ("Visitante Anônimo", "Anonymous visitor"),
)
PREFIXES = (
    ("**Usuario:** ", "**User:** "),
    ("Ola, ", "Hello, "),
    ("Baixar arquivo", "Download file"),
    ("Baixar ", "Download "),
    ("Erro ao processar: ", "Error while processing: "),
    ("Desculpe, ocorreu um erro: ", "Sorry, an error occurred: "),
)


def translate(text):
    """Translates a Semente UI string to English.

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
    """Wraps ``st.file_uploader``: accepts GeoJSON and keeps the validated file for the next turn.

    Side effects:
        Stores ``{"name", "content", "vertices"}`` of the first valid ``.geojson``/``.json``
        upload (within the domain parser limits) in ``st.session_state["_pending_geojson"]``; the
        chat input wrapper adds a short note to the message and ``Workflow.run`` (see
        ``_locked_run``) hands the content to the agent. Those files are removed from the returned
        list because Semente only forwards images and audio. Oversized or invalid files are
        rejected with a warning, never truncated. Semente replaces the uploader after each
        answer, which clears the pending upload.
    """
    translating = _translating(original)

    def wrapper(label, *args, **kwargs):
        types = kwargs.get("type")
        if types is not None:
            kwargs["type"] = list(types) + [t for t in _EXTRA_UPLOAD_TYPES if t not in types]
        files = translating(label, *args, **kwargs)
        kept, accepted = [], []
        for uploaded in files or []:
            if not uploaded.name.lower().endswith(tuple(f".{t}" for t in _EXTRA_UPLOAD_TYPES)):
                kept.append(uploaded)
                continue
            raw = uploaded.getvalue()
            name = safe_filename(uploaded.name)
            digest = hashlib.sha256(raw).hexdigest()
            checked = st.session_state.setdefault(_CHECKED_KEY, {})
            if digest not in checked:
                checked[digest] = _inspect_geojson(raw)
            problem, vertices = checked[digest]
            if problem:
                st.warning(f"{name}: {problem}")
            elif accepted:
                st.warning(f"{name}: only one GeoJSON file is used per message; it was ignored.")
            else:
                accepted.append({"name": name, "content": raw.decode("utf-8"), "vertices": vertices})
        if accepted:
            st.session_state[_GEOJSON_KEY] = accepted[0]
        else:
            st.session_state.pop(_GEOJSON_KEY, None)
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
    """Validates an uploaded GeoJSON file with the same cheap checks the domain parser applies.

    Reuses ``MAX_BYTES``, ``raw_vertex_count`` (iterative, bounded depth, finite lon/lat, rings
    and vertex limits) and the NaN/Infinity rejection of ``user_polygon_source``, and never raises:
    any failure, including a ``RecursionError`` from deeply nested JSON, becomes a problem text.

    Args:
        raw (bytes): The file content.

    Returns:
        tuple: ``(problem, vertices)``; ``problem`` is a user-facing reason (None when the file
        can be sent to the assistant) and ``vertices`` the number of positions found.
    """
    from domain.services.property_sources import user_polygon_source as source

    try:
        if len(raw) > source.MAX_BYTES:
            return f"this file is too large ({len(raw):,} bytes; the limit is {source.MAX_BYTES:,}) and was ignored. Simplify the polygon and try again.", 0
        document = json.loads(raw.decode("utf-8"), parse_constant=source._reject_constant)
        geometries = source._geometries(document) if isinstance(document, dict) else []
        vertices = source.raw_vertex_count(geometries)
        if vertices > source.max_polygon_vertices():
            return f"this polygon has too many vertices (the limit is {source.max_polygon_vertices():,}) and was ignored. Simplify it and try again.", 0
        if not any(isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon") for g in geometries):
            return "no polygon was found in this file and it was ignored.", 0
        return None, vertices
    except source.InvalidPolygon as error:
        return f"this file cannot be used as a polygon ({error.kind.replace('_', ' ')}) and was ignored.", 0
    except Exception:
        return "this file is not valid JSON/GeoJSON and was ignored.", 0


def _chat_input(original):
    """Wraps ``st.chat_input``: translates the placeholder and injects pending text.

    Returns:
        The injected ``st.session_state["_pending_prompt"]`` (consumed once) when set,
        otherwise what the user typed, followed by a one-line note when a GeoJSON file is
        attached (the content itself never goes into the message).
    """
    translating = _translating(original)

    def wrapper(*args, **kwargs):
        typed = translating(*args, **kwargs)
        pending = st.session_state.pop("_pending_prompt", None)
        if pending:
            return pending
        upload = st.session_state.get(_GEOJSON_KEY)
        if typed and upload:
            return f"{typed}\n\n[GeoJSON file attached: {upload['name']}, {upload['vertices']} vertices]"
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
    """Wraps ``Workflow.run``: per-session lock and hand-over of an uploaded GeoJSON.

    ``Workflow.run`` is synchronous (it returns the final output; ``stream=True`` is ignored by
    Semente), so holding the lock for the call covers the whole agent turn.

    Side effects:
        Holds ``session_lock(session_id)``. If a validated upload is pending in
        ``st.session_state``, it is consumed and written to the persisted session state under
        ``uploaded_geojson`` (``{"name", "content"}``) before the turn, so the registration tool can
        read it without the model copying it; the key is removed from the store after the turn.
    """

    def wrapper(self, *args, **kwargs):
        session_id = kwargs.get("session_id")
        with session_lock(session_id):
            upload = st.session_state.pop(_GEOJSON_KEY, None)
            if upload:
                _set_stored_upload(session_id, {"name": upload["name"], "content": upload["content"]})
            try:
                return original(self, *args, **kwargs)
            finally:
                if upload:
                    _set_stored_upload(session_id, None)

    wrapper.__wrapped__ = original
    return wrapper


def _set_stored_upload(session_id, value) -> None:
    """Sets (or removes, with None) ``uploaded_geojson`` in the persisted session state of a session."""
    from semente.database.session_store import store

    loaded = store.get(session_id, session_id) or {"session_state": {}, "runs": []}
    if value is None:
        loaded["session_state"].pop(STATE_KEY, None)
    else:
        loaded["session_state"][STATE_KEY] = value
    store.save(session_id, session_id, session_state=loaded["session_state"], runs=loaded["runs"])


def _empty(original):
    """Wraps ``st.empty`` so ``.markdown`` of the placeholder translates the generic error answer."""

    def wrapper(*args, **kwargs):
        placeholder = original(*args, **kwargs)
        markdown = placeholder.markdown
        placeholder.markdown = lambda body, *a, **kw: markdown(translate(body) if body == "Erro" else body, *a, **kw)
        return placeholder

    wrapper.__wrapped__ = original
    return wrapper


def _markdown(original):
    """Wraps ``st.markdown`` so stored generic-error and error-prefix messages are shown in English."""

    def wrapper(body, *args, **kwargs):
        if isinstance(body, str) and (body == "Erro" or body.startswith("Desculpe, ocorreu um erro: ")):
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
        wraps ``Workflow.run``; sets ``debug_mode_enabled`` to False in the session (once) so the debug
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
    from semente.core.orchestrator import Workflow

    Workflow.run = _locked_run(Workflow.run)
    setattr(st, _FLAG, True)
