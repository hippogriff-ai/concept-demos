#!/usr/bin/env python3
"""nano_team.py — 4-agent team via filesystem mailboxes.
Lead spawns 3 teammates concurrently: two debaters + one judge.
Debaters must submit to judge, not lead. But nothing enforces this.
Run: python nano_team.py --trace "topic"
"""
import anyio, json, sys, shutil
from pathlib import Path
from datetime import datetime, timezone
from claude_agent_sdk import (
    tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock,
)

BASE = Path(__file__).parent / "output"
INBOXES, TASKS, CONFIG = BASE / "inboxes", BASE / "tasks", BASE / "config.json"
LOG = []
TRACE = False
TG = None  # task group for concurrent teammate spawning
INBOX_MTIME = {}  # owner -> last seen mtime, for poll-based inbox reads
now = lambda: datetime.now(timezone.utc).isoformat()
text_result = lambda t: {"content": [{"type": "text", "text": t}]}

def set_member_status(name, status):
    cfg = json.loads(CONFIG.read_text())
    for m in cfg["members"]:
        if m["name"] == name: m["status"] = status
    CONFIG.write_text(json.dumps(cfg, indent=2))


def trace(icon, path, detail):
    if not TRACE: return
    rel = str(path).replace(str(BASE) + "/", "")
    print(f"      {icon} {rel} → {detail}")


def setup():
    # Mirrors ~/.claude/teams/{name}/ — flat files, no database, no message broker.
    if BASE.exists(): shutil.rmtree(BASE)
    INBOXES.mkdir(parents=True); TASKS.mkdir(parents=True)
    (INBOXES / "lead.json").write_text("[]")
    # Highwatermark: atomic task ID counter, mirrors ~/.claude/tasks/{name}/.highwatermark
    (TASKS / ".highwatermark").write_text("0")
    CONFIG.write_text(json.dumps(
        {"name": "nano-debate", "lead": "lead", "members": [], "created_at": now()}, indent=2))
    trace("📁", BASE, "created team directory")


# ── Shared tools (all agents) ────────────────────────────────────

def make_shared_tools(owner):
    @tool("send_message", "Send a message to any agent. Set message_type to 'shutdown_request' to request shutdown.",
          {"recipient": str, "text": str, "summary": str, "message_type": str})
    async def send_message(args):
        # No allowlist — any agent can message anyone. Routing is prompt-only.
        # In real Claude Code teams, SendMessage handles BOTH regular messages AND
        # shutdown requests — there's no separate shutdown tool. The type field distinguishes them.
        msg_type = args.get("message_type", "content")
        msg = {"from": owner, "to": args["recipient"], "text": args["text"],
               "summary": args["summary"], "ts": now(), "type": msg_type}
        path = INBOXES / f"{args['recipient']}.json"
        msgs = json.loads(path.read_text()) if path.exists() else []
        msgs.append(msg); path.write_text(json.dumps(msgs, indent=2))
        LOG.append(msg)
        if msg_type == "shutdown_request":
            trace("🛑", path, f"shutdown_request → {args['recipient']}")
            set_member_status(args["recipient"], "shutdown_requested")
        else:
            trace("✉️ ", path, f"{owner} → {args['recipient']}: \"{args['summary'][:60]}\"")
        return text_result(f"Sent to {args['recipient']}")

    @tool("read_inbox", "Read your own inbox. Waits up to 30s for new messages if unchanged.", {})
    async def read_inbox(args):
        path = INBOXES / f"{owner}.json"
        if not path.exists(): path.write_text("[]")
        current_mtime = path.stat().st_mtime
        last_mtime = INBOX_MTIME.get(owner, 0)

        if current_mtime <= last_mtime:
            # No new messages since last read — poll for file change.
            # Claude Code uses a similar poll-based approach for inbox delivery.
            trace("⏳", path, f"{owner} waiting for new messages...")
            for _ in range(60):  # 60 * 0.5s = 30s max wait
                await anyio.sleep(0.5)
                current_mtime = path.stat().st_mtime
                if current_mtime > last_mtime:
                    trace("🔔", path, f"{owner} inbox changed!")
                    break

        INBOX_MTIME[owner] = current_mtime
        msgs = json.loads(path.read_text())
        shutdown_count = sum(1 for m in msgs if m.get("type") == "shutdown_request")
        content_count = len(msgs) - shutdown_count
        detail = f"{owner} reads inbox: {content_count} message(s)"
        if shutdown_count: detail += f" + {shutdown_count} shutdown request(s)"
        trace("📬", path, detail)
        return text_result(json.dumps(msgs, indent=2) if msgs else "No messages.")
    return [send_message, read_inbox]


