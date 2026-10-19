// agent-pm's OpenCode plugin: turns the root session's events into calls of the tui hook command
// (AGENT_PM_OPENCODE_HOOK, a JSON argv; the label last, a JSON payload on stdin), one at a time.
// The root is the resumed session (AGENT_PM_OPENCODE_SESSION) or the first session created without
// a parent; its children's permission asks and questions count too. State lives here, not per plugin call:
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
const REPLY_WAIT = 5000; // ms an idle waits for its turn's final message to be saved
const REPLY_POLL = 200;
let turnUser = null; // the root's latest user message id
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

function call(label, fields = {}) {
  // fields (or its promise) is taken when the event comes, not when the call's turn does.
  const session = root;
  const extra = Promise.resolve(fields).catch(() => ({}));
  chain = chain.then(async () => run(label, { session_id: session, ...(await extra) }));
}

function texts(message) {
  return (message?.parts ?? [])
    .filter((p) => p?.type === "text" && typeof p.text === "string" && !p.synthetic && !p.ignored)
    .map((p) => p.text)
    .join("\n");
}

// The turn's messages: those after its user message (unknown: after the last one), up to the
// next; null while that message is not saved yet.
function slice(list, user) {
  const start = user ? list.findIndex((m) => m?.info?.id === user) : list.findLastIndex((m) => m?.info?.role === "user");
  if (user && start < 0) return null;
  const rest = list.slice(start + 1);
  const end = rest.findIndex((m) => m?.info?.role === "user");
  return end < 0 ? rest : rest.slice(0, end);
}

// The turn's newest assistant text, read until its last assistant message is saved: completed
// with text, or failed. An idle can come before that save; after REPLY_WAIT, the best found.
async function reply(client, user) {
  const until = Date.now() + REPLY_WAIT;
  let best = "";
  for (;;) {
    try {
      const found = await client.session.messages({ path: { id: root } });
      const list = Array.isArray(found) ? found : found?.data;
      const turn = Array.isArray(list) ? slice(list, user) : null;
      const answers = (turn ?? []).filter((m) => m?.info?.role === "assistant");
      best = answers.map(texts).findLast((t) => t.trim()) ?? best;
      const last = answers.at(-1);
      const saved = typeof last?.info?.time?.completed === "number" && texts(last).trim();
      if (turn && last && (saved || last.info.error != null)) return best;
    } catch {} // a failed read is one more try, not the reply
    if (Date.now() >= until) return best;
    await new Promise((resolve) => setTimeout(resolve, REPLY_POLL));
  }
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
      // Busy comes at every step: a step after an error (compaction, a retry) clears it.
      if (p.sessionID !== root || p.status?.type !== "busy") return;
      failed = false;
      seen.clear();
      if (!busy) call("session.busy");
      busy = true;
      return;
    case "session.idle": {
      if (p.sessionID !== root) return;
      busy = false;
      // A fatal error's turn idles twice (its halt, then its run's end); only a busy clears it.
      if (failed) return;
      const text = reply(client, turnUser);
      call("session.idle", text.then((last) => ({ last_assistant_message: last })));
      return;
    }
    case "session.error":
      if (p.sessionID !== root || root === null) return;
      failed = true;
      call("session.error", { error: p.error?.name || "unknown", message: p.error?.data?.message || "" });
      return;
    case "permission.asked":
      if (!owned(p.sessionID)) return;
      call("permission.asked", { permission: p.permission ?? "", patterns: p.patterns ?? [] });
      return;
    case "permission.replied":
      if (!owned(p.sessionID)) return;
      call("permission.replied", { reply: p.reply ?? "" });
      return;
    case "question.asked": {
      if (!owned(p.sessionID)) return;
      const titles = (Array.isArray(p.questions) ? p.questions : []).map((q) => q?.header || q?.question || "");
      call("question.asked", { questions: titles.filter((t) => typeof t === "string") });
      return;
    }
    case "question.replied":
    case "question.rejected":
      if (!owned(p.sessionID)) return;
      call(event.type);
      return;
    case "message.updated":
      if (p.sessionID === root && p.info?.role === "user" && p.info.id) turnUser = p.info.id;
      return;
    case "message.part.updated": {
      const part = p.part ?? {};
      const status = part.state?.status;
      if ((p.sessionID ?? part.sessionID) !== root || part.type !== "tool" || !TOOL.has(status)) return;
      const key = `${part.id}:${status}`;
      if (seen.has(key)) return;
      seen.add(key);
      call("tool", { tool_name: part.tool ?? "" });
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
