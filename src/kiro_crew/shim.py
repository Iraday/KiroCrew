"""Anthropic-to-OpenAI translation proxy, bound on loopback.

Speaks the Anthropic Messages API on the front (``POST /v1/messages``) and
forwards to any OpenAI-compatible ``/v1/chat/completions`` backend on the back
(Ollama, llama.cpp's server, vLLM, LM Studio, DeepSeek, LiteLLM). That lets a
harness in ``ACP_BACKENDS_ANTHROPIC_BASE_URL`` drive an OpenAI-shaped endpoint
with no external router in the chain: the harness is pointed at this process
through ``ANTHROPIC_BASE_URL`` and never learns the difference.

**Loopback only.** The listener binds :data:`DEFAULT_HOST` and carries no
authentication of its own, because it holds the backend credential and would
otherwise hand it to anything that can reach the port. Binding it to a routable
interface turns it into an open relay for that key.

Translated, because an agent session is unusable without them: system prompts,
multi-turn text, images (``data:`` URLs pass straight through), tool
advertisement and tool calls in both directions, and SSE streaming with
Anthropic's event framing rebuilt from OpenAI deltas.

NOT translated, and dropped rather than half-honoured: ``tool_choice`` beyond
``auto``, extended-thinking blocks, and server-side tools. Each is logged once
and the turn proceeds without it, on the reasoning that a working turn missing
an optional hint beats a hard error on every request; a caller that needs one of
them wants a real router, not this.

Token counting is a heuristic (see :func:`handle_count_tokens`). The caller uses
it for context-window gating, where a ballpark is sufficient and exact tokenizer
parity would mean shipping a tokenizer per backend.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import ClientSession, web

logger = logging.getLogger(__name__)

#: The only interface this proxy may bind. See the module docstring.
DEFAULT_HOST = "127.0.0.1"

#: Anthropic's required ``max_tokens`` has no OpenAI equivalent, so a request
#: that omits it still needs a number to forward.
DEFAULT_MAX_TOKENS = 4096

#: Flat token cost charged per image by the heuristic counter. Real cost varies
#: with resolution; this is the order of magnitude for a typical screenshot and
#: errs high, so context gating trips early rather than late.
_IMAGE_TOKEN_COST = 1600

#: Characters per token for the heuristic counter.
_CHARS_PER_TOKEN = 4

STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}

#: Anthropic SSE event names this proxy emits.
_EVENT_NAMES = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
        "error",
    }
)

#: Request fields with no faithful translation. Logged once per process, then
#: ignored -- a module-level ledger rather than per-request, so a long session
#: does not repeat the same line on every turn.
_UNSUPPORTED_WARNED: set[str] = set()


def _warn_unsupported_once(field: str) -> None:
    if field in _UNSUPPORTED_WARNED:
        return
    _UNSUPPORTED_WARNED.add(field)
    logger.warning("shim: %s has no OpenAI equivalent; forwarding without it", field)


def _openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Anthropic ``{name, description, input_schema}`` to OpenAI function defs."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _flatten_content(content: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Anthropic message content to ``(text, image_parts, tool_results)``.

    ``tool_use`` blocks are skipped here and rebuilt by the caller, which has the
    surrounding message role and can attach them as OpenAI ``tool_calls``.
    """
    if isinstance(content, str):
        return content, [], []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "image":
            src = block.get("source", {}) or {}
            if src.get("type") == "base64":
                media = src.get("media_type", "image/png")
                data_url = f"data:{media};base64,{src.get('data', '')}"
            else:
                data_url = src.get("url", "")
            if data_url:
                images.append({"type": "image_url", "image_url": {"url": data_url}})
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner_text = " ".join(
                    b.get("text", "")
                    for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                inner_text = str(inner or "")
            tool_results.append(
                {"tool_call_id": block.get("tool_use_id", ""), "content": inner_text}
            )
    return "\n".join(texts), images, tool_results


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages request into a chat.completions one."""
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "max_tokens": body.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("thinking"):
        _warn_unsupported_once("thinking")
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") not in (None, "auto"):
        _warn_unsupported_once("tool_choice")

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            sys_text = str(system)
        if sys_text.strip():
            messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        text, images, tool_results = _flatten_content(msg.get("content"))
        if role == "user":
            # Tool results are their OWN OpenAI messages and must precede any
            # remaining user text, or the backend sees a result with no call.
            for tr in tool_results:
                messages.append(
                    {"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]}
                )
            if tool_results and not text and not images:
                continue
            if images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
                parts.extend(images)
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": text or ""})
        else:
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            calls = [
                {
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {})),
                    },
                }
                for b in msg.get("content", [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)

    out["messages"] = messages
    tools = _openai_tools(body.get("tools"))
    if tools:
        out["tools"] = tools
    stream = bool(body.get("stream"))
    out["stream"] = stream
    if stream:
        # Ask for a final usage-only chunk. A backend that does not know the
        # field ignores it, and usage then stays 0 -- the same shape the caller
        # would see from any backend that reports nothing.
        out["stream_options"] = {"include_usage": True}
    return out


def openai_to_anthropic(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate a non-streaming chat.completions response back."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function", {}) or {}
        try:
            input_obj = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            # Keep the turn alive: the agent sees a malformed argument object
            # and can retry, where an exception here would drop the whole reply.
            input_obj = {"_raw": fn.get("arguments", "")}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": input_obj,
            }
        )
    usage = payload.get("usage", {}) or {}
    return {
        "id": payload.get("id", "msg_shim"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": STOP_REASON_MAP.get(choice.get("finish_reason", "stop"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _error_body(code: str, kind: str, message: str) -> dict[str, Any]:
    """An Anthropic-shaped error carrying a machine-readable ``code``."""
    return {"code": code, "type": "error", "error": {"type": kind, "message": message}}


def _backend_detail(data: object) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(data)[:300]


class ShimState:
    """Backend coordinates, captured at start and fixed for the app's lifetime."""

    def __init__(self, openai_base_url: str, api_key: str) -> None:
        self.openai_base_url = openai_base_url.rstrip("/")
        self.api_key = api_key


async def handle_messages(request: web.Request) -> web.StreamResponse:
    state: ShimState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            _error_body("invalid_json", "invalid_request_error", "invalid JSON"), status=400
        )
    model = body.get("model", "")
    forward = anthropic_to_openai(body)
    headers = {"content-type": "application/json"}
    if state.api_key:
        headers["authorization"] = f"Bearer {state.api_key}"

    session: ClientSession = request.app["client"]
    try:
        async with session.post(
            f"{state.openai_base_url}/chat/completions", json=forward, headers=headers
        ) as resp:
            if forward.get("stream"):
                return await _stream_translation(resp, request, model)
            data = await resp.json(content_type=None)
            if resp.status != 200:
                return web.json_response(
                    _error_body(
                        "backend_error",
                        "api_error",
                        f"backend {resp.status}: {_backend_detail(data)}",
                    ),
                    status=502,
                )
            return web.json_response(openai_to_anthropic(data, model))
    except Exception as exc:
        logger.exception("shim backend failure")
        return web.json_response(
            _error_body("backend_unreachable", "api_error", f"backend unreachable: {exc}"),
            status=502,
        )