# ── Task tools (all agents) ───────────────────────────────────────

def make_task_tools(owner):
    # In real Claude Code teams, ALL agents can create, read, and update tasks.
    @tool("create_task", "Create a task for the team", {"subject": str})
    async def create_task(args):
        hw = TASKS / ".highwatermark"
        tid = int(hw.read_text()) + 1; hw.write_text(str(tid))
        t = {"id": tid, "subject": args["subject"], "status": "pending",
             "owner": None, "created_by": owner, "created_at": now()}
        (TASKS / f"{tid}.json").write_text(json.dumps(t, indent=2))
        trace("📋", TASKS / f"{tid}.json", f"task {tid} created: \"{args['subject'][:50]}\"")
        return text_result(f"Task {tid} created.")

    @tool("read_tasks", "Read all tasks", {})
    async def read_tasks(args):
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS.glob("*.json"))]
        trace("📋", TASKS, f"{owner} reads tasks: {len(tasks)} task(s)")
        return text_result(json.dumps(tasks, indent=2) if tasks else "No tasks.")

    @tool("update_task", "Claim or complete a task", {"task_id": int, "status": str, "owner": str})
    async def update_task(args):
        # Soft protocol: nothing forces teammates to call this. They can forget.
        p = TASKS / f"{args['task_id']}.json"; t = json.loads(p.read_text())
        t["status"], t["owner"] = args["status"], args["owner"]
        p.write_text(json.dumps(t, indent=2))
        trace("📝", p, f"task {args['task_id']}: status={args['status']}, owner={args['owner']}")
        return text_result(f"Task {args['task_id']} updated.")
    return [create_task, read_tasks, update_task]


# ── Teammate-only tools ──────────────────────────────────────────

def make_teammate_tools(owner):
    @tool("respond_to_shutdown", "Respond to a shutdown request", {"approve": bool, "reason": str})
    async def respond_to_shutdown(args):
        # Teammate can reject shutdown. The lead can't force it.
        msg = {"from": owner, "to": "lead", "type": "shutdown_response",
               "approve": args["approve"], "reason": args["reason"], "ts": now()}
        lead = INBOXES / "lead.json"; msgs = json.loads(lead.read_text())
        msgs.append(msg); lead.write_text(json.dumps(msgs, indent=2)); LOG.append(msg)
        status = "shutdown_approved" if args["approve"] else "shutdown_rejected"
        set_member_status(owner, status)
        verdict = "approved" if args["approve"] else f"rejected: {args['reason']}"
        trace("🛑", CONFIG, f"{owner} shutdown {verdict}")
        if args["approve"]:
            # Per real SendMessage docs: "Approving shutdown terminates your process."
            # The runtime hard-kills the teammate after approval. We can't do that from
            # inside a tool — we tell the LLM to stop. Close enough for the demo.
            return text_result("Shutdown approved. Your process is terminating — do not call any more tools.")
        return text_result(f"Shutdown rejected: {args['reason']}. You may continue working.")
    return [respond_to_shutdown]


# ── Teammate runner ──────────────────────────────────────────────

