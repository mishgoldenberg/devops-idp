r"""Check an OpenAI-compatible model endpoint from inside a pod: DNS, firewall, TLS, auth, tool calling.

Pipe it into the TEST backend from PowerShell (the image already has Python and httpx):

    Get-Content -Raw .\scripts\check_llm_endpoint.py | oc exec -i -n devops-hub deploy/backend-test -- python - https://<model-gateway>

With an API key, and/or only one model instead of every model the server lists:

    ... -- env LLM_API_KEY=<key> python - https://<model-gateway> openai/gpt-oss-120b

Keep this file ASCII: Windows PowerShell pipes text to native programs as ASCII.
"""
import json
import os
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx

sys.stdout.reconfigure(errors="replace")

TOOL = {
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
QUESTION = "What is the weather in Haifa right now? Use the get_weather tool."
FAKE_RESULT = {"city": "Haifa", "temp_c": 23, "condition": "sunny"}
PARSER_HINT = (
    "the server is not turning the model's output into tool_calls. On vLLM it must be started with "
    "--enable-auto-tool-choice AND the --tool-call-parser for this model family "
    "(e.g. gpt-oss: openai, Llama 3.x: llama3_json, Qwen/Hermes: hermes, Mistral: mistral)."
)
MAX_MODELS = 8


def say(tag, text):
    print(f"      {tag:5} {text}", flush=True)


def pod_namespace():
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except OSError:
        return "this namespace"


def snippet(text, n=200):
    return repr((text or "")[:n])


def network_checks(host, port, scheme, has_proxy):
    """Return (ok, verify). verify is False when TLS works but the CA is not trusted."""
    print(f"[1] DNS {host}", flush=True)
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        say("OK", "resolves to " + ", ".join(ips))
    except socket.gaierror as exc:
        say("FAIL", f"the pod cannot resolve this name ({exc}). Check the hostname, or ask for a DNS entry.")
        return False, True

    print(f"[2] TCP {host}:{port}", flush=True)
    try:
        socket.create_connection((host, port), timeout=8).close()
        say("OK", "port is open from this pod")
    except TimeoutError:
        say("FAIL", f"no answer in 8s: a firewall or the egress policy of {pod_namespace()} drops the traffic. "
                    f"Ask the network team to open {pod_namespace()} -> {host}:{port}.")
        return has_proxy, True
    except ConnectionRefusedError:
        say("FAIL", "connection refused: the host is reachable but nothing listens on this port.")
        return has_proxy, True
    except OSError as exc:
        say("FAIL", f"cannot connect: {exc}")
        return has_proxy, True

    if scheme != "https":
        return True, True
    print("[3] TLS", flush=True)
    try:
        with socket.create_connection((host, port), timeout=8) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                issuer = dict(item[0] for item in tls.getpeercert().get("issuer", ()))
                say("OK", f"certificate trusted ({tls.version()}), issued by "
                          f"{issuer.get('organizationName') or issuer.get('commonName') or '?'}")
                return True, True
    except ssl.SSLCertVerificationError as exc:
        say("WARN", f"certificate NOT trusted by this image ({exc.verify_message}). The server is reachable; "
                    "the agent will need the internal CA bundle. Continuing WITHOUT verification.")
        return True, False
    except (ssl.SSLError, OSError) as exc:
        say("FAIL", f"TLS handshake failed: {exc}")
        return False, True


def find_api(client, base):
    print("[4] API", flush=True)
    candidates = [base] if base.endswith("/v1") else [base + "/v1", base]
    for api in candidates:
        try:
            r = client.get(api + "/models")
        except httpx.HTTPError as exc:
            say("FAIL", f"GET {api}/models: {type(exc).__name__}: {exc}")
            return None, []
        kind = r.headers.get("content-type", "")
        if r.status_code in (401, 403):
            hint = "set LLM_API_KEY" if "Authorization" not in client.headers else "the key was refused"
            say("FAIL", f"GET {api}/models -> HTTP {r.status_code}: {hint}. Server said: {snippet(r.text)}")
            return None, []
        if r.status_code == 200 and "json" in kind:
            models = r.json().get("data") or []
            say("OK", f"GET {api}/models -> HTTP 200, {len(models)} model(s)")
            return api, models
        say("--", f"GET {api}/models -> HTTP {r.status_code} ({kind or 'no content-type'}), not the API")
    say("FAIL", "no OpenAI-compatible /models found. Ask for the exact base URL (it usually ends in /v1).")
    return None, []


def get_json(client, url):
    try:
        r = client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
        return r.json()
    return None


def describe_server(client, api, models):
    """What the gateway says about its models and about this key. Returns the mode
    (chat / embedding) and context window per model, as far as it says."""
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(api))
    version = get_json(client, origin + "/version")
    if version:
        say("", f"version endpoint says {version}")
    info = {}
    for url in (api + "/model/info", origin + "/model/info"):
        body = get_json(client, url)
        if body and isinstance(body.get("data"), list):
            for row in body["data"]:
                details = row.get("model_info") or {}
                info.setdefault(row.get("model_name"), {"mode": details.get("mode") or "",
                                                         "context": details.get("max_input_tokens") or details.get("max_tokens")})
            break
    for m in models:
        mid = m.get("id")
        extra = info.get(mid) or {}
        context = extra.get("context") or m.get("max_model_len")
        say("", f"model {mid!r} (mode {extra.get('mode') or '?'}, context {context or 'not reported'})")
    key = get_json(client, origin + "/key/info")
    details = (key or {}).get("info") or {}
    if details:
        say("", "key limits: requests/min %s, tokens/min %s, parallel %s, budget %s (spent %s), expires %s" % (
            details.get("rpm_limit"), details.get("tpm_limit"), details.get("max_parallel_requests"),
            details.get("max_budget"), details.get("spend"), details.get("expires")))
    else:
        say("", "key limits: the gateway did not describe this key (/key/info)")
    return info


