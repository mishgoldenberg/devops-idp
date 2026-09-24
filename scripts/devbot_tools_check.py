r"""Run every DevBot read tool as one Hub user, with that user's stored tokens. No model is
involved and nothing is written anywhere: it is how you see each system answer DevBot
before asking DevBot anything.

Pipe it into the backend pod from PowerShell (it imports the running app's own code):

    Get-Content -Raw .\scripts\devbot_tools_check.py | oc exec -i -n devops-hub deploy/backend-test -- python - someone@example.com

The argument is the person's Hub e-mail or username. After it, optionally, one tool name
and its arguments as name=value pairs -- not JSON, because Windows PowerShell strips the
double quotes out of an argument on its way to oc:

    ... -- python - someone@example.com ado_work_item id=1234
    ... -- python - someone@example.com conf_search "query=deployment runbook"

Keep this file ASCII: Windows PowerShell pipes text to native programs as ASCII.
"""
import json
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(errors="replace")

from db import query_one  # noqa: E402
from devbot import tools  # noqa: E402
from devbot.orchestrator import system_states  # noqa: E402

# Safe arguments for each read tool: nothing that needs a name only you know.
DEFAULT_CALLS = [
    ("ado_projects", {}),
    ("ado_pipeline_runs", {"mine": True, "top": 3}),
    ("ado_work_items", {"assigned_to_me": True, "state": "open", "top": 5}),
    ("ado_pull_requests", {"role": "review"}),
    ("sonar_projects", {}),
    ("art_repositories", {}),
    ("conf_spaces", {}),
    ("conf_recent", {}),
]


def main():
    if len(sys.argv) < 2:
        print("usage: python - <hub e-mail or username> [tool_name name=value ...]")
        return 2
    who = sys.argv[1].strip().lower()
    row = query_one(
        "SELECT id, username, email FROM users WHERE LOWER(email) = %s OR LOWER(username) = %s LIMIT 1",
        [who, who],
    )
    if not row:
        print(f"No Hub user called {who!r}.")
        return 1
    user = {"id": str(row["id"]), "username": row["username"], "email": row["email"] or "", "role": "User"}
    states = system_states(user["id"])
    print(f"User {row['username']} ({row['email']})")
    print("Systems: " + ", ".join(f"{k}={v}" for k, v in states.items()))
    ctx = tools.ToolContext(user, {k: v in ("connected", "public") for k, v in states.items()})

    calls = DEFAULT_CALLS
    if len(sys.argv) > 2:
        args = {}
        for pair in sys.argv[3:]:
            name, _, value = pair.partition("=")
            if not _:
                print(f"{pair!r} is not name=value.")
                return 2
            args[name.strip()] = value.strip()
        calls = [(sys.argv[2], args)]
    worst = 0
    for name, args in calls:
        tool = tools.REGISTRY.get(name)
        if tool is None:
            print(f"\n{name}: no such tool. Known: {', '.join(sorted(tools.REGISTRY))}")
            return 1
        if not ctx.systems.get(tool.system) and len(sys.argv) <= 2:
            print(f"\n{name:22} SKIP  {tool.system} is {states.get(tool.system)}")
            continue
        started = time.monotonic()
        result = tools.run(ctx, name, args)
        took = time.monotonic() - started
        sent = tools.base.for_model(result, tool.max_chars)
        verdict = "OK  " if result.get("ok") else "FAIL"
        worst = worst or (0 if result.get("ok") else 1)
        print(f"\n{name:22} {verdict}  {took:5.1f}s  {len(sent):5d} chars to the model")
        print("  " + str(result.get("summary") or result.get("error") or "")[:300])
        if len(sys.argv) > 2:
            print(json.dumps(result.get("data") if result.get("ok") else result, indent=1, ensure_ascii=True, default=str)[:4000])
    return worst


if __name__ == "__main__":
    sys.exit(main())
