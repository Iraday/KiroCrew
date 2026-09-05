"""Custom-endpoint validation, the ``safe_mode`` gate, and the doctor probe.

Three concerns over one piece of configuration -- the base URL and API key a
harness in :data:`~kiro_crew.acp_backends.ACP_BACKENDS_ANTHROPIC_BASE_URL` is
pointed at:

* :func:`validate_provider_settings` -- pure inspection of the config object.
  Returns human-readable problems and never raises, so the caller owns severity:
  ``doctor`` prints them, the provider factory logs them once per boot.
* :func:`assert_endpoint_allowed` -- the ``agent.safe_mode`` guardrail. With
  safe mode on, the resolved endpoint must be loopback, RFC1918, link-local or
  Tailscale CGNAT, so a typo cannot point the agent (and the credentials and
  source it reads) at an arbitrary public router.
* :func:`probe_provider_endpoint` -- one small Anthropic-shaped request, used by
  ``doctor`` to report reachability, the auth verdict and latency.

**Safe mode fails CLOSED.** An unresolvable host is refused rather than
allowed: a name that does not resolve now may resolve to anything later, and the
whole point of the gate is that the operator never finds out by having traffic
leave the machine.

:func:`assert_endpoint_allowed` and :func:`probe_provider_endpoint` both block
on network I/O (DNS and a synchronous HTTP request). Callers on the event loop
must route them through a thread.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

from kiro_crew.acp_backends import ACP_BACKENDS_ANTHROPIC_BASE_URL

logger = logging.getLogger(__name__)

#: Doctor's probe budget. Short: it runs inline in a diagnostic the operator is
#: waiting on, and "unreachable" is a useful answer well before a TCP timeout.
_PROBE_TIMEOUT_S = 6.0

#: Tailscale's CGNAT range. Not covered by ``ip_address.is_private``, but a
#: tailnet address is exactly as operator-controlled as an RFC1918 one.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

#: Hostname suffixes that resolve only inside the host or the local network.
#: ``localhost`` is matched exactly; the rest match as dotted suffixes.
_LOCAL_HOST_EXACT = frozenset({"localhost"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _host_is_private(host: str) -> bool:
    """Whether *host* is an IP literal in a loopback, private or CGNAT range."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal -- the caller has to resolve it first.
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local or addr in _CGNAT_NETWORK


