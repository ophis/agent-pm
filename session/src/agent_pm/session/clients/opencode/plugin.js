// agent-pm's OpenCode plugin: turns the root session's events into calls of the tui hook command
// (AGENT_PM_OPENCODE_HOOK, a JSON argv; the label last, a JSON payload on stdin), one at a time.
// The root is the resumed session (AGENT_PM_OPENCODE_SESSION) or the first session created without
// a parent; its children's permission asks count too. State lives here, not per plugin call:
// OpenCode calls a plugin again whenever it rebuilds its instance.
import { spawn } from "node:child_process";

const hook = parse(process.env.AGENT_PM_OPENCODE_HOOK);
let root = process.env.AGENT_PM_OPENCODE_SESSION || null;
// Shell calls inherit process.env: a nested opencode must not find the hook.
delete process.env.AGENT_PM_OPENCODE_HOOK;
delete process.env.AGENT_PM_OPENCODE_SESSION;

const children = new Set();
const seen = new Set();
const TOOL = new Set(["running", "completed", "error"]);
let busy = false;
let failed = false;
let started = false;
let chain = Promise.resolve();

function parse(text) {
  try {
    const argv = JSON.parse(text ?? "");
    const ok = Array.isArray(argv) && argv.length > 0 && argv.every((a) => typeof a === "string");
    return ok ? argv : null;
  } catch {
    return null;
  }
}

function run(label, payload) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(hook[0], [...hook.slice(1), label], { stdio: ["pipe", "ignore", "ignore"] });
    } catch {
      resolve();
      return;
    }
    child.on("error", resolve);
    child.on("close", resolve);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

function call(label, fields = async () => ({})) {
  const session = root;
  chain = chain.then(async () => {
    let extra = {};
    try {
      extra = await fields();
    } catch {}
    await run(label, { session_id: session, ...extra });
  });
}

async function reply(client) {
  const found = await client.session.messages({ path: { id: root } });
  const list = Array.isArray(found) ? found : found?.data;
  if (!Array.isArray(list)) return "";
  const last = list.findLastIndex((m) => m?.info?.role === "user");
  for (let i = list.length - 1; i > last; i--) {
    if (list[i]?.info?.role !== "assistant") continue;
    const text = (list[i].parts ?? [])
      .filter((p) => p?.type === "text" && typeof p.text === "string" && !p.synthetic && !p.ignored)
      .map((p) => p.text)
      .join("\n");
    if (text.trim()) return text;
  }
  return "";
}

function owned(session) {
  return session != null && (session === root || children.has(session));
}

function handle(event, client) {
  const p = event?.properties ?? {};
  switch (event?.type) {
    case "session.created":
    case "session.updated": {
      const info = p.info ?? {};
      if (event.type === "session.created" && root === null && !info.parentID && info.id) {
        root = info.id;
      } else if (info.parentID && owned(info.parentID) && info.id) {
        children.add(info.id);
      }
      return;
    }
    case "session.status":
      if (p.sessionID !== root || p.status?.type !== "busy" || busy) return;
      busy = true;
      failed = false;
      seen.clear();
      call("session.busy");
      return;
    case "session.idle":
      if (p.sessionID !== root) return;
      busy = false;
      if (failed) {
        failed = false;
        return;
      }
      call("session.idle", async () => ({ last_assistant_message: await reply(client).catch(() => "") }));
      return;
    case "session.error":
      if (p.sessionID !== root || root === null) return;
      failed = true;
      call("session.error", async () => ({
        error: p.error?.name || "unknown",
        message: p.error?.data?.message || "",
      }));
      return;
    case "permission.asked":
      if (!owned(p.sessionID)) return;
      call("permission.asked", async () => ({ permission: p.permission ?? "", patterns: p.patterns ?? [] }));
      return;
    case "permission.replied":
      if (!owned(p.sessionID)) return;
      call("permission.replied", async () => ({ reply: p.reply ?? "" }));
      return;
    case "message.part.updated": {
      const part = p.part ?? {};
      const status = part.state?.status;
      if ((p.sessionID ?? part.sessionID) !== root || part.type !== "tool" || !TOOL.has(status)) return;
      const key = `${part.id}:${status}`;
      if (seen.has(key)) return;
      seen.add(key);
      call("tool", async () => ({ tool_name: part.tool ?? "" }));
      return;
    }
  }
}

export const AgentPm = async ({ client }) => {
  if (hook === null) return {};
  if (!started && root !== null) call("plugin.start");
  started = true;
  return { event: async ({ event }) => handle(event, client) };
};
