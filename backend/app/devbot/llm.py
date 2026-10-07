"""
The model gateway: an OpenAI-compatible API (LiteLLM in front of vLLM).

Every call carries the PERSON's own key. The gateway decides from it which models they
may use and how many requests and tokens a minute they get, and it says so in the
x-ratelimit-* headers of every answer -- which is what lets DevBot show the limits and
stop before hitting them instead of after.

Failures are classified here, once, into a kind the caller can act on and a sentence a
person can: a rejected key, a model the key may not use, a rate limit (with how long to
wait), a model that cannot call tools, a request too big for the model, the gateway
being unreachable. A LiteLLM error body is a paragraph of exception names; nobody should
have to read one.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

from resilient_http import explain_integration_failure

from . import config
import resilient_http

log = logging.getLogger(__name__)

GATEWAY = "The AI service"

# Text a model writes when it tried to call a tool and the server did not turn that
# into a tool call: the server is missing --enable-auto-tool-choice / --tool-call-parser.
TOOL_TEXT_MARKERS = ("<tool_call>", "<|call|>", "[TOOL_CALLS]", "<function=")


def wrote_tool_call_as_text(text: str, tool_names: List[str]) -> bool:
    """Did the model write a call to one of the offered tools as plain text?

    Only the markers a parser leaves behind, or an answer that IS a call -- a JSON object
    naming one of the offered tools -- count. An answer that merely contains JSON (a
    config example, a quoted API response) is an answer: mistaking it for a failed call
    would mark a working model as unable to read data for everybody, for a week.
    """
    if any(marker in text for marker in TOOL_TEXT_MARKERS):
        return True
    stripped = text.strip().strip("`").strip()
    if stripped.startswith("json"):
        stripped = stripped[4:].strip()
    found = re.match(r'^\{\s*"(?:name|function)"\s*:\s*"([\w.-]+)"\s*,\s*"(?:arguments|parameters)"', stripped)
    return bool(found and found.group(1) in tool_names)

# LiteLLM names its limit headers after the SCOPE that set the limit once more than one
# can apply (x-ratelimit-api_key-remaining-requests, ...-model_per_key-..., ...-team-...),
# and older versions and plain OpenAI servers use the bare x-ratelimit-remaining-requests.
# A proxy may also forward the model server's own as llm_provider-x-ratelimit-*.
_LIMIT_HEADER = re.compile(
    r"^(?:llm_provider-)?x-ratelimit-(?:(?P<scope>[a-z_]+)-)?(?P<what>limit|remaining)-(?P<unit>requests|tokens)$")
# LiteLLM also states the key's own limits on every answer, without what is left.
_KEY_LIMIT_HEADER = re.compile(r"^x-litellm-key-(?P<unit>rpm|tpm)-limit$")

_EMBEDDING_NAME = re.compile(r"embed|(^|[/_-])e5([-_]|$)|bge|rerank|whisper|tts|clip|colbert", re.I)


class LLMError(Exception):
    """A gateway failure, with a kind to act on and a sentence to show.

    kind: not_configured, key, model_denied, model_missing, limit, budget, tools,
          context, request, server, timeout, network
    """

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None,
                 retry_after: Optional[int] = None, raw: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.raw = raw
        # For kind "context": the model's real window, when its refusal states it.
        self.context_tokens: Optional[int] = None


# ── plumbing ─────────────────────────────────────────────────────────────────

def base_url() -> str:
    url = config.llm_base_url()
    if not url:
        raise LLMError("not_configured", "DevBot is not set up on this Hub yet. Ask a platform admin.")
    return url


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _headers(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(read, connect=10.0)


def read_limits(headers: Any) -> Dict[str, Any]:
    """The key's limits as the gateway reported them on this answer.

    Where several scopes limit the same thing (the key, the key on this model, the team),
    the one with the least left is the one that will stop the next request, so that is
    the one reported, with its scope.
    """
    found: Dict[tuple, Dict[str, int]] = {}
    try:
        items = list(headers.items())
    except AttributeError:
        return {}
    for name, value in items:
        name = str(name).strip().lower()
        match = _LIMIT_HEADER.match(name)
        own = _KEY_LIMIT_HEADER.match(name)
        if not match and not own:
            continue
        try:
            number = int(float(str(value).strip()))
        except ValueError:
            continue
        if own:
            unit = "requests" if own.group("unit") == "rpm" else "tokens"
            found.setdefault((unit, "key"), {}).setdefault("limit", number)
            continue
        slot = found.setdefault((match.group("unit"), match.group("scope") or "key"), {})
        slot[match.group("what")] = number
    out: Dict[str, Any] = {}
    for unit, short in (("requests", "rpm"), ("tokens", "tpm")):
        scopes = [(scope, v) for (u, scope), v in found.items() if u == unit and v.get("limit", 0) > 0]
        if not scopes:
            continue
        scope, v = min(scopes, key=lambda sv: (sv[1].get("remaining", sv[1]["limit"]), sv[1]["limit"]))
        out[short] = v["limit"]
        if "remaining" in v:
            out[short + "_left"] = max(0, v["remaining"])
        out[short + "_scope"] = scope
    return out


def _server_message(text: str) -> str:
    """The gateway's own words, without the exception-class noise around them."""
    message = text or ""
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or message)
            elif isinstance(err, str):
                message = err
            elif body.get("detail"):
                message = str(body["detail"])
    except ValueError:
        pass
    message = re.sub(r"\blitellm\.\w+:\s*", "", message)
    message = re.sub(r"\b\w+(Error|Exception):\s*", "", message)
    return " ".join(message.split())[:400]


