"""Google Earth Engine access — lazy, idempotent initialization.

Importing this module never touches the network nor reads credentials:
Earth Engine is initialized on the first call to ``initialize_ee`` using the
``GEE_PROJECT``, ``GEE_SERVICE_ACCOUNT`` and ``GEE_KEY_FILE`` settings of
``semente.configs.config``. All other geospatial services call it before
building any ``ee`` object.
"""

import threading

import ee

from semente.configs.config import config
from semente.logging import log_debug

_HIGHVOLUME_URL = "https://earthengine-highvolume.googleapis.com"
_lock = threading.Lock()
_initialized = False


class EarthEngineUnavailable(Exception):
    """Earth Engine cannot be used (missing credentials or failed initialization)."""


def initialize_ee() -> None:
    """Initializes Earth Engine once per process (thread-safe, idempotent).

    Uses the service account key file when ``GEE_SERVICE_ACCOUNT`` and
    ``GEE_KEY_FILE`` are set, otherwise default credentials, always with the
    ``GEE_PROJECT`` project and the high-volume endpoint.

    Raises:
        EarthEngineUnavailable: If ``GEE_PROJECT`` is not set or the
        initialization fails; the original error is chained.

    Side effects:
        Authenticates against Earth Engine (network) on the first successful
        call; later calls return immediately.
    """
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        if not config.GEE_PROJECT:
            raise EarthEngineUnavailable("GEE_PROJECT is not set.")
        try:
            credentials = None
            if config.GEE_SERVICE_ACCOUNT and config.GEE_KEY_FILE:
                credentials = ee.ServiceAccountCredentials(config.GEE_SERVICE_ACCOUNT, config.GEE_KEY_FILE)
            ee.Initialize(credentials, project=config.GEE_PROJECT, opt_url=_HIGHVOLUME_URL)
        except Exception as error:
            raise EarthEngineUnavailable(f"Earth Engine initialization failed: {error}") from error
        _initialized = True
        log_debug("initialize_ee: Earth Engine initialized")
