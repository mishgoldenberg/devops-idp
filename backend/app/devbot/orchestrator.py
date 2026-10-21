"""
One question, start to finish, as a stream of events for the page.

  start      the conversation (new or existing) and the model answering
  notice     something the person should know: a limit, a model that cannot read live
             data, earlier messages left out to fit
  reasoning  the model thinking, when it shows its thinking
  interim    text the model wrote before deciding to look something up
  step       a lookup starting and finishing, with its system and outcome
  action     a change proposed for the person to review and confirm
  delta      the answer, as it is written
  done       the saved answer, with what it cost and what it read
  error      why there is no answer

Runs in the event loop and never blocks it: the model is called with an async client,
and the database and the tools (which reuse the Hub's own synchronous integration
code) run on the thread pool. A slow model therefore holds a connection, not a thread.

AdminBot (bot="admin") is the same loop over the Hub's own records, for admins: the
Hub's AI key instead of the person's, the tools of system "hub" instead of DevBot's,
its own prompt, and its conversations kept apart from DevBot's.

The per-minute limits shape the loop. Every model call is one request against the
person's key, so a question makes at most ``plan.rounds`` of them and the LAST one is
always made without tools -- a question ends in an answer, never in a lookup that ran
out of room. A rate limit that frees up within half a minute is waited out once; one
that does not ends the question with whatever was found, rather than with nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import date
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from fastapi.concurrency import run_in_threadpool

from redis_client import get_redis
from security import AuthUser

from . import budget, config, grounding, knowledge, llm, models, monitor, prompts, store
from .tools import base as tools

log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 10
LIMIT_WAIT_MAX = 30
_LIMITS_TTL = 900
# Changes one answer, and one conversation, may put in front of the person. A model
# stuck in a loop would otherwise fill the page with cards nobody asked for; a bulk
# card (several requests at once) counts as one. Offers an investigation makes by
# itself ("suggested") do not count: they are the obvious next steps, not requests.
MAX_PROPOSALS_PER_ANSWER = 5
MAX_PROPOSALS_PER_CONVERSATION = 30


def proposals_so_far(rows: List[Dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("role") == "assistant"
               for a in (r.get("meta") or {}).get("actions") or [] if not a.get("suggested"))


def room_for_proposals(state: "_Turn") -> int:
    asked = sum(1 for a in state.actions if not a.get("suggested"))
    return max(0, min(MAX_PROPOSALS_PER_ANSWER - asked,
                      MAX_PROPOSALS_PER_CONVERSATION - state.proposed_before - asked))

_active = 0
_per_user: Dict[str, int] = {}


def sse(event: Dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


def user_id(user: AuthUser) -> str:
    return str(user.get("id") or "").strip()


def display_name(user: AuthUser) -> str:
    name = str(user.get("username") or user.get("email") or "the user")
    local = name.split("@", 1)[0].split("\\")[-1].replace(".", " ").replace("_", " ")
    return " ".join(p.capitalize() for p in local.split()) or name


# ── limits the gateway last reported, per person ─────────────────────────────

def last_limits(uid: str) -> Dict[str, Any]:
    try:
        raw = get_redis().get(f"devbot:limits:{uid}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_limits(uid: str, limits: Dict[str, Any]) -> None:
    if not limits:
        return
    try:
        merged = {**last_limits(uid), **limits, "at": int(time.time())}
        get_redis().setex(f"devbot:limits:{uid}", _LIMITS_TTL, json.dumps(merged))
    except Exception:
        pass


# ── the stored conversation, as the model reads it ───────────────────────────

def to_model_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stored messages as chat messages, with every tool call paired with its result.

    A question stopped half-way can leave a call without its result (or the reverse),
    and the gateway refuses a conversation like that outright -- so unpaired ones are
    left out rather than sent.
    """
    out: List[Dict[str, Any]] = []
    results = {str((r.get("meta") or {}).get("call_id") or "") for r in rows if r["role"] == "tool"}
    asked: set = set()
    for row in rows:
        meta = row.get("meta") or {}
        role = row["role"]
        if role == "user":
            out.append({"role": "user", "content": row["content"]})
        elif role == "assistant" and meta.get("tool_calls"):
            calls = [c for c in meta["tool_calls"] if str(c.get("id")) in results]
            if calls:
                asked.update(str(c.get("id")) for c in calls)
                out.append({"role": "assistant", "content": row["content"] or None, "tool_calls": calls})
        elif role == "assistant" and meta.get("error"):
            # A question that failed was not answered: leave both out, or asking again
            # hands the model the same question twice.
            if out and out[-1]["role"] == "user":
                out.pop()
        elif role == "assistant":
            content = row["content"] or ""
            # What became of the changes this answer proposed, so the model knows whether
            # the pipeline was re-run or the bug opened before it suggests it again.
            ended = [a for a in meta.get("actions") or [] if a.get("status") in ("done", "simulated", "declined")]
            if ended:
                content += "\n\n(" + "; ".join(
                    f"{a.get('title')}: " + ("declined by the person" if a["status"] == "declined" else
                                             "simulated only, Safe Mode is on" if a["status"] == "simulated" else
                                             "done -- " + str(a.get("result") or "confirmed"))
                    for a in ended) + ")"
            if content:
                out.append({"role": "assistant", "content": content})
        elif role == "tool":
            call_id = str(meta.get("call_id") or "")
            if call_id in asked:
                out.append({"role": "tool", "tool_call_id": call_id, "content": row["content"]})
    return out