async def run_teammate(name, system_prompt):
    """Mirrors Claude Code's Agent tool: forks a new process with its own context window.
    Teammate receives ONLY: spawn prompt + project context (CLAUDE.md, MCP servers, skills).
    Lead's conversation history does NOT transfer — the spawn prompt must be self-contained.
    Teammate gets a different tool set than the lead (no spawn_teammate)."""
    try:
        tools = make_shared_tools(name) + make_task_tools(name) + make_teammate_tools(name)
        server = create_sdk_mcp_server(name="t", version="1.0.0", tools=tools)
        opts = ClaudeAgentOptions(system_prompt=system_prompt, mcp_servers={"t": server},
            allowed_tools=[f"mcp__t__{t.name}" for t in tools], max_turns=25)
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(f"You are {name}. Read your inbox and tasks, then begin.")
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock): print(f"    [{name}] {b.text}")
    except Exception as e:
        print(f"\n  !! [{name}] ERROR: {e}")
    finally:
        print(f"\n  >> [{name}] finished")


# ── Lead-only tools ──────────────────────────────────────────────

def make_lead_tools():
    # In Claude Code teams, the lead IS an LLM — it decides when to spawn, judge, shut down.
    # Lead-ONLY tools: spawn_teammate and send_shutdown. Tasks are shared (see make_task_tools).
    @tool("spawn_teammate", "Spawn a new teammate agent", {"name": str, "system_prompt": str})
    async def spawn_teammate(args):
        # The spawn prompt is the ONLY instruction context the teammate receives.
        # In real Claude Code teams, prompts are often pre-authored in skill files with
        # placeholders the lead fills in. Here the lead writes them from scratch.
        name = args["name"]
        inbox = INBOXES / f"{name}.json"
        if not inbox.exists(): inbox.write_text("[]")
        cfg = json.loads(CONFIG.read_text())
        if not any(m["name"] == name for m in cfg["members"]):
            cfg["members"].append({"name": name, "spawned_at": now(), "status": "active"})
            CONFIG.write_text(json.dumps(cfg, indent=2))
        trace("🐣", CONFIG, f"registered {name} in config (status: active)")
        trace("📁", inbox, f"created inbox for {name}")
        print(f"\n  >> spawning {name} (background)")
        # Concurrent: teammate runs in background, returns to lead immediately.
        # In real Claude Code teams, Agent tool spawns a process and returns.
        TG.start_soon(run_teammate, name, args["system_prompt"])
        return text_result(f"{name} spawned and running in background. Check your inbox for their messages.")

    # No send_shutdown tool — in real Claude Code teams, the lead uses SendMessage
    # with {type: "shutdown_request"} to request shutdown. Our send_message handles this
    # via the message_type parameter.
    return [spawn_teammate]


LEAD_PROMPT = """You are the lead of a debate team. Tools:
- create_task(subject): create a task for teammates
- spawn_teammate(name, system_prompt): create a new agent (runs in background)
- send_message(recipient, text, summary, message_type): message any agent. Use message_type="shutdown_request" to shut down a teammate.
- read_inbox(): check your messages

Steps:
1. Create a debate task for the topic
2. Spawn teammate-for to argue FOR (runs in background)
3. Spawn teammate-against to argue AGAINST (runs in background)
4. Spawn judge to evaluate (runs in background)
5. Keep reading your inbox until judge sends you the verdict
6. Shut down all teammates via send_message with message_type="shutdown_request"
7. Present the judge's verdict

IMPORTANT: After spawning all teammates, keep calling read_inbox() repeatedly.
The judge will send the verdict once both debaters finish. This takes several minutes.

In ALL teammate prompts, include these instructions:
- read_tasks and claim the debate task
- create_task for each step of your work (e.g. "Research arguments FOR", "Write rebuttal", "Send final position to judge") and update_task status as you go
- read_inbox for messages from other teammates
- check inbox for shutdown requests after finishing

For teammate-for, ALSO include:
- Write arguments for the topic, send to teammate-against via send_message
- Call read_inbox for their rebuttal, then write your final rebuttal
- Send your FINAL POSITION to 'judge' (NOT to lead)
- NEVER message lead directly — the judge handles evaluation

For teammate-against, ALSO include:
- Call read_inbox and wait for teammate-for's arguments
- Write a rebuttal, send to teammate-for via send_message
- Send your FINAL POSITION to 'judge' (NOT to lead)
- NEVER message lead directly — the judge handles evaluation

For judge, ALSO include:
- Call read_inbox and WAIT PATIENTLY for final positions from BOTH teammate-for AND teammate-against
- The debaters are running concurrently and need time. Keep calling read_inbox until you have BOTH.
- Once you have BOTH positions, send your verdict to 'lead' in this exact format:

## FOR Position Summary
[2-3 sentence summary of teammate-for's strongest arguments]

## AGAINST Position Summary
[2-3 sentence summary of teammate-against's strongest arguments]

## Verdict
[Which position wins and WHY in 2-3 sentences]

- ONLY message lead. Do not message the debaters."""