def _sse(event: dict[str, Any]) -> bytes:
    """One Anthropic SSE frame. ``_t`` names the event and is not sent."""
    name = event.get("_t", "")
    if name not in _EVENT_NAMES:
        name = "message_stop"
    payload = {k: v for k, v in event.items() if k != "_t"}
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


async def _stream_error(request: web.Request, message: str) -> web.StreamResponse:
    """A 502 delivered as an SSE error frame.

    The caller has already committed to a stream by this point, so an error has
    to arrive in the framing it is parsing; a JSON body here reads as a
    protocol violation and surfaces as a parse failure instead of the reason.
    """
    out = web.StreamResponse(
        status=502,
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )
    await out.prepare(request)
    await out.write(
        _sse({"_t": "error", "type": "error", "error": {"type": "api_error", "message": message}})
    )
    await out.write_eof()
    return out


async def _stream_translation(resp: Any, request: web.Request, model: str) -> web.StreamResponse:
    """Rebuild Anthropic SSE framing from OpenAI chat.completions chunks.

    Text and tool calls interleave. OpenAI streams a tool call as per-index
    fragments (``delta.tool_calls[i].function.arguments`` arrives in pieces),
    which are re-emitted as an Anthropic ``tool_use`` block carrying
    ``input_json_delta`` partials. Anthropic blocks are strictly sequential, so
    exactly one is open at a time and switching kinds closes the previous one.
    """
    if resp.status != 200:
        try:
            detail = _backend_detail(await resp.json(content_type=None))
        except Exception:
            try:
                detail = (await resp.text())[:300]
            except Exception:
                detail = f"status {resp.status}"
        return await _stream_error(request, f"backend {resp.status}: {detail}")

    out = web.StreamResponse(
        headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "connection": "keep-alive",
        }
    )
    await out.prepare(request)

    msg_id = f"msg_{model}_{id(resp)}"
    await out.write(
        _sse(
            {
                "_t": "message_start",
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )
    )

    next_index = 0
    # None, "text", or ("tool", openai_index) -- which block is currently open.
    open_kind: str | tuple[str, int] | None = None
    tools: dict[int, dict[str, Any]] = {}

    async def close_open() -> None:
        nonlocal open_kind
        if open_kind is None:
            return
        await out.write(
            _sse(
                {"_t": "content_block_stop", "type": "content_block_stop", "index": next_index - 1}
            )
        )
        open_kind = None

    async def open_text() -> None:
        nonlocal next_index, open_kind
        await close_open()
        idx = next_index
        next_index += 1
        await out.write(
            _sse(
                {
                    "_t": "content_block_start",
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        )
        open_kind = "text"

    async def ensure_tool_open(oai_idx: int) -> None:
        nonlocal next_index, open_kind
        acc = tools[oai_idx]
        if acc.get("opened"):
            return
        await close_open()
        a_idx = next_index
        next_index += 1
        await out.write(
            _sse(
                {
                    "_t": "content_block_start",
                    "type": "content_block_start",
                    "index": a_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": acc["id"],
                        "name": acc["name"],
                        "input": {},
                    },
                }
            )
        )
        acc["opened"] = True
        acc["a_index"] = a_idx
        open_kind = ("tool", oai_idx)

    finish_reason = "stop"
    usage_out = 0

    async for raw in resp.content:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:") :].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        usage = chunk.get("usage")
        if usage:
            usage_out = usage.get("completion_tokens", usage_out)

        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta", {}) or {}

        text = delta.get("content")
        if text:
            if open_kind != "text":
                await open_text()
            await out.write(
                _sse(
                    {
                        "_t": "content_block_delta",
                        "type": "content_block_delta",
                        "index": next_index - 1,
                        "delta": {"type": "text_delta", "text": text},
                    }
                )
            )

        for call in delta.get("tool_calls") or []:
            oai_idx = call.get("index", 0)
            acc = tools.setdefault(
                oai_idx, {"id": f"toolu_{oai_idx}", "name": "", "args": "", "opened": False}
            )
            if call.get("id"):
                acc["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                acc["name"] += fn["name"]
            fragment = fn.get("arguments") or ""
            if (fragment or fn.get("name")) and not acc.get("opened"):
                await ensure_tool_open(oai_idx)
            if fragment:
                await out.write(
                    _sse(
                        {
                            "_t": "content_block_delta",
                            "type": "content_block_delta",
                            "index": acc["a_index"],
                            "delta": {"type": "input_json_delta", "partial_json": fragment},
                        }
                    )
                )
                acc["args"] += fragment

    # A backend that named a tool but streamed no arguments still owes the
    # caller a block; without this the call vanishes and the agent stalls.
    for oai_idx, acc in list(tools.items()):
        if not acc.get("opened") and acc["name"]:
            await ensure_tool_open(oai_idx)
    await close_open()

    await out.write(
        _sse(
            {
                "_t": "message_delta",
                "type": "message_delta",
                "delta": {
                    "stop_reason": STOP_REASON_MAP.get(finish_reason, "end_turn"),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": usage_out},
            }
        )
    )
    await out.write(_sse({"_t": "message_stop", "type": "message_stop"}))
    await out.write_eof()
    return out


async def handle_count_tokens(request: web.Request) -> web.Response:
    """``POST /v1/messages/count_tokens`` -- a character-ratio estimate.

    The caller needs a ballpark for context-window gating. Exact parity would
    mean shipping a tokenizer per backend, and the estimate errs high, so gating
    trips early rather than after a turn has already overflowed.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            _error_body("invalid_json", "invalid_request_error", "invalid JSON"), status=400
        )
    total = len(json.dumps(body.get("system", "")))
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                total += len(block.get("text", ""))
            elif block.get("type") == "image":
                total += _IMAGE_TOKEN_COST
            else:
                total += len(json.dumps(block))
    for tool in body.get("tools") or []:
        total += len(json.dumps(tool))
    return web.json_response({"input_tokens": max(1, total // _CHARS_PER_TOKEN)})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "kirocrew-shim"})


def build_shim_app(openai_base_url: str, api_key: str) -> web.Application:
    app = web.Application()
    app["state"] = ShimState(openai_base_url, api_key)
    app["client"] = ClientSession()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_post("/v1/messages/count_tokens", handle_count_tokens)

    async def _close(app_: web.Application) -> None:
        await app_["client"].close()

    app.on_cleanup.append(_close)
    return app


async def start_shim(
    port: int, openai_base_url: str, api_key: str, *, host: str = DEFAULT_HOST
) -> tuple[web.AppRunner, web.TCPSite]:
    """Bind the proxy and return ``(runner, site)``.

    The caller holds both for the process lifetime and awaits
    ``runner.cleanup()`` at shutdown; dropping the runner leaks the listener and
    the backend ``ClientSession``.

    *host* defaults to loopback and should stay there -- see the module
    docstring on why this listener must not be reachable off-host.
    """
    runner = web.AppRunner(build_shim_app(openai_base_url, api_key), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("anthropic shim listening on http://%s:%s -> %s", host, port, openai_base_url)
    return runner, site