def recent_systems(rows: List[Dict[str, Any]]) -> List[str]:
    """Systems the previous answer read, so a follow-up keeps its tools."""
    for row in reversed(rows[:-1]):
        steps = (row.get("meta") or {}).get("steps")
        if row["role"] == "assistant" and steps:
            return sorted({s.get("system") for s in steps if s.get("system")})
    return []


def system_states(uid: str) -> Dict[str, str]:
    """Each system as the page describes it: connected, public, missing or unavailable.

    ``public`` is SonarQube without a token: this instance serves public projects to
    anybody, which is how its widgets work without one, so DevBot can still read those
    -- and says that it is only those. ``unavailable`` means the Hub has no address for
    the system, so there is nothing a person could connect.
    """
    from api.azure_devops import ADO_BASE
    from api.integrations import _base_url, _get_token
    from secrets_manager import get_user_azure_devops_pat

    def configured(system: str) -> bool:
        try:
            _base_url(system)
            return True
        except Exception:
            return False

    states: Dict[str, str] = {}
    try:
        has_pat = bool(get_user_azure_devops_pat(uid))
    except Exception:
        has_pat = False
    states["azure"] = "unavailable" if not ADO_BASE else ("connected" if has_pat else "missing")
    for system in ("sonarqube", "artifactory", "confluence"):
        if not configured(system):
            states[system] = "unavailable"
            continue
        try:
            has_token = bool(_get_token(uid, system))
        except Exception:
            has_token = False
        states[system] = "connected" if has_token else ("public" if system == "sonarqube" else "missing")
    # Support tickets need no token of the person's: the Hub reads them with its own
    # account and shows each person only theirs (tools/support.py).
    states["servicenow"] = "connected" if knowledge.snow_available() else "unavailable"
    return states


def connected_systems(uid: str) -> Dict[str, bool]:
    """Which systems this person can be answered from."""
    out = {system: state in ("connected", "public") for system, state in system_states(uid).items()}
    # The Hub's own forms need no connection: everyone can be handed a draft of one.
    out["requests"] = True
    return out


def model_key(uid: str) -> str:
    from api.integrations import _get_token

    try:
        return _get_token(uid, "devbot") or ""
    except Exception:
        return ""


def hub_key() -> str:
    """The Hub's own AI key (Platform Managing -> DevBot search index), which AdminBot
    answers with: its questions are the Hub's business, not the admin's own budget."""
    try:
        return knowledge.settings(secrets=True)["gateway_key"]
    except Exception:
        return ""


def still_admin(user: AuthUser) -> bool:
    from security import has_effective_admin_access_live

    try:
        return bool(has_effective_admin_access_live(user))
    except Exception:
        return False


# ── streaming ────────────────────────────────────────────────────────────────

async def stream_turn(user: AuthUser, message: str, conversation_id: str = "", model_id: str = "",
                      bot: str = "devbot") -> AsyncIterator[str]:
    """The events of one question, with a keep-alive while nothing else is said.

    Proxies close a connection that stays silent for long enough, and a model that is
    thinking, or a pipeline log being read, can be silent for longer than that.
    """
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
    worker = asyncio.create_task(_turn(user, message, conversation_id, model_id, queue.put_nowait, bot))
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            yield sse(event)
    finally:
        if not worker.done():
            worker.cancel()
            try:
                await worker
            except BaseException:
                pass