def classify_endpoint(url: str) -> tuple[str, int]:
    """Split *url* into ``(host, port)``, applying the scheme's default port.

    Raises ``ValueError`` when the URL carries no host at all, which is the one
    shape no caller can do anything sensible with.
    """
    parsed = urlparse(url if "//" in url else f"http://{url}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"provider_base_url has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _is_local_name(host: str) -> bool:
    """Whether *host* is a name that cannot leave the host or local network."""
    lowered = host.lower().rstrip(".")
    if lowered in _LOCAL_HOST_EXACT:
        return True
    return any(lowered.endswith(suffix) for suffix in _LOCAL_HOST_SUFFIXES)


def endpoint_is_local(url: str) -> bool | None:
    """Safe-mode verdict for *url*: True local, False public, None unresolvable.

    ``None`` is distinct from ``False`` because the two want different words in
    the error the operator reads; both are refused by
    :func:`assert_endpoint_allowed`.

    Blocking: resolves DNS.
    """
    host, _port = classify_endpoint(url)
    if _host_is_private(host):
        return True
    if _is_local_name(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.warning("safe_mode: cannot resolve %s (%s)", host, exc)
        return None
    # A single public record makes the host public. Any other rule lets a
    # split-horizon name with one private answer carry traffic off the machine.
    return all(_host_is_private(info[4][0]) for info in infos)


def assert_endpoint_allowed(url: str, *, safe_mode: bool) -> None:
    """Raise ``ValueError`` when *safe_mode* is on and *url* is not local.

    Blocking: resolves DNS. Called from provider-factory construction and from
    ``doctor``, both of which are already off the event loop.
    """
    if not safe_mode:
        return
    verdict = endpoint_is_local(url)
    if verdict is True:
        return
    reason = (
        "resolves to a PUBLIC address"
        if verdict is False
        else "could not be resolved (fail-closed)"
    )
    raise ValueError(
        f"agent.safe_mode is on and provider_base_url {url!r} {reason}. Only "
        "loopback, RFC1918, link-local and Tailscale (100.64.0.0/10) endpoints "
        "are permitted. Turn agent.safe_mode off to allow public routers."
    )


def custom_endpoint_env(agent: object, backend: str) -> dict[str, str]:
    """Environment a *backend* needs to reach the operator's custom endpoint.

    ``{}`` for every harness outside
    :data:`~kiro_crew.acp_backends.ACP_BACKENDS_ANTHROPIC_BASE_URL`, and for a
    member with nothing configured. Callers invoke it unconditionally and merge
    the result, so the Kiro construction path gains no branch of its own and no
    new way to fail (harness-parity H13): a non-member returns on the membership
    test before anything is read, resolved or raised.

    Blocking when, and only when, ``safe_mode`` is on AND a base URL is
    configured AND this backend reads it -- the gate resolves DNS. The factory
    already performs filesystem I/O, and this costs an operator who opted into
    both settings one lookup per provider construction.

    Raises ``ValueError`` from :func:`assert_endpoint_allowed` when safe mode
    refuses the endpoint. That is deliberate: a refused endpoint must fail
    construction loudly rather than silently fall back to the harness default,
    which is the public endpoint safe mode exists to prevent reaching.
    """
    if backend not in ACP_BACKENDS_ANTHROPIC_BASE_URL:
        return {}

    from kiro_crew.provider_secrets import effective_provider_api_key

    env: dict[str, str] = {}
    if getattr(agent, "use_shim", False):
        # The shim owns the base URL it binds; a configured provider_base_url
        # would point past the proxy the operator just asked for.
        port = int(getattr(agent, "shim_port", 0) or 0)
        if port:
            from kiro_crew.shim import DEFAULT_HOST

            env["ANTHROPIC_BASE_URL"] = f"http://{DEFAULT_HOST}:{port}"
    else:
        base_url = (getattr(agent, "provider_base_url", "") or "").strip()
        if base_url:
            assert_endpoint_allowed(base_url, safe_mode=bool(getattr(agent, "safe_mode", False)))
            env["ANTHROPIC_BASE_URL"] = base_url

    api_key = effective_provider_api_key(getattr(agent, "provider_api_key", "") or "")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def validate_provider_settings(agent: object) -> list[str]:
    """Problems with the custom-endpoint settings on *agent*, worst first.

    Pure: no network and no config writes, so a caller can render this on a
    dashboard poll. Keyed on ``acp_backend`` membership rather than on a
    harness name, so a second harness that reads ``ANTHROPIC_BASE_URL`` is
    validated by joining the set (harness-parity H6).
    """
    problems: list[str] = []
    backend = getattr(agent, "acp_backend", "")
    if backend not in ACP_BACKENDS_ANTHROPIC_BASE_URL:
        return problems

    base_url = (getattr(agent, "provider_base_url", "") or "").strip()
    model = (getattr(agent, "model", "") or "").strip()
    configured_key = getattr(agent, "provider_api_key", "") or ""
    use_shim = bool(getattr(agent, "use_shim", False))

    from kiro_crew.provider_secrets import describe_key_source, effective_provider_api_key

    api_key = effective_provider_api_key(configured_key)

    if base_url and model == "auto":
        problems.append(
            "provider_base_url is set but agent.model is 'auto'. A custom "
            "endpoint cannot resolve 'auto' -- set a model id the endpoint serves."
        )
    if use_shim and not (getattr(agent, "shim_openai_base_url", "") or "").strip():
        problems.append(
            "agent.use_shim is on but agent.shim_openai_base_url is empty; the "
            "shim has nowhere to forward."
        )
    if use_shim and base_url:
        problems.append(
            "agent.use_shim and agent.provider_base_url are both set. The shim "
            "supplies the base URL it binds, so the configured one is ignored."
        )
    if base_url and not api_key and not _is_probably_local(base_url):
        problems.append(
            "provider_base_url points off-host with no API key resolved; the "
            "endpoint will most likely answer 401."
        )
    if configured_key and describe_key_source(configured_key) == "config.json plaintext":
        problems.append(
            "The router API key is stored in plaintext in config.json. Prefer "
            "the OS keyring or the KIROCREW_PROVIDER_API_KEY environment variable."
        )
    if base_url:
        try:
            classify_endpoint(base_url)
        except ValueError as exc:
            problems.append(f"provider_base_url is unusable: {exc}")
    return problems


def _is_probably_local(url: str) -> bool:
    """Literal-only locality test, for messages that must not resolve DNS.

    :func:`validate_provider_settings` is documented as pure, so it cannot call
    :func:`endpoint_is_local`. A name that would resolve to a private address
    is treated as non-local here, which at worst produces one extra advisory
    line rather than a missing one.
    """
    try:
        host, _port = classify_endpoint(url)
    except ValueError:
        return False
    return _host_is_private(host) or _is_local_name(host)


def probe_provider_endpoint(base_url: str, api_key: str) -> dict[str, object]:
    """One small Anthropic-shaped POST at *base_url*, for ``doctor``.

    Returns ``{ok, status, latency_ms, verdict}``. Blocking (stdlib urllib, so
    this module stays importable with no third-party dependency); the caller
    runs it in a worker thread.

    A 400 counts as success: the payload is deliberately minimal and a backend
    that rejects it has still proved it is reachable and accepted the
    credential. Only 401/403 distinguish an auth failure.
    """
    import json
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/v1/messages"
    body = json.dumps(
        {"model": "probe", "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    ).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
        headers["authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "verdict": f"unreachable: {exc}",
        }
    latency = int((time.monotonic() - started) * 1000)

    if status == 200:
        ok, verdict = True, "reachable, credential accepted"
    elif status == 400:
        ok, verdict = True, "reachable, credential accepted (probe payload rejected, as expected)"
    elif status in (401, 403):
        ok, verdict = False, "reachable but authentication FAILED; check the API key"
    elif status == 404:
        ok, verdict = False, "reachable but /v1/messages is absent; check the base URL path"
    else:
        ok, verdict = status < 500, f"reachable, unexpected status {status}"
    return {"ok": ok, "status": status, "latency_ms": latency, "verdict": verdict}