def is_embedding(model_id, info):
    mode = (info.get(model_id) or {}).get("mode") or ""
    if mode:
        return mode.lower() not in ("chat", "completion", "responses")
    return bool(re.search(r"embed|(^|[/_-])e5([-_]|$)|bge|rerank|whisper|tts", model_id or "", re.I))


def stream_test(client, api, model):
    """The same tool call, STREAMED: DevBot streams every answer, so a server that only
    gets tool calls right when not streaming would still leave it unable to read data."""
    body = {"model": model, "messages": [{"role": "user", "content": QUESTION}], "tools": [TOOL],
            "tool_choice": "auto", "temperature": 0, "max_tokens": 1024, "stream": True}
    name, args, text = "", "", ""
    try:
        with client.stream("POST", api + "/chat/completions", json=body) as r:
            if r.status_code != 200:
                return f"stream FAIL (HTTP {r.status_code}: {snippet(r.read().decode('utf-8', 'replace'))})"
            for line in r.iter_lines():
                line = line.strip()
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text += delta.get("content") or ""
                    for call in delta.get("tool_calls") or []:
                        fn = call.get("function") or {}
                        if fn.get("name") and not name:
                            name = fn["name"]
                        args += fn.get("arguments") or ""
    except httpx.HTTPError as exc:
        return f"stream FAIL ({type(exc).__name__}: {exc})"
    if name == "get_weather":
        try:
            json.loads(args or "{}")
            return "stream OK"
        except ValueError:
            return f"stream FAIL (arguments arrived broken: {snippet(args)})"
    return f"stream FAIL (no tool call while streaming; wrote {snippet(text, 120)}) -> set DEVBOT_STREAM=false"


