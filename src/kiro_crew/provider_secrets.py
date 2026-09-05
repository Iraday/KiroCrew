"""Router API-key storage, with optional OS-keyring backing.

Precedence everywhere the custom-endpoint API key is read:

1. ``KIROCREW_PROVIDER_API_KEY`` (CI and service deployments, where no keyring
   daemon exists and config.json is often baked into an image),
2. the OS keyring (service :data:`SERVICE_NAME`, entry :data:`ENTRY_NAME`) when
   the optional :mod:`keyring` package is installed and has a usable backend,
3. plaintext ``agent.provider_api_key`` from config.json.

Nothing here is load-bearing for boot. Every failure degrades to "the plaintext
path still works" with a log line, never an exception: a headless host with no
keyring daemon must still start, and the desktop app must not acquire a hard
dependency on one just because it happens to be installed.

The key resolved here is a SECRET that reaches a subprocess environment, so it
is never logged. :func:`describe_key_source` reports the source only, and is the
only function whose output is intended for an operator-facing surface.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Keyring service and entry names. Stable identifiers -- changing either
#: orphans every key an operator has already stored, with no error and no
#: migration path, because the old entry stays readable only under the old name.
SERVICE_NAME = "kirocrew"
ENTRY_NAME = "provider_api_key"

#: Environment override, highest precedence.
ENV_VAR = "KIROCREW_PROVIDER_API_KEY"

#: Substring identifying keyring's null backend. It imports and constructs
#: cleanly on a headless box and then raises on every actual call, so importing
#: the package is NOT evidence that a key can be stored.
_FAIL_BACKEND_MARKER = "fail"


def keyring_available() -> bool:
    """Whether :mod:`keyring` imports AND resolves to a backend that can store.

    A headless Linux host without gnome-keyring, KWallet or D-Bus resolves to
    keyring's ``fail`` backend, which raises on every operation. Reporting that
    as "available" would send an operator to a migration that cannot succeed.
    """
    try:
        import keyring

        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        return _FAIL_BACKEND_MARKER not in name.lower()
    except Exception:
        # Any import or backend-resolution failure means "no keyring here".
        return False


def store_provider_key(key: str) -> bool:
    """Persist *key* in the OS keyring. False when unsupported or it failed.

    Returning False leaves the caller's existing storage untouched, which is
    what makes a failed migration non-destructive.
    """
    if not key:
        return False
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, ENTRY_NAME, key)
        return True
    except Exception as exc:
        logger.warning("keyring store failed (%s); the key stays where it was", exc)
        return False


def load_provider_key() -> str:
    """The key held in the keyring, or ``""`` when absent or unsupported."""
    try:
        import keyring

        return (keyring.get_password(SERVICE_NAME, ENTRY_NAME) or "").strip()
    except Exception:
        return ""


def clear_provider_key() -> bool:
    """Best-effort removal from the keyring."""
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, ENTRY_NAME)
        return True
    except Exception:
        return False


def effective_provider_api_key(configured: str | None) -> str:
    """Resolve the key by precedence: env, then keyring, then plaintext config.

    Every reader of ``agent.provider_api_key`` goes through here, so the
    precedence rule has exactly one implementation. A reader that consults the
    config field directly silently ignores an operator's keyring migration.
    """
    env_val = (os.environ.get(ENV_VAR) or "").strip()
    if env_val:
        return env_val
    ring_val = load_provider_key()
    if ring_val:
        return ring_val
    return (configured or "").strip()


def describe_key_source(configured: str | None) -> str:
    """Where the effective key comes from. Never the key itself."""
    if (os.environ.get(ENV_VAR) or "").strip():
        return f"environment ({ENV_VAR})"
    if load_provider_key():
        return "OS keyring"
    if (configured or "").strip():
        return "config.json plaintext"
    return "not set"