def _retry_after(headers: Any, message: str) -> Optional[int]:
    raw = str(headers.get("retry-after") or "").strip()
    if raw.isdigit():
        return int(raw)
    found = re.search(r"(?:try again|retry)[^0-9]{0,20}(\d+)\s*(?:s|sec|second)", message, re.I)
    return int(found.group(1)) if found else None


def classify(status: int, text: str, headers: Any, model: str = "") -> LLMError:
    """One gateway refusal, as something a person can act on."""
    said = _server_message(text)
    low = said.lower()
    which = f" {model}" if model else " this model"
    if "budget" in low and ("exceed" in low or "reached" in low):
        return LLMError("budget", "Your AI key has used up its budget. It resets on the gateway's "
                        "schedule; ask whoever issued the key for more.", status=status, raw=said)
    if status == 429:
        wait = _retry_after(headers, said)
        what = "tokens per minute" if re.search(r"\btpm\b|token", low) else \
            "requests per minute" if re.search(r"\brpm\b|request", low) else "rate limit"
        return LLMError(
            "limit",
            f"Your AI key has reached its {what}." + (f" It frees up in about {wait} seconds." if wait else
                                                      " Wait a minute and ask again."),
            status=status, retry_after=wait, raw=said,
        )
    if re.search(r"not allowed to access model|team not allowed|key not allowed|invalid model name", low):
        return LLMError("model_denied", f"Your AI key may not use{which}. Pick another model, or ask "
                        "for access to this one.", status=status, raw=said)
    if status == 401:
        return LLMError("key", "The AI service did not accept your key. It may have expired or been "
                        "revoked: connect a new one on the DevBot page or on Connections.", status=status, raw=said)
    if status == 403:
        return LLMError("model_denied", f"The AI service refused your key for{which}: {said}", status=status, raw=said)
    if status == 404:
        return LLMError("model_missing", f"The AI service does not have{which}. Pick another model.",
                        status=status, raw=said)
    if status in (400, 422):
        if re.search(r"tool-call-parser|auto-tool-choice|tool_choice|does not support tools|tools are not supported", low):
            return LLMError("tools", f"{model or 'This model'} cannot read live data: its server is not set up "
                            "for tool calling.", status=status, raw=said)
        if re.search(r"context length|maximum context|too many tokens|prompt is too long|max_tokens|input length", low):
            err = LLMError("context", "The conversation is too long for this model.", status=status, raw=said)
            stated = re.search(r"maximum (?:context length|input length|model length) is (\d+)", low)
            err.context_tokens = int(stated.group(1)) if stated else None
            return err
        return LLMError("request", f"The AI service refused the request: {said}", status=status, raw=said)
    if status >= 500:
        return LLMError("server", f"The AI service failed to answer ({status}): {said or 'no details'}",
                        status=status, raw=said)
    return LLMError("request", f"The AI service answered {status}: {said}", status=status, raw=said)