def tool_test(client, api, model):
    messages = [{"role": "user", "content": QUESTION}]
    body = {"model": model, "messages": messages, "tools": [TOOL], "tool_choice": "auto",
            "temperature": 0, "max_tokens": 1024}
    started = time.monotonic()
    try:
        r = client.post(api + "/chat/completions", json=body)
    except httpx.HTTPError as exc:
        return "FAIL", f"request failed after {time.monotonic() - started:.0f}s: {type(exc).__name__}: {exc}"
    took = f"{time.monotonic() - started:.1f}s"
    if r.status_code != 200:
        text = r.text[:400]
        if "tool-choice" in text or "tool-call-parser" in text or "tool_choice" in text:
            return "FAIL", f"HTTP {r.status_code}: {snippet(text, 400)} -> {PARSER_HINT}"
        return "FAIL", f"HTTP {r.status_code}: {snippet(text, 400)}"
    choice = (r.json().get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = msg.get("tool_calls") or []
    content = msg.get("content") or ""
    if not calls:
        if choice.get("finish_reason") == "length":
            return "UNSURE", f"ran out of max_tokens before answering ({took})"
        if any(s in content for s in ("get_weather", "<tool_call>", "<|call|>", "[TOOL_CALLS]", '"name"')):
            return "FAIL", f"the model wrote the call as TEXT: {snippet(content)} -> {PARSER_HINT}"
        return "FAIL", f"answered without calling the tool: {snippet(content)} -> the model ignores tools, or {PARSER_HINT}"
    fn = calls[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "")
    except ValueError:
        return "FAIL", f"tool_calls returned, but the arguments are not JSON: {snippet(fn.get('arguments'))}"
    if fn.get("name") != "get_weather":
        return "FAIL", f"called an unknown tool {fn.get('name')!r}"

    # Round two: hand the result back, the way the agent loop will.
    messages += [
        {"role": "assistant", "content": content or None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": calls[0].get("id") or "call_0", "content": json.dumps(FAKE_RESULT)},
    ]
    try:
        r2 = client.post(api + "/chat/completions", json=body)
    except httpx.HTTPError as exc:
        return "PARTIAL", f"called get_weather({args}), then the follow-up failed: {type(exc).__name__}: {exc}"
    if r2.status_code != 200:
        return "PARTIAL", f"called get_weather({args}), but the tool result was rejected: HTTP {r2.status_code} {snippet(r2.text)}"
    answer = ((r2.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if "23" in answer:
        return "PASS", f"called get_weather({args}) in {took} and answered from the result: {snippet(answer, 120)}"
    return "PARTIAL", f"called get_weather({args}), but the final answer ignores the result: {snippet(answer)}"


def main():
    if len(sys.argv) < 2:
        print("usage: python - <base_url> [model_id]    (API key in env LLM_API_KEY)")
        return 2
    base = sys.argv[1].strip().rstrip("/")
    only = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    key = os.environ.get("LLM_API_KEY", "").strip()
    parts = urlsplit(base)
    host, scheme = parts.hostname, parts.scheme
    port = parts.port or (443 if scheme == "https" else 80)

    proxies = sorted(k for k in os.environ if k.lower() in ("https_proxy", "http_proxy", "all_proxy", "no_proxy"))
    print(f"Target {base}   API key: {'set, %d chars' % len(key) if key else 'not set'}   "
          f"proxy env: {', '.join(proxies) or 'none'}", flush=True)

    ok, verify = network_checks(host, port, scheme, bool(proxies))
    if not ok:
        return 1

    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(verify=verify, headers=headers, timeout=httpx.Timeout(180, connect=10)) as client:
        api, models = find_api(client, base)
        if not api:
            return 1
        info = describe_server(client, api, models)

        ids = [only] if only else [m.get("id") for m in models if m.get("id")][:MAX_MODELS]
        print("[5] Tool calling (a dummy tool, the result sent back, then the same call streamed)", flush=True)
        results = []
        for model in ids:
            print(f"    {model}", flush=True)
            if is_embedding(model, info) and not only:
                say("SKIP", "an embedding model: it cannot chat, so DevBot never offers it")
                results.append((model, "SKIP"))
                continue
            verdict, detail = tool_test(client, api, model)
            streamed = ""
            if verdict == "PASS":
                streamed = stream_test(client, api, model)
                detail += "; " + streamed
            say(verdict, detail)
            results.append((model, verdict + ("  (" + streamed.split(" (")[0] + ")" if streamed else "")))

    print("\nSummary", flush=True)
    for model, verdict in results:
        print(f"  {verdict:24} {model}")
    return 0 if any(v.startswith("PASS") for _, v in results) else 1


if __name__ == "__main__":
    sys.exit(main())