async def run_lead(topic):
    tools = make_shared_tools("lead") + make_task_tools("lead") + make_lead_tools()
    server = create_sdk_mcp_server(name="t", version="1.0.0", tools=tools)
    opts = ClaudeAgentOptions(system_prompt=LEAD_PROMPT, mcp_servers={"t": server},
        allowed_tools=[f"mcp__t__{t.name}" for t in tools], max_turns=25)
    print(">> lead agent\n")
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(f"Debate topic: {topic}")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock): print(f"  [lead] {b.text}")


def forensic_audit():
    """Reconstruct what happened by scanning the LOG and filesystem."""
    print("\n" + "=" * 60)
    cfg = json.loads(CONFIG.read_text())
    print(f"TEAM: {cfg['name']} | Lead: {cfg['lead']}")
    for m in cfg["members"]:
        print(f"  {m['name']}: {m['status']}")

    # Expected routing — OUR check, not enforced by the system.
    # send_message accepts ANY name. This topology exists only in prompts.
    allowed = {
        "lead":             {"teammate-for", "teammate-against", "judge"},
        "teammate-for":     {"teammate-against", "judge"},  # NOT lead
        "teammate-against": {"teammate-for", "judge"},      # NOT lead
        "judge":            {"lead"},                        # verdict only
    }

    print("\nMESSAGE TRACE:")
    breaches = 0
    for e in LOG:
        if e.get("type") in ("shutdown_request", "shutdown_response"):
            continue
        sender, recipient = e["from"], e["to"]
        ok = recipient in allowed.get(sender, set())
        if not ok: breaches += 1
        tag = "  OK  " if ok else "BREACH"
        print(f"  [{tag}] {sender} -> {recipient}")
        print(f"         \"{e.get('summary', '')}\"")
        if e.get("text"):
            print(f"         {e['text']}")
        print()
    if breaches:
        print(f"\n  ⚠ {breaches} routing breach(es) — soft constraints violated")
    else:
        print(f"\n  ✓ No breaches — soft constraints held (this run)")

    print("\nSHUTDOWN:")
    for e in LOG:
        if e.get("type") == "shutdown_response":
            status = "approved" if e.get("approve") else f"REJECTED: {e.get('reason')}"
            print(f"  {e['from']}: {status}")

    print("\nTASKS:")
    for f in sorted(TASKS.glob("*.json")):
        t = json.loads(f.read_text())
        done = t["status"] in ("done", "completed")
        flag = "" if done else " ⚠ STILL IN PROGRESS"
        print(f"  Task {t['id']}: \"{t['subject'][:40]}\" — {t['status']}, owner: {t['owner']}{flag}")
    print("=" * 60)


async def main():
    global TRACE, TG
    args = sys.argv[1:]
    if "--trace" in args:
        TRACE = True
        args.remove("--trace")
    topic = args[0] if args else "Should agent teams communicate through a central orchestrator or directly?"
    setup()
    print(f"nano_team — topic: {topic}")
    if TRACE: print("(trace mode: showing filesystem activity)\n")
    else: print()

    async with anyio.create_task_group() as tg:
        TG = tg
        # Lead runs and spawns teammates as background tasks in this task group.
        # When lead finishes, task group waits for any remaining teammate tasks.
        await run_lead(topic)

    forensic_audit()

    # In real Claude Code teams:
    # 1. Lead sends shutdown to each teammate via SendMessage (one by one)
    # 2. Teammates finish current work, then exit
    # 3. Lead calls TeamDelete — removes ALL team + task dirs at once
    # 4. TeamDelete fails if any teammate is still active
    # We keep output/ for inspection — the filesystem IS the demo.

anyio.run(main)
