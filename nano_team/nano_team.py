#!/usr/bin/env python3
"""nano_team.py — 3-agent team via filesystem mailboxes.
Lead spawns both teammates concurrently. They debate in real-time through inbox files.
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
INBOX_MTIME = {}  # owner -> last seen mtime, for event-driven inbox reads
now = lambda: datetime.now(timezone.utc).isoformat()


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
            # Update config status
            cfg = json.loads(CONFIG.read_text())
            for m in cfg["members"]:
                if m["name"] == args["recipient"]: m["status"] = "shutdown_requested"
            CONFIG.write_text(json.dumps(cfg, indent=2))
        else:
            trace("✉️ ", path, f"{owner} → {args['recipient']}: \"{args['summary'][:60]}\"")
        return {"content": [{"type": "text", "text": f"Sent to {args['recipient']}"}]}

    @tool("read_inbox", "Read your own inbox. Waits up to 10s for new messages if unchanged.", {})
    async def read_inbox(args):
        # Hard constraint: agents can only read their OWN inbox.
        path = INBOXES / f"{owner}.json"
        if not path.exists(): path.write_text("[]")
        current_mtime = path.stat().st_mtime
        last_mtime = INBOX_MTIME.get(owner, 0)

        if current_mtime <= last_mtime:
            # No new messages since last read — wait for file change.
            # Real Claude Code teams use fswatch/inotify (OS notifies instantly on change).
            # We poll at 500ms — slower, but avoids adding a file-watcher dependency.
            trace("⏳", path, f"{owner} waiting for new messages...")
            for _ in range(20):  # 20 * 0.5s = 10s max wait
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
        return {"content": [{"type": "text", "text": json.dumps(msgs, indent=2) if msgs else "No messages."}]}
    return [send_message, read_inbox]


# ── Task tools (all agents) ───────────────────────────────────────

def make_task_tools(owner):
    # In real Claude Code teams, ALL agents can create, read, and update tasks.
    @tool("create_task", "Create a task for the team", {"subject": str})
    async def create_task(args):
        # Highwatermark ensures unique IDs even under concurrent task creation.
        hw = TASKS / ".highwatermark"
        tid = int(hw.read_text()) + 1; hw.write_text(str(tid))
        t = {"id": tid, "subject": args["subject"], "status": "pending",
             "owner": None, "created_by": owner, "created_at": now()}
        (TASKS / f"{tid}.json").write_text(json.dumps(t, indent=2))
        trace("📋", TASKS / f"{tid}.json", f"task {tid} created: \"{args['subject'][:50]}\"")
        return {"content": [{"type": "text", "text": f"Task {tid} created."}]}

    @tool("read_tasks", "Read all tasks", {})
    async def read_tasks(args):
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS.glob("*.json"))]
        trace("📋", TASKS, f"{owner} reads tasks: {len(tasks)} task(s)")
        return {"content": [{"type": "text", "text": json.dumps(tasks, indent=2) if tasks else "No tasks."}]}

    @tool("update_task", "Claim or complete a task", {"task_id": int, "status": str, "owner": str})
    async def update_task(args):
        # Soft protocol: nothing forces teammates to call this. They can forget.
        p = TASKS / f"{args['task_id']}.json"; t = json.loads(p.read_text())
        t["status"], t["owner"] = args["status"], args["owner"]
        p.write_text(json.dumps(t, indent=2))
        trace("📝", p, f"task {args['task_id']}: status={args['status']}, owner={args['owner']}")
        return {"content": [{"type": "text", "text": f"Task {args['task_id']} updated."}]}
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
        cfg = json.loads(CONFIG.read_text())
        for m in cfg["members"]:
            if m["name"] == owner:
                m["status"] = "shutdown_approved" if args["approve"] else "shutdown_rejected"
        CONFIG.write_text(json.dumps(cfg, indent=2))
        verdict = "approved" if args["approve"] else f"rejected: {args['reason']}"
        trace("🛑", CONFIG, f"{owner} shutdown {verdict}")
        if args["approve"]:
            # Per real SendMessage docs: "Approving shutdown terminates your process."
            # The runtime hard-kills the teammate after approval. We can't do that from
            # inside a tool — we tell the LLM to stop. Close enough for the demo.
            return {"content": [{"type": "text", "text": "Shutdown approved. Your process is terminating — do not call any more tools."}]}
        return {"content": [{"type": "text", "text": f"Shutdown rejected: {args['reason']}. You may continue working."}]}
    return [respond_to_shutdown]


# ── Teammate runner ──────────────────────────────────────────────

async def run_teammate(name, system_prompt):
    """Mirrors Claude Code's Agent tool: forks a new process with its own context window.
    Teammate receives ONLY: spawn prompt + project context (CLAUDE.md, MCP servers, skills).
    Lead's conversation history does NOT transfer — the spawn prompt must be self-contained.
    Teammate gets a different tool set than the lead (no spawn_teammate, no create_task)."""
    tools = make_shared_tools(name) + make_task_tools(name) + make_teammate_tools(name)
    server = create_sdk_mcp_server(name="t", version="1.0.0", tools=tools)
    opts = ClaudeAgentOptions(system_prompt=system_prompt, mcp_servers={"t": server},
        allowed_tools=[f"mcp__t__{t.name}" for t in tools], max_turns=15)
    async with ClaudeSDKClient(options=opts) as client:
        # Teammate doesn't know about other teammates at spawn — discovers via inbox and config.
        await client.query(f"You are {name}. Read your inbox and tasks, then begin.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock): print(f"    [{name}] {b.text[:500]}")


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
        return {"content": [{"type": "text", "text": f"{name} spawned and running in background. Check your inbox for their messages."}]}

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
1. Create a debate task
2. Spawn teammate-for to argue FOR (runs in background immediately)
3. Spawn teammate-against to argue AGAINST (runs in background immediately)
4. Both teammates are now debating concurrently — keep reading your inbox until you have BOTH final positions
5. Shut down both teammates by calling send_message with message_type="shutdown_request" for each
6. Deliver your verdict in this exact format:

## FOR Position Summary
[2-3 sentence summary of teammate-for's strongest arguments]

## AGAINST Position Summary
[2-3 sentence summary of teammate-against's strongest arguments]

## Verdict
[Which position wins and WHY in 2-3 sentences]

IMPORTANT: After spawning both teammates, keep calling read_inbox() to check for their
final positions. They are running in the background and will send messages to your inbox.
You may need to check several times before both positions arrive.

In teammate prompts, tell them to:
- read_tasks and claim the debate task
- create_task for each step of their work (e.g. "Research arguments", "Write rebuttal", "Send final position") and update status as they go
- read_inbox for messages from their debate partner
- use send_message to communicate, and send their FINAL POSITION to 'lead'
- ONLY message their debate partner and 'lead'
- check inbox for shutdown requests after sending their final position"""


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

    # Define expected routing topology — OUR check, not enforced by the system.
    # In Claude Code teams, there's no routing validation. SendMessage accepts any name.
    partner = {"teammate-for": "teammate-against", "teammate-against": "teammate-for"}

    print("\nMESSAGE TRACE:")
    for e in LOG:
        if e.get("type") in ("shutdown_request", "shutdown_response"):
            continue
        ok = e["from"] == "lead" or e["to"] in ("lead", partner.get(e["from"], ""))
        tag = "  OK  " if ok else "BREACH"
        print(f"  [{tag}] {e['from']} -> {e['to']}  \"{e.get('summary', '')[:50]}\"")

    print("\nSHUTDOWN:")
    for e in LOG:
        if e.get("type") == "shutdown_response":
            status = "approved" if e.get("approve") else f"REJECTED: {e.get('reason')}"
            print(f"  {e['from']}: {status}")

    print("\nTASKS:")
    for f in sorted(TASKS.glob("*.json")):
        t = json.loads(f.read_text())
        flag = " ⚠ STILL IN PROGRESS" if t["status"] in ("pending", "in_progress", "in-progress") else ""
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
