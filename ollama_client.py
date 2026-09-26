"""
ollama_client.py
=================
Shared low-level Ollama /api/chat client - the "think: True" + format="json"
+ tolerant-JSON-recovery + cloud-then-local-fallback logic that
telegram_nlp.py and telegram_go.py each used to independently reimplement.
Both now delegate here; telegram_nlp.py's local-only structured calls
(extract_resources/extract_quantities) use chat_json() directly, telegram_go.py
and telegram_orna.py's ReAct loop use chat_json_with_fallback().

Why "think": True is never optional: verified directly (2026-09-22, see
CLAUDE.md) that gpt-oss:20b returns empty/truncated-mid-reasoning content
instead of the requested JSON near 100% of the time with "think": False -
not occasional flakiness, a near-total failure rate. "think": True is
reliable because Ollama separates the reasoning out on its own so `content`
comes back as clean JSON.

Why a non-dict parse result is a hard failure, not a soft {}: a caller
always does `data.get(...)` on the result. If the model's JSON happens to
be a bare list/string/number (valid JSON, just not an object), handing that
straight back let `.get()` raise an uncaught AttributeError one level up -
not an OllamaError, so no `except OllamaError:` call site could catch it,
and (telegram_bot.py registering no global error handler) the whole
request died silently. Reproduced live via telegram_nlp/telegram_go's old
duplicate implementations before this module existed. See
telegram_bot.py's error handler for the other half of this fix.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import httpx

import usage_stats

logger = logging.getLogger(__name__)

OLLAMA_CLOUD_HOST = "https://ollama.com"
# read=120s: the local models this falls back to are slow on a long
# context, and cutting one off mid-generation wastes the whole call.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=20.0, pool=10.0)


# Circuit breaker for the cloud leg. When every call tries cloud first (which
# is what telegram_orna's loop does now), a cloud outage costs a FULL cloud
# timeout on every single step before the local fallback even starts - a
# 16-step /orna request pays 16 x 45s of pure waiting and blows its whole
# wall-clock budget without doing any work. Live outage 2026-09-24: Ollama
# Cloud started returning ReadTimeout (an httpx error whose str() is empty,
# which is why the log line reads "Ollama request failed: " with nothing after
# it) and the bot went silent from the user's side. One failure now parks the
# cloud leg for CLOUD_COOLDOWN_SECONDS so the rest of that request - and other
# requests in the same window - go straight to local at full speed; the next
# call after the cooldown probes cloud again, and any success clears it.
CLOUD_COOLDOWN_SECONDS = 300
_cloud_down_until = 0.0


def cloud_is_parked() -> bool:
    """Whether the cloud leg is currently skipped (see CLOUD_COOLDOWN_SECONDS)."""
    return time.monotonic() < _cloud_down_until


def reset_cloud_cooldown() -> None:
    """Un-park the cloud leg immediately.

    For the model-comparison harness: one model's failures would otherwise park
    cloud for 300s and the NEXT model under test would be measured entirely
    through its local fallback, which looks like that model being fast and
    wrong. Not used by the bot itself - a real request should always respect the
    breaker."""
    global _cloud_down_until
    _cloud_down_until = 0.0


class OllamaError(RuntimeError):
    """Raised when Ollama can't be reached, or replies with something no
    caller can use as a structured-output dict (including valid JSON that
    isn't an object - see module docstring)."""


def _is_no_vision_error(body: str) -> bool:
    """Whether a 400 body means "this model can't accept images".

    Ollama does NOT use one wording for this. Two real ones, both seen live:
    local Ollama says "does not support multimodal requests", Ollama Cloud
    says "this model does not support image input". The original check looked
    for "multimodal" only, so the cloud phrasing fell through as a generic
    OllamaError - which, with a cloud-first loop, is actively harmful: it is
    indistinguishable from an outage, so it would trip the cloud circuit
    breaker and park cloud for CLOUD_COOLDOWN_SECONDS for every caller,
    degrading /orna because someone sent /go a photo. Caught 2026-09-24 while
    switching GO_MODEL to a vision-less model, by actually posting an image
    and reading the body rather than trusting the existing check."""
    low = body.lower()
    return "multimodal" in low or ("image" in low and "support" in low)


class OllamaUnavailable(OllamaError):
    """The request never produced a reply: timeout, connection failure, or an
    HTTP error status. Distinct from its parent, which also covers a reply
    that ARRIVED but wasn't usable JSON - and the difference decides whether
    the cloud circuit breaker trips. Live 2026-09-24: nemotron-3-super
    answered one step with plain prose instead of the requested JSON, and
    because that raised a bare OllamaError it parked the cloud leg for 300s -
    i.e. one bad ANSWER took cloud away from every request for five minutes.
    A model writing something unparseable says nothing about whether the
    service is reachable, so only this subclass parks it."""


class UnsupportedMultimodal(Exception):
    """Raised when Ollama rejects a request because the model has no
    vision support (a clean 400 "does not support multimodal requests") -
    distinct from a generic OllamaError so a caller can drop the image and
    retry text-only instead of failing the whole turn."""


def _extract_json(content: str) -> dict:
    """Parse `content` as JSON, tolerating trailing garbage after an
    otherwise-valid object - format="json" should guarantee clean output,
    but a stray code fence/preamble or trailing garbage has been seen live.
    `raw_decode` parses exactly one complete value starting at the first
    "{" and stops there, instead of a greedy `\\{.*\\}` regex that would
    span all the way to the LAST "}" in the string (the exact bug this
    replaced - see git history). Raises OllamaError if nothing parseable
    is found, or if the parsed value isn't a JSON object."""
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        if start == -1:
            raise OllamaError(f"Ollama returned non-JSON content: {content[:200]!r}")
        try:
            obj, _ = json.JSONDecoder().raw_decode(content, start)
        except json.JSONDecodeError as e:
            raise OllamaError(f"Ollama returned malformed JSON: {content[:200]!r}") from e
    if not isinstance(obj, dict):
        raise OllamaError(f"Ollama returned non-object JSON ({type(obj).__name__}): {content[:200]!r}")
    return obj


def _from_tool_calls(message: dict) -> Optional[dict]:
    """Recover the intended JSON object from a NATIVE tool call, for a model
    that answered with one even though no tools were ever declared.

    gpt-oss:20b is a Harmony-format model: asked to pick one of several named
    actions (which is what /orna's loop prompt is), it emits a real tool call
    roughly half the time instead of the plain JSON object asked for - content
    comes back EMPTY and the choice lands in message.tool_calls. Measured live
    2026-09-24 against the real /orna prompt: 11/20 calls. Ollama logs
    "harmony parser: no reverse mapping found for function name" (there is no
    mapping - we declare no tools) and, less often, 500s outright on it.

    The model's decision is CORRECT in these replies, just delivered in the
    wrong field - so translate rather than discard. Prompt wording alone can't
    close this (measured: 9/20 -> 17/20 OK, never 20/20); this is the half of
    the fix that's deterministic.

    The 500 itself is NOT recoverable here (it carries no body) - it's
    prevented instead, by declaring those action names in `tools` so the
    parser HAS a reverse mapping. Live 2026-09-24: 91 "no reverse mapping"
    warnings -> 27 bare 500s in one day's ollama server log with nothing
    declared; 0 warnings and 0 500s over the same probe with the names
    declared. See telegram_orna._STEP_TOOLS. This function still matters
    after that fix - a mapped call comes back as a perfectly valid
    tool_calls reply with EMPTY content, which is exactly what it
    translates.

    `arguments` IS the object the model meant to send. Two real shapes:
    the whole object inside arguments ({"action":"open_entry",...}), or the
    action in the call's NAME with only its own args inside
    ({"name":"knowledge_search","arguments":{"action_input":"Knight Sirus"}}).
    A leftover key that isn't part of the action schema is that action's own
    argument (a `query`'s conditions/category/sort_by arrive flat), so it gets
    nested under "args" where the loop reads it from."""
    calls = message.get("tool_calls") or []
    fn = (calls[0].get("function") or {}) if calls else {}
    args = fn.get("arguments")
    if not isinstance(args, dict):
        return None
    if "action" in args or not fn.get("name"):
        return dict(args)
    rest = dict(args)
    obj = {"action": fn["name"]}
    for key in ("thought", "action_input", "options", "args"):
        if key in rest:
            obj[key] = rest.pop(key)
    if rest:
        obj.setdefault("args", rest)
    return obj


def _sampling_options() -> dict:
    """{"temperature": float, "seed": int} from ORNA_LLM_TEMPERATURE /
    ORNA_LLM_SEED, or {} when neither is set. Read once at import: this is a
    test-harness knob, not something to flip per request."""
    out: dict = {}
    for env, key, cast in (("ORNA_LLM_TEMPERATURE", "temperature", float), ("ORNA_LLM_SEED", "seed", int)):
        raw = os.environ.get(env)
        if raw not in (None, ""):
            try:
                out[key] = cast(raw)
            except ValueError:
                logger.warning("ollama: ignoring unparseable %s=%r", env, raw)
    return out


_SAMPLING_OPTIONS = _sampling_options()


async def chat_json(host: str, model: str, messages: list[dict], headers: Optional[dict] = None,
                     timeout: httpx.Timeout = DEFAULT_TIMEOUT, tools: Optional[list] = None) -> dict:
    """POST /api/chat with think:True + format=json, return the parsed
    JSON object (a single HTTP attempt - no retry/fallback here, see
    chat_json_with_fallback for that). Raises OllamaError on any HTTP
    failure or unusable reply, UnsupportedMultimodal if the model rejected
    an attached image.

    `tools` is passed straight through to Ollama. A caller whose prompt asks
    the model to pick one of several NAMED actions should declare those names
    here even though this function never dispatches a tool call itself - see
    _from_tool_calls for why (a harmony-format model emits a native call
    regardless, and with nothing declared Ollama 500s on some of them)."""
    payload = {"model": model, "messages": messages, "stream": False, "format": "json", "think": True}
    if tools:
        payload["tools"] = tools
    # Opt-in reproducibility for the test suite (orna_test_suite.py). Ollama
    # takes temperature/seed under "options"; a fixed pair makes a step's reply
    # repeatable for the same prompt, which is the only real determinism lever
    # a suite over this loop has. Unset in production, and then the payload is
    # byte-identical to before - same reason `tools` is only added when truthy.
    if _SAMPLING_OPTIONS:
        payload["options"] = dict(_SAMPLING_OPTIONS)
    usage_stats.record_llm_call(model, "cloud" if host == OLLAMA_CLOUD_HOST else "local")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{host}/api/chat", json=payload, headers=headers or {})
            if resp.status_code == 400 and _is_no_vision_error(resp.text):
                raise UnsupportedMultimodal(resp.text[:300])
            resp.raise_for_status()
            msg = resp.json().get("message") or {}
            content = msg.get("content") or ""
    except httpx.HTTPError as e:
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e
    if not content.strip():
        recovered = _from_tool_calls(msg)
        if recovered is not None:
            return recovered
    return _extract_json(content)


def drop_images(messages: list[dict]) -> bool:
    """Strip any attached images from `messages` IN PLACE, noting it in
    that message's text - if `messages` is a session's own list, this also
    stops a future turn from re-attempting the same doomed image. Returns
    whether anything was actually dropped, so the caller knows a retry is
    worth it."""
    dropped = False
    for m in messages:
        if m.get("images"):
            m.pop("images")
            m["content"] = f"{m.get('content', '')}\n[an attached image couldn't be processed - this model has no vision support]"
            dropped = True
    return dropped


async def chat_json_with_fallback(cloud_model: str, local_host: str, local_model: str, messages: list[dict],
                                   api_key: Optional[str] = None, cloud_host: str = OLLAMA_CLOUD_HOST,
                                   timeout: httpx.Timeout = DEFAULT_TIMEOUT, tools: Optional[list] = None,
                                   local_timeout: Optional[httpx.Timeout] = None) -> dict:
    """Try Ollama Cloud first, falling back to a local Ollama model/host if
    the cloud call fails for any reason (out of credits, network, flaky
    wifi, ...). Either way, if a vision-carrying message hits a model with
    no vision support, drop the image(s) and retry once rather than
    failing the whole turn - mirrors telegram_go.py's original
    _call_model, generalized so telegram_orna.py's loop can use the same
    logic instead of a third copy. `timeout` applies to both legs - a
    caller with its own step/retry budget (telegram_orna's loop) can pass
    something shorter than DEFAULT_TIMEOUT so a slow/hanging cloud call
    fails over to local faster instead of eating most of that budget on
    one attempt (live incident: a single step spent ~2.5 minutes - a full
    90s cloud timeout, then a slow local response - before giving up).

    Log level is deliberately light here (no exc_info): a cloud->local
    fallback is an anticipated, handled path, not a crash - the caller
    logs the full traceback if and when it actually gives up, so one
    real failure doesn't produce several redundant stack traces across
    every retry/fallback layer (verified live: 4 full tracebacks for one
    failed step before this)."""
    # A caller that tries cloud on EVERY call (telegram_orna's loop) wants the
    # two legs on different deadlines: cloud short, so a slow/dead cloud call
    # gives up and hands over quickly, and local long, since the local model is
    # genuinely slower and is the last resort - cutting it off produces nothing
    # at all. Defaults to one deadline for both, which is what /go passes.
    global _cloud_down_until
    local_timeout = local_timeout or timeout
    cloud_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if cloud_is_parked():
        # Cloud failed recently - don't pay its timeout again yet.
        return await _local_leg(local_host, local_model, messages, local_timeout, tools)
    try:
        result = await chat_json(cloud_host, cloud_model, messages, cloud_headers, timeout=timeout, tools=tools)
        _cloud_down_until = 0.0
        return result
    except UnsupportedMultimodal:
        logger.warning("ollama_client: %s has no vision support, dropping attached image(s)", cloud_model)
        if not drop_images(messages):
            raise
        try:
            result = await chat_json(cloud_host, cloud_model, messages, cloud_headers, timeout=timeout, tools=tools)
            _cloud_down_until = 0.0
            return result
        except OllamaError as e:
            # The post-image-drop retry can still fail for an ordinary
            # transient reason (rate limit, network). Fall through to the
            # local fallback below instead of letting it escape - previously
            # this retry was a bare await inside the multimodal handler, so
            # its OllamaError bypassed local entirely.
            logger.warning("ollama_client: cloud retry after image-drop failed (%s), falling back to local", e)
            if isinstance(e, OllamaUnavailable):
                _cloud_down_until = time.monotonic() + CLOUD_COOLDOWN_SECONDS
    except OllamaError as e:
        # Park the cloud leg only for a genuine availability failure. A reply
        # that arrived but wasn't usable JSON is a CONTENT problem - still
        # worth falling back over, never worth taking cloud away from every
        # other request for five minutes.
        if isinstance(e, OllamaUnavailable):
            _cloud_down_until = time.monotonic() + CLOUD_COOLDOWN_SECONDS
            logger.warning("ollama_client: Ollama Cloud unreachable (%s), skipping it for %ds",
                           e, CLOUD_COOLDOWN_SECONDS)
        else:
            logger.warning("ollama_client: cloud reply unusable (%s), falling back to local this turn", e)

    return await _local_leg(local_host, local_model, messages, local_timeout, tools)


async def _local_leg(local_host: str, local_model: str, messages: list[dict],
                     local_timeout: httpx.Timeout, tools: Optional[list]) -> dict:
    """The local half of chat_json_with_fallback, with its own
    drop-image-and-retry-once. Separate so the parked-cloud path can reach
    it without going through the cloud attempt first."""
    try:
        return await chat_json(local_host, local_model, messages, timeout=local_timeout, tools=tools)
    except UnsupportedMultimodal:
        logger.warning("ollama_client: %s has no vision support, dropping attached image(s)", local_model)
        if drop_images(messages):
            return await chat_json(local_host, local_model, messages, timeout=local_timeout, tools=tools)
        raise


def _demo() -> None:
    """Pins _from_tool_calls against the three tool-call shapes gpt-oss:20b
    actually produced against the live /orna prompt (captured 2026-09-24)."""
    # 1. action in the call's NAME, only that action's own arg inside.
    got = _from_tool_calls({"tool_calls": [{"function": {
        "name": "knowledge_search", "arguments": {"action_input": "Knight Sirus"}}}]})
    assert got == {"action": "knowledge_search", "action_input": "Knight Sirus"}, got

    # 2. the whole action object handed over as `arguments` (name is junk).
    got = _from_tool_calls({"tool_calls": [{"function": {"name": "output", "arguments": {
        "thought": "Open the raid entry", "action": "open_entry", "action_input": "/codex/raids/apollyon/"}}}]})
    assert got["action"] == "open_entry" and got["action_input"] == "/codex/raids/apollyon/", got

    # 3. a `query`'s own args arrive FLAT - they belong under "args", which is
    #    where the loop reads conditions/category/sort_by from.
    got = _from_tool_calls({"tool_calls": [{"function": {"name": "query", "arguments": {
        "category": "items", "combinator": "and", "sort_by": "magic",
        "conditions": [{"kind": "stat", "field": "magic", "cmp": ">", "value": 250}]}}}]})
    assert got["action"] == "query", got
    assert got["args"]["category"] == "items" and got["args"]["sort_by"] == "magic", got
    assert got["args"]["conditions"][0]["field"] == "magic", got
    assert "conditions" not in got, got

    # 3b. with tools DECLARED the model also nests an action's own args under
    #     "args" itself (observed live 2026-09-24) - keep that dict as-is.
    got = _from_tool_calls({"tool_calls": [{"function": {"name": "query", "arguments": {
        "args": {"category": "items", "combinator": "or", "conditions": [{"kind": "text"}]}}}}]})
    assert got == {"action": "query", "args": {
        "category": "items", "combinator": "or", "conditions": [{"kind": "text"}]}}, got

    # Both real "no vision" wordings must be recognised - a miss here looks
    # like an outage and parks the cloud leg for everyone (see the function).
    assert _is_no_vision_error('{"error":"this model does not support image input (ref: abc)"}')
    assert _is_no_vision_error("does not support multimodal requests")
    assert not _is_no_vision_error('{"error":"model not found"}')
    assert not _is_no_vision_error('{"error":"rate limit exceeded"}')

    # A transport failure is OllamaUnavailable (parks cloud); an unusable
    # reply is a plain OllamaError (must NOT park it).
    assert issubclass(OllamaUnavailable, OllamaError)
    try:
        _extract_json("Balor Sword:\n- Tier 5")
    except OllamaError as e:
        assert not isinstance(e, OllamaUnavailable), "bad content must not look like an outage"
    else:
        raise AssertionError("prose should not parse as JSON")

    # 4. an ordinary reply must NOT be rescued - it goes to _extract_json.
    assert _from_tool_calls({"content": '{"action":"finish"}'}) is None
    assert _from_tool_calls({"tool_calls": []}) is None
    assert _from_tool_calls({"tool_calls": [{"function": {"name": "x", "arguments": "not-a-dict"}}]}) is None

    print("ollama_client: all checks passed")


if __name__ == "__main__":
    _demo()