class _Turn:
    """Everything one question accumulates on its way to an answer."""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.sources: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []
        self.notices: List[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.requests = 0
        self.limits: Dict[str, Any] = {}
        self.answer = ""
        self.reasoning_chars = 0
        self.conv_id = ""
        self.failed = False
        self.error: Dict[str, Any] = {}
        self.question_saved = False
        self.stopped = False
        # For the monitoring page (monitor.py): how the question ended and what it took.
        self.limit_waits = 0
        self.found_only = False
        self.message_id: Optional[int] = None
        self.model_id = ""
        # An investigation that found no page and no past fix leaves this; the finished
        # answer is then offered as a new Confluence page (save_fix_action).
        self.save_fix: Dict[str, Any] = {}
        # Whose limits the gateway's figures are: the person's key, or the Hub's.
        self.limits_owner = ""
        # Proposals already made earlier in this conversation (room_for_proposals).
        self.proposed_before = 0

    def add_source(self, link: Dict[str, Any]) -> None:
        url = str(link.get("url") or "")
        if url and all(s.get("url") != url for s in self.sources) and len(self.sources) < 24:
            self.sources.append({"title": str(link.get("title") or url)[:160], "url": url,
                                 "system": str(link.get("system") or "")})


async def _turn(user: AuthUser, message: str, conversation_id: str, model_id: str,
                emit: Callable[[Optional[Dict[str, Any]]], None], bot: str = "devbot") -> None:
    global _active
    uid = user_id(user)
    admin = bot == "admin"
    started = time.monotonic()
    state = _Turn()
    state.limits_owner = "hub" if admin else uid
    conv_id = ""
    slot = False

    def notice(text: str, level: str = "info") -> None:
        if text not in state.notices:
            state.notices.append(text)
            emit({"type": "notice", "level": level, "text": text})

    def fail(kind: str, text: str, **extra: Any) -> None:
        state.error = {"kind": kind, "message": text, **extra}
        emit({"type": "error", "kind": kind, "message": text, **extra})

    try:
        if not config.enabled():
            return fail("not_configured", "DevBot is not set up on this Hub yet. Ask a platform admin.")
        text = (message or "").strip()
        if not text:
            return fail("empty", "Type a question first.")
        if _active >= config.max_streams() or _per_user.get(uid, 0) >= 2:
            return fail("busy", "DevBot is answering too many questions right now. Try again in a moment.")
        _active += 1
        _per_user[uid] = _per_user.get(uid, 0) + 1
        slot = True

        if admin and not await run_in_threadpool(still_admin, user):
            return fail("forbidden", "AdminBot is for the Hub's admins.")
        key = await run_in_threadpool(hub_key if admin else model_key, *([] if admin else [uid]))
        if not key:
            if admin:
                return fail("no_key", "AdminBot answers with the Hub's AI key, which is not set. An admin sets it on "
                                      "Platform Managing, under DevBot search index.")
            return fail("no_key", "Connect your AI model key first: DevBot uses your own key to talk to the models.")

        try:
            available = await run_in_threadpool(models.available, key)
        except llm.LLMError as exc:
            return fail(exc.kind, exc.message)
        if not available:
            return fail("model_denied", "Your AI key may not use any chat model.")

        conversation = None
        if conversation_id:
            conversation = await run_in_threadpool(store.get_conversation, uid, conversation_id, bot)
            if not conversation:
                return fail("not_found", "That conversation does not exist any more.")
        if model_id and model_id not in {m["id"] for m in available}:
            return fail("model_denied", f"Your AI key may not use {models.label(model_id)}. Pick another model.")
        model = models.choose(available, model_id, (conversation or {}).get("model", ""))
        assert model is not None
        mid = model["id"]
        state.model_id = mid
        if conversation is None:
            title = " ".join(text.split())[:80]
            conversation = await run_in_threadpool(store.create_conversation, uid, title, mid, bot)
        conv_id = conversation["id"]
        state.conv_id = conv_id

        await run_in_threadpool(store.add_message, conv_id, "user", text, {})
        state.question_saved = True
        rows = await run_in_threadpool(store.messages, uid, conv_id, 400, bot)
        history = to_model_messages(rows)
        state.proposed_before = proposals_so_far(rows)
        systems = {"hub": True} if admin else await run_in_threadpool(connected_systems, uid)
        emit({"type": "start", "conversation_id": conv_id, "title": conversation.get("title", ""),
              "model": mid, "model_label": model["label"], "systems": systems})

        cap = models.capability(mid)
        if cap.get("tools") is False:
            offered = []
        elif admin:
            offered = tools.hub_specs()
        else:
            offered = tools.specs_for(text, systems, recent_systems(rows))
        if cap.get("tools") is False:
            notice(f"{model['label']} cannot read live data, so it answers from general knowledge only. "
                   "Pick a model marked Live data to ask about " + ("the Hub." if admin else "your systems."), "warn")

        # What the gateway said about the key stands in until a chat answer has reported
        # the live figures, which win: headers are what the gateway is enforcing now.
        described = llm.limits_for(await run_in_threadpool(models.key_info, key, False), mid)
        plan = budget.plan(int(model["context"]), model.get("max_output"),
                           {**described, **await run_in_threadpool(last_limits, state.limits_owner)})
        ctx = tools.ToolContext(user, systems, admin=admin)
        names = [t["function"]["name"] for t in offered]
        system_msg = {"role": "system", "content": prompts.admin_prompt(display_name(user), names, date.today())
                      if admin else prompts.system_prompt(display_name(user), systems, names, date.today(),
                                                          model_can_call_tools=cap.get("tools") is not False)}
        if not admin:
            # What the Hub already knows about the question, before the model is asked.
            known = await _ground(ctx, text, state, emit)
            if known:
                system_msg["content"] += "\n\n" + known

        await _answer(user, key, model, plan, system_msg, history, offered, ctx, state, emit, notice)
        if state.failed:
            # The error is already on its way to the page. The requests it spent still
            # count against the key, so they still count here.
            if state.requests and not admin:
                await run_in_threadpool(store.record_usage, uid, mid, state.prompt_tokens,
                                        state.completion_tokens, state.requests)
            return

        offer = None if admin else save_fix_action(state, systems)
        if offer:
            state.actions.append(offer)
            emit({"type": "action", "action": offer})
        if not state.answer.strip() and state.actions:
            state.answer = "Review the proposed change below. Nothing is sent until you confirm it."
        if not state.answer.strip():
            if state.steps:
                state.answer = _fallback_answer(state)
                state.found_only = True
            else:
                return fail("empty_answer", "The model returned an empty answer. Ask again, or pick another model.")

        meta = {
            "model": mid,
            "model_label": model["label"],
            "steps": state.steps,
            "sources": state.sources,
            "actions": state.actions,
            "notices": state.notices,
            "usage": {"prompt_tokens": state.prompt_tokens, "completion_tokens": state.completion_tokens,
                      "requests": state.requests},
            "limits": state.limits,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        message_id = await run_in_threadpool(store.add_message, conv_id, "assistant", state.answer, meta)
        state.message_id = message_id
        await run_in_threadpool(store.touch_conversation, uid, conv_id, mid, bot)
        # AdminBot spends the Hub's key, not the admin's: nothing to add to their usage.
        if state.requests and not admin:
            await run_in_threadpool(store.record_usage, uid, mid, state.prompt_tokens,
                                    state.completion_tokens, state.requests)
        emit({"type": "done", "message": {"id": message_id, "role": "assistant", "content": state.answer, "meta": meta}})
    except asyncio.CancelledError:
        # The person pressed Stop or closed the tab. Keep what was written so far.
        state.stopped = True
        if conv_id and (state.answer or state.steps):
            partial = (state.answer or _fallback_answer(state)) + "\n\n*(Stopped.)*"
            meta = {"steps": state.steps, "sources": state.sources, "stopped": True}
            try:
                await asyncio.shield(run_in_threadpool(store.add_message, conv_id, "assistant", partial, meta))
            except BaseException:
                pass
        raise
    except Exception:
        log.exception("devbot: the question failed")
        fail("internal", "Something went wrong while answering. The details are in the Hub's log.")
    finally:
        # Every question that got as far as a model, however it ended. In the finally for
        # the same reason as the failed answer below: each way out leaves with `return`.
        if slot and state.model_id:
            outcome = ("stopped" if state.stopped else "failed" if state.error else
                       "found_only" if state.found_only else "answered")
            try:
                await asyncio.shield(run_in_threadpool(
                    monitor.record, uid, conversation_id=state.conv_id, message_id=state.message_id,
                    model=state.model_id, outcome=outcome, error_kind=str(state.error.get("kind") or ""),
                    requests=state.requests, prompt_tokens=state.prompt_tokens,
                    completion_tokens=state.completion_tokens,
                    duration_ms=int((time.monotonic() - started) * 1000), limit_waits=state.limit_waits,
                    steps=state.steps, bot=bot))
            except BaseException:
                log.warning("devbot: the question's outcome was not recorded", exc_info=True)
        if slot:
            _active -= 1
            _per_user[uid] = max(0, _per_user.get(uid, 1) - 1)
        # A question that got no answer gets its failure as the answer: the page shows it
        # after a reload, and the model never replays an unanswered question (see
        # to_model_messages). In the finally because every failure leaves with `return`;
        # skipped when the person pressed Stop, whose partial answer is kept above.
        if state.error and state.question_saved and state.conv_id and not state.stopped:
            try:
                await run_in_threadpool(store.add_message, state.conv_id, "assistant", "",
                                        {"error": state.error, "notices": state.notices})
            except Exception:
                log.warning("devbot: the failed answer was not recorded", exc_info=True)
        emit(None)


async def _answer(user: AuthUser, key: str, model: Dict[str, Any], plan: budget.Plan, system_msg: Dict[str, Any],
                  history: List[Dict[str, Any]], offered: List[Dict[str, Any]], ctx: tools.ToolContext,
                  state: _Turn, emit: Callable, notice: Callable) -> None:
    mid = model["id"]
    waited = shrunk = False
    round_no = 0
    while round_no < plan.rounds:
        last = round_no == plan.rounds - 1
        offer = offered if (offered and not last) else None
        msgs, dropped = budget.fit([system_msg] + history, plan.prompt_cap, tools=offer, model=mid)
        if dropped:
            notice("Earlier parts of this conversation were left out so the question fits your key's limits. "
                   "Start a new conversation for an unrelated question.")
        estimated = budget.total(msgs, offer, mid)
        parts: List[str] = []
        end: Dict[str, Any] = {}
        try:
            async for event in llm.chat(key, mid, msgs, tools=offer, max_tokens=plan.answer_tokens,
                                        stream=config.stream()):
                if event["type"] == "content":
                    parts.append(event["text"])
                    emit({"type": "delta", "text": event["text"]})
                elif event["type"] == "reasoning":
                    state.reasoning_chars += len(event["text"])
                    emit({"type": "reasoning", "text": event["text"]})
                elif event["type"] == "end":
                    end = event
        except llm.LLMError as exc:
            state.requests += 1
            if exc.kind == "tools" and offer:
                models.remember(mid, False, exc.message)
                notice(f"{model['label']} cannot read live data (its server is not set up for it). "
                       "Answering without it; pick a model marked Live data to ask about your systems.", "warn")
                offered = []
                if parts:
                    emit({"type": "interim", "text": ""})
                continue
            if exc.kind == "limit" and not waited and exc.retry_after is not None and exc.retry_after <= LIMIT_WAIT_MAX:
                waited = True
                state.limit_waits += 1
                emit({"type": "notice", "level": "warn",
                      "text": f"The key's per-minute limit was reached. Waiting {exc.retry_after} seconds for it to free up."})
                await asyncio.sleep(exc.retry_after + 1)
                continue
            if exc.kind == "context" and not shrunk:
                shrunk = True
                if exc.context_tokens:
                    # The model said how big it really is: plan against that from now on.
                    await run_in_threadpool(models.learn_context, mid, exc.context_tokens)
                    plan.answer_tokens = min(plan.answer_tokens, max(256, exc.context_tokens // 4))
                    plan.prompt_cap = max(1000, min(plan.prompt_cap, exc.context_tokens - plan.answer_tokens - 256))
                else:
                    plan.prompt_cap = max(1500, int(estimated * 0.55))
                continue
            if state.steps:
                notice(exc.message, "warn")
                state.answer = _fallback_answer(state)
                state.found_only = True
                return
            emit_error(exc, emit, state)
            state.failed = True
            return
        state.requests += 1
        usage = end.get("usage") or {}
        state.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        state.completion_tokens += int(usage.get("completion_tokens") or 0)
        if usage.get("prompt_tokens"):
            budget.learn(mid, estimated, int(usage["prompt_tokens"]))
        if end.get("limits"):
            state.limits = {**state.limits, **end["limits"]}
            _save_limits(state.limits_owner, end["limits"])
            emit({"type": "limits", "limits": state.limits})

        calls = end.get("tool_calls") or []
        if calls and offer:
            if models.capability(mid).get("tools") is not True:
                models.remember(mid, True, "Calls tools, so it can read live data.")
            interim = "".join(parts).strip()
            if interim:
                emit({"type": "interim", "text": interim})
            await _run_tools(user, ctx, calls, interim, history, state, emit)
            round_no += 1
            continue
        if end.get("text_tool_call") and offer:
            models.remember(mid, False, "It writes tool calls as text: its server does not pass them on.")
            notice(f"{model['label']} cannot read live data (its server does not pass tool calls on). "
                   "Pick a model marked Live data to ask about your systems.", "warn")
            emit({"type": "interim", "text": ""})
            offered = []
            round_no += 1
            continue
        state.answer = "".join(parts).strip()
        if not state.answer and end.get("finish_reason") == "length":
            notice("The model used up its room for this answer while thinking. Ask a narrower question, "
                   "or pick another model.", "warn")
        return


GROUNDING_SECONDS = 20


async def _ground(ctx: tools.ToolContext, question: str, state: _Turn, emit: Callable) -> str:
    """Look the question up in the index (grounding.py) as the first step the person
    sees, and hand back what the prompt should carry. Bounded: a slow or failed search
    costs a line in the steps, never the answer."""
    step: Dict[str, Any] = {"id": "knowledge-" + uuid.uuid4().hex[:8], "tool": "knowledge_search", "system": "knowledge",
                            "label": "Checking what the Hub already knows", "status": "running"}
    started = time.monotonic()
    announced = False
    try:
        pending = asyncio.ensure_future(run_in_threadpool(grounding.gather, ctx.child(lambda s, t: None), question))
        done, _ = await asyncio.wait({pending}, timeout=0.05)
        if not done:
            emit({"type": "step", **step})
            announced = True
        found = await asyncio.wait_for(pending, timeout=GROUNDING_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("devbot: the knowledge lookup failed: %s", exc)
        found, failed = {}, "skipped: the lookup failed"
    else:
        failed = ""
    if found is None:
        # Nothing to search (no index, or a message too short): no step at all.
        if announced:
            step.update(status="done", detail="Nothing to search", ms=int((time.monotonic() - started) * 1000))
            emit({"type": "step", **step})
        return ""
    step.update(status="error" if failed else "done", detail=failed or grounding.summary(found),
                ms=int((time.monotonic() - started) * 1000))
    state.steps.append(step)
    for link in grounding.links(found or {}):
        state.add_source(link)
    emit({"type": "step", **step})
    return grounding.for_prompt(found or {})


def emit_error(exc: "llm.LLMError", emit: Callable, state: Optional["_Turn"] = None) -> None:
    extra = {"retry_after": exc.retry_after} if exc.retry_after else {}
    if state is not None:
        state.error = {"kind": exc.kind, "message": exc.message, **extra}
    emit({"type": "error", "kind": exc.kind, "message": exc.message, **extra})


async def _run_tools(user: AuthUser, ctx: tools.ToolContext, calls: List[Dict[str, Any]], interim: str,
                     history: List[Dict[str, Any]], state: _Turn, emit: Callable) -> None:
    """Run the lookups the model asked for, side by side, and hand it the results."""
    # Every call gets an id of its own. Some servers number calls call_0, call_1 afresh
    # in each answer, or send none, and a replayed conversation pairs each result with
    # its call by id: a repeated one hands the model the wrong result, or the gateway
    # refuses the whole conversation.
    for call in calls:
        call["id"] = f"{str(call.get('id') or 'call')[:40]}-{uuid.uuid4().hex[:8]}"
        call.setdefault("type", "function")
    history.append({"role": "assistant", "content": interim or None, "tool_calls": calls})
    conv_rows: List[Dict[str, Any]] = []

    loop = asyncio.get_running_loop()

    async def one(call: Dict[str, Any]) -> Dict[str, Any]:
        name = str((call.get("function") or {}).get("name") or "")
        args = (call.get("function") or {}).get("arguments") or "{}"
        step_id = str(call.get("id") or f"s{len(state.steps)}")
        system = tools.system_of(name)
        step = {"id": step_id, "tool": name, "system": system, "label": tools.label_of(name), "status": "running"}
        emit({"type": "step", **step})
        trail: List[Dict[str, str]] = []

        def progress(sub_system: str, text: str) -> None:
            # Called from the tool's worker thread; the queue belongs to the event loop.
            trail.append({"system": sub_system, "text": text})
            loop.call_soon_threadsafe(emit, {"type": "substep", "id": step_id, "system": sub_system, "text": text})

        result = await run_in_threadpool(tools.run, ctx.child(progress), name, args)
        asked = [a for a in result.get("actions") or [] if not a.get("suggested")]
        if asked and len(asked) > room_for_proposals(state):
            # Refused, not trimmed: a card that silently did not appear is a change the
            # model goes on to describe as proposed.
            result = {"ok": False, "ms": result.get("ms"),
                      "error": f"No more changes can be proposed here: at most {MAX_PROPOSALS_PER_ANSWER} in one "
                               f"answer and {MAX_PROPOSALS_PER_CONVERSATION} in one conversation. Tell the person "
                               "to review the cards already shown, or to start a new conversation."}
        # A failed step carries its reason: "Cancel the run ✗" with nothing after it
        # leaves the person guessing what to do.
        detail = result.get("summary") or ("" if result.get("ok") else result.get("error"))
        step.update({"status": "done" if result.get("ok") else "error", "detail": str(detail or "")[:300],
                     "ms": result.get("ms")})
        if trail:
            step["trail"] = trail[-12:]
        if result.get("not_connected"):
            step["not_connected"] = result["not_connected"]
        state.steps.append(step)
        for link in result.get("links") or []:
            state.add_source({**link, "system": link.get("system") or system})
        for action in result.get("actions") or []:
            state.actions.append(action)
            emit({"type": "action", "action": action})
        if result.get("save_fix"):
            state.save_fix = result["save_fix"]
        emit({"type": "step", **step})
        return {"call": call, "name": name, "result": result, "args": args}

    done = await asyncio.gather(*(one(c) for c in calls))
    for item in done:
        content = tools.for_model(item["result"], tools.max_chars_of(item["name"]))
        history.append({"role": "tool", "tool_call_id": item["call"].get("id"), "content": content})
        conv_rows.append({"call_id": str(item["call"].get("id") or ""), "tool": item["name"],
                          "system": tools.system_of(item["name"]), "ok": bool(item["result"].get("ok")),
                          "content": content})

    # Stored as the model saw it, so a follow-up question replays the same lookups
    # instead of making them again against the person's limits.
    if state.conv_id:
        await run_in_threadpool(store.add_message, state.conv_id, "assistant", interim,
                                {"internal": True, "tool_calls": calls})
        for row in conv_rows:
            content = row.pop("content")
            await run_in_threadpool(store.add_message, state.conv_id, "tool", content, row)


def save_fix_action(state: _Turn, systems: Dict[str, bool]) -> Optional[Dict[str, Any]]:
    """"Save this fix to Confluence", when nothing had it written down.

    Only for a real answer (not the list of what was found when the model gave up), only
    where the person can write to Confluence, and as a DRAFT in the widgets' dialog: the
    space, the title and every line can be changed before anything is created.
    """
    if not state.save_fix or state.found_only or not state.answer.strip() or not systems.get("confluence"):
        return None
    from .tools import proposals

    try:
        spaces = knowledge.settings()["spaces"]
    except Exception:
        spaces = []
    space = spaces[0] if spaces else ""
    title = str(state.save_fix.get("title") or "A fix found with DevBot")[:200]
    content = (state.answer.strip() + "\n\n---\n\nWritten with DevBot in DevOps Hub, from the investigation of:\n\n```\n"
               + str(state.save_fix.get("about") or "")[:600] + "\n```\n")
    return proposals.action(
        "conf-create", "Save this fix to Confluence",
        "Nothing had this fix written down. Save this answer as a page" + (f" in {space}" if space else "")
        + " so the next person who hits the error finds it. You can change it first.",
        {"space": space, "title": title, "content": content, "parent_id": ""},
        suggested=True,
    )


def _fallback_answer(state: _Turn) -> str:
    """What was found, when the model could not write the answer itself."""
    lines = ["The model could not finish an answer, but this is what DevBot found:", ""]
    for step in state.steps:
        mark = "✓" if step.get("status") == "done" else "✗"
        lines.append(f"- {mark} **{step.get('label')}** — {step.get('detail') or ''}")
    if state.sources:
        lines += ["", "Sources:"] + [f"- [{s['title']}]({s['url']})" for s in state.sources[:8]]
    return "\n".join(lines)