def _network_error(exc: Exception) -> LLMError:
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return LLMError("timeout", "The model took too long to answer. Ask again, or pick a faster model.")
    return LLMError("network", explain_integration_failure(GATEWAY, exc))


def is_embedding(model_id: str, mode: str = "") -> bool:
    if mode:
        return mode.lower() not in ("chat", "completion", "responses")
    return bool(_EMBEDDING_NAME.search(model_id or ""))


# ── what the key may use ─────────────────────────────────────────────────────

def list_models(key: str, embedding: bool = False) -> List[Dict[str, Any]]:
    """The models this key may use, described as far as the gateway describes them.

    /models is the list; LiteLLM's /model/info adds the mode (chat or embedding) and the
    context window. The second is best-effort: not every gateway exposes it, and a model
    it does not describe gets the configured default window.

    Chat models by default; embedding=True gives the embedding models instead, which is
    what the page search index is built with.
    """
    base = base_url()
    try:
        with resilient_http.Client(timeout=_timeout(20.0), headers=_headers(key)) as client:
            resp = client.get(f"{base}/models")
            if resp.status_code != 200:
                raise classify(resp.status_code, resp.text, resp.headers)
            if "json" not in (resp.headers.get("content-type") or ""):
                raise LLMError("request", "The AI service answered with a web page instead of a model list. "
                               "The configured address is probably not its API.")
            ids = [str(m.get("id")) for m in (resp.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
            info: Dict[str, Dict[str, Any]] = {}
            for url in (f"{base}/model/info", f"{_origin(base)}/model/info"):
                try:
                    more = client.get(url)
                except httpx.HTTPError:
                    continue
                if more.status_code == 200 and "json" in (more.headers.get("content-type") or ""):
                    for row in more.json().get("data") or []:
                        if not isinstance(row, dict):
                            continue
                        name = str(row.get("model_name") or "")
                        details = row.get("model_info") if isinstance(row.get("model_info"), dict) else {}
                        if name and name not in info:
                            info[name] = details
                    break
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    models = []
    for model_id in ids:
        details = info.get(model_id) or {}
        mode = str(details.get("mode") or "")
        if is_embedding(model_id, mode) != embedding:
            continue

        def number(*names: str) -> Optional[int]:
            for name in names:
                try:
                    value = int(details.get(name) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
            return None

        models.append({
            "id": model_id,
            "context": number("max_input_tokens", "max_tokens"),
            "max_output": number("max_output_tokens"),
        })
    return models


def embed(key: str, model: str, texts: List[str], timeout: float = 60.0) -> Tuple[List[List[float]], Dict[str, Any]]:
    """Vectors for ``texts``, in order, and the limits the gateway reported on the answer.

    Raises LLMError, classified like a chat failure: a 429 carries its retry_after, so a
    long indexing run can wait for the minute to turn instead of giving up.
    """
    base = base_url()
    try:
        with resilient_http.Client(timeout=_timeout(timeout), headers=_headers(key)) as client:
            resp = client.post(f"{base}/embeddings", json={"model": model, "input": texts})
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    if resp.status_code != 200:
        raise classify(resp.status_code, resp.text, resp.headers, model)
    try:
        rows = resp.json().get("data") or []
    except ValueError:
        raise LLMError("request", "The AI service answered the embeddings request with something that is not JSON.")
    ordered = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: int(r.get("index") or 0))
    vectors = [[float(x) for x in (r.get("embedding") or [])] for r in ordered]
    if len(vectors) != len(texts) or any(not v for v in vectors):
        raise LLMError("request", f"The AI service returned {len(vectors)} vectors for {len(texts)} passages.")
    return vectors, read_limits(resp.headers)


_THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


def complete(key: str, model: str, messages: List[Dict[str, Any]], max_tokens: int = 800,
             timeout: float = 120.0) -> Tuple[str, Dict[str, Any]]:
    """One answer, whole, outside any conversation: for work the Hub does in the
    background (reviewing a fix before the index keeps it). Returns the answer's text,
    without any thinking the model wrote into it, and the limits the gateway reported.
    Raises LLMError, classified like a chat failure."""
    base = base_url()
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
    try:
        with resilient_http.Client(timeout=_timeout(timeout), headers=_headers(key)) as client:
            resp = client.post(f"{base}/chat/completions", json=body)
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    if resp.status_code != 200:
        raise classify(resp.status_code, resp.text, resp.headers, model)
    try:
        choice = (resp.json().get("choices") or [{}])[0]
    except ValueError:
        raise LLMError("request", "The AI service answered with something that is not JSON.")
    text = str((choice.get("message") or {}).get("content") or "")
    return _THINK.sub("", text).strip(), read_limits(resp.headers)


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _prefix(base: str) -> str:
    """The gateway's root, keeping any path it is mounted under: /key/info lives beside
    /v1, not inside it, and not necessarily at the host's root."""
    path = urlsplit(base).path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return _origin(base) + path


def _get_json(client: httpx.Client, urls: List[str]) -> Optional[Dict[str, Any]]:
    for url in urls:
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200 and "json" in (resp.headers.get("content-type") or ""):
            try:
                body = resp.json()
            except ValueError:
                continue
            if isinstance(body, dict):
                return body
    return None


def _limits_of(row: Any) -> Dict[str, float]:
    """rpm / tpm / budget from one LiteLLM record: a key, its budget table, a team, a user."""
    if not isinstance(row, dict):
        return {}
    out = {"rpm": _num(row.get("rpm_limit")), "tpm": _num(row.get("tpm_limit")),
           "budget": _num(row.get("max_budget"))}
    return {k: v for k, v in out.items() if v}


def _per_model(row: Dict[str, Any], name: str) -> Dict[str, int]:
    """model_rpm_limit / model_tpm_limit: {model: n}, on the key or in its metadata."""
    out: Dict[str, int] = {}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for source in (row, metadata):
        value = source.get(name)
        if isinstance(value, dict):
            for model, n in value.items():
                number = _num(n)
                if number:
                    out[str(model)] = int(number)
    return out


def key_info(key: str) -> Dict[str, Any]:
    """What the gateway says about this key: its limits, budget and expiry.

    LiteLLM answers /key/info for the key that asks. A limit need not sit on the key
    itself: it can be per model on the key, in a budget the key is linked to, or on the
    key's team or user, and a key with none of its own obeys those. They are read in
    that order and the first that sets a limit is reported, with where it came from.

    `described` says the gateway answered at all, which is what separates "this key has
    no limits" from "the gateway did not say".
    """
    try:
        root = _prefix(base_url())
    except LLMError:
        return {}
    roots = [root] if root == _origin(root) else [root, _origin(root)]
    try:
        with resilient_http.Client(timeout=_timeout(10.0), headers=_headers(key)) as client:
            body = _get_json(client, [r + "/key/info" for r in roots])
            info = (body or {}).get("info")
            if not isinstance(info, dict):
                return {}
            limits = _limits_of(info)
            source = "key" if limits.get("rpm") or limits.get("tpm") else ""
            if not source:
                table = _limits_of(info.get("litellm_budget_table"))
                if table.get("rpm") or table.get("tpm"):
                    limits, source = {**limits, **table}, "budget"
            if not source and info.get("team_id"):
                team = _get_json(client, [f"{r}/team/info?team_id={quote(str(info['team_id']), safe='')}" for r in roots])
                found = _limits_of((team or {}).get("team_info") or team)
                if found.get("rpm") or found.get("tpm"):
                    limits, source = {**found, **limits}, "team"
            if not source and info.get("user_id"):
                user = _get_json(client, [r + "/user/info" for r in roots])
                found = _limits_of((user or {}).get("user_info"))
                if found.get("rpm") or found.get("tpm"):
                    limits, source = {**found, **limits}, "user"
    except httpx.HTTPError:
        return {}

    out: Dict[str, Any] = {"described": True}
    for name in ("rpm", "tpm"):
        if limits.get(name):
            out[name] = int(limits[name])
    if source:
        out["source"] = source
    model_rpm, model_tpm = _per_model(info, "model_rpm_limit"), _per_model(info, "model_tpm_limit")
    if model_rpm:
        out["model_rpm"] = model_rpm
    if model_tpm:
        out["model_tpm"] = model_tpm
    extra = {
        "parallel": _num(info.get("max_parallel_requests")),
        "budget": _num(info.get("max_budget")) or limits.get("budget"),
        "spend": _num(info.get("spend")),
        "budget_resets": str(info.get("budget_reset_at") or ""),
        "expires": str(info.get("expires") or ""),
        "name": str(info.get("key_alias") or info.get("key_name") or ""),
    }
    for k, v in extra.items():
        if v in (None, ""):
            continue
        out[k] = int(v) if k == "parallel" else v
    return out


def limits_for(info: Dict[str, Any], model_id: str) -> Dict[str, int]:
    """The rpm / tpm that apply to one model: a per-model limit beats the key's own."""
    out: Dict[str, int] = {}
    for name in ("rpm", "tpm"):
        per_model = info.get("model_" + name)
        value = per_model.get(model_id) if isinstance(per_model, dict) else None
        if value or info.get(name):
            out[name] = int(value or info[name])
    return out


def validate_key(key: str) -> int:
    """How many chat models the key may use. Raises LLMError when the key is refused."""
    models = list_models(key)
    if not models:
        raise LLMError("model_denied", "The AI service accepted the key, but it may not use any chat model. "
                       "Ask whoever issued it for access to one.")
    return len(models)


# ── one model call ───────────────────────────────────────────────────────────

class _ThinkSplitter:
    """Separates <think>...</think> from the answer when a server leaves it in the text.

    With a reasoning parser, vLLM sends the thinking as reasoning_content. Without one,
    Qwen-style models write it into the answer itself. Streamed, a tag can arrive split
    across chunks, so the text is held back only while it could still be the start of one.
    """

    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, text: str) -> List[tuple]:
        self.pending += text
        out: List[tuple] = []
        while self.pending:
            tag = "</think>" if self.inside else "<think>"
            at = self.pending.find(tag)
            if at >= 0:
                if at:
                    out.append(("reasoning" if self.inside else "content", self.pending[:at]))
                self.pending = self.pending[at + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0
            for n in range(min(len(tag) - 1, len(self.pending)), 0, -1):
                if tag.startswith(self.pending[-n:]):
                    keep = n
                    break
            emit = self.pending[:len(self.pending) - keep]
            if emit:
                out.append(("reasoning" if self.inside else "content", emit))
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return out

    def flush(self) -> List[tuple]:
        rest, self.pending = self.pending, ""
        return [("reasoning" if self.inside else "content", rest)] if rest else []


async def chat(
    key: str,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 1500,
    stream: bool = True,
    read_timeout: float = 180.0,
) -> AsyncIterator[Dict[str, Any]]:
    """One request to the model, as events.

    Yields ``{"type": "reasoning"|"content", "text"}`` while the model writes, then one
    ``{"type": "end", "tool_calls", "finish_reason", "usage", "limits", "text_tool_call"}``.
    Raises LLMError for anything that is not an answer.
    """
    url = f"{base_url()}/chat/completions"
    body: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

    splitter = _ThinkSplitter()
    calls: Dict[int, Dict[str, Any]] = {}
    content_seen: List[str] = []
    finish = ""
    usage: Dict[str, Any] = {}

    def emit(pieces: List[tuple]) -> List[Dict[str, Any]]:
        events = []
        for kind, text in pieces:
            if not text:
                continue
            if kind == "content":
                content_seen.append(text)
            events.append({"type": kind, "text": text})
        return events

    def take_calls(raw_calls: Any) -> None:
        for position, call in enumerate(raw_calls or []):
            if not isinstance(call, dict):
                continue
            index = call.get("index", position)
            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = str(call["id"])
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] += str(fn["name"]) if not slot["name"].endswith(str(fn["name"])) else ""
            if fn.get("arguments"):
                args = fn["arguments"]
                slot["arguments"] += args if isinstance(args, str) else json.dumps(args)

    try:
        async with resilient_http.AsyncClient(timeout=_timeout(read_timeout)) as client:
            async with client.stream("POST", url, json=body, headers=_headers(key)) as resp:
                limits = read_limits(resp.headers)
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise classify(resp.status_code, text, resp.headers, model)

                if not stream:
                    data = json.loads((await resp.aread()).decode("utf-8", "replace") or "{}")
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    finish = str(choice.get("finish_reason") or "")
                    usage = data.get("usage") or {}
                    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                    if reasoning:
                        yield {"type": "reasoning", "text": str(reasoning)}
                    for event in emit(splitter.feed(str(message.get("content") or "")) + splitter.flush()):
                        yield event
                    take_calls(message.get("tool_calls"))
                else:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(chunk.get("error"), (dict, str)):
                            raise classify(500, payload, resp.headers, model)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                yield {"type": "reasoning", "text": str(reasoning)}
                            if delta.get("content"):
                                for event in emit(splitter.feed(str(delta["content"]))):
                                    yield event
                            if delta.get("tool_calls"):
                                take_calls(delta["tool_calls"])
                            if choice.get("finish_reason"):
                                finish = str(choice["finish_reason"])
                    for event in emit(splitter.flush()):
                        yield event
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc

    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot["name"]:
            continue
        tool_calls.append({
            "id": slot["id"] or f"call_{index}",
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
        })
    written = "".join(content_seen)
    yield {
        "type": "end",
        "tool_calls": tool_calls,
        "finish_reason": finish,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        },
        "limits": limits,
        "text_tool_call": bool(tools) and not tool_calls and wrote_tool_call_as_text(
            written, [str((t.get("function") or {}).get("name") or "") for t in tools or []]),
    }


PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}


async def probe_tools(key: str, model: str) -> Dict[str, Any]:
    """Can this model call tools through this gateway? One small request.

    The same test scripts/check_llm_endpoint.py runs from a pod: a dummy tool and a
    question that needs it. A proper tool call is a yes; a refusal naming the tool
    flags, or the call written out as text, is a no.
    """
    messages = [{"role": "user", "content": "What is the weather in Haifa right now? Use the get_weather tool."}]
    try:
        end: Dict[str, Any] = {}
        async for event in chat(key, model, messages, tools=[PROBE_TOOL], max_tokens=600, stream=False, read_timeout=90.0):
            if event["type"] == "end":
                end = event
    except LLMError as exc:
        if exc.kind == "tools":
            return {"tools": False, "detail": exc.message}
        raise
    names = [c["function"]["name"] for c in end.get("tool_calls") or []]
    if "get_weather" in names:
        return {"tools": True, "detail": "Calls tools, so it can read live data."}
    if end.get("text_tool_call"):
        return {"tools": False, "detail": "It tried to call the tool, but its server does not pass tool calls on."}
    if end.get("finish_reason") == "length":
        return {"tools": None, "detail": "It ran out of room before answering. Check it again."}
    return {"tools": False, "detail": "It answered without calling the tool it was given."}
