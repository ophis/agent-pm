// A fake `opencode` TUI: loads the plugins its OPENCODE_CONFIG_CONTENT lists, as OpenCode 1.19.3
// does, and plays FAKE_SCENARIO's turns ("turns", or "resume" for a --session start) as OpenCode bus
// events to them. The first turn's text is --prompt's (a fresh start), the rest stdin lines. A step:
// {text}, {tool}, {bash, child?} (asks unless --auto, then waits for the stdin line "allow"),
// {error, recover?} (recovered: the turn goes on, as after a compaction), {foreign}, {busy}, {reinit}, {nested}. Each start and nested probe is a FAKE_LOG line.
import { spawnSync } from "node:child_process";
import { appendFileSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const NATIVE = "ses_fakeTuiRootSession0001";
const CHILD = "ses_fakeTuiChildSession001";
const FOREIGN = "ses_fakeTuiForeignSession1";

function log(value) {
  appendFileSync(process.env.FAKE_LOG, JSON.stringify(value) + "\n");
}

function options(argv) {
  const found = { session: null, prompt: null, auto: false, model: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--session" || arg === "-s") found.session = argv[++i];
    else if (arg.startsWith("--prompt=")) found.prompt = arg.slice("--prompt=".length);
    else if (arg === "--model") found.model = argv[++i];
    else if (arg === "--auto") found.auto = true;
  }
  return found;
}

const argv = process.argv.slice(2);
const opts = options(argv);
const config = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT ?? "{}");
const scene = JSON.parse(readFileSync(process.env.FAKE_SCENARIO, "utf8"));
const native = opts.session ?? NATIVE;
log({
  mode: "tui",
  argv,
  cwd: process.cwd(),
  config,
  env: {
    hook: process.env.AGENT_PM_OPENCODE_HOOK ?? null,
    session: process.env.AGENT_PM_OPENCODE_SESSION ?? null,
    pure: process.env.OPENCODE_PURE ?? null,
  },
});

const messages = [];
const client = { session: { messages: async () => ({ data: messages.map((m) => ({ ...m })) }) } };
const modules = [];
for (const url of config.plugin ?? []) modules.push(await import(url));
let hooks = [];

async function init() {
  hooks = [];
  for (const module of modules) {
    for (const plugin of Object.values(module)) {
      hooks.push(await plugin({ client, directory: process.cwd(), worktree: process.cwd(), $: undefined }));
    }
  }
}

async function emit(type, properties) {
  for (const found of hooks) await found.event?.({ event: { id: "evt", type, properties } });
}

const lines = [];
const waiting = [];
const input = createInterface({ input: process.stdin, terminal: false });
input.on("line", (line) => (waiting.length ? waiting.shift()(line) : lines.push(line)));
input.on("close", () => waiting.splice(0).forEach((resolve) => resolve(null)));

function next() {
  return lines.length ? Promise.resolve(lines.shift()) : new Promise((resolve) => waiting.push(resolve));
}

let part = 0;
async function tool(session, name) {
  const id = `prt_${++part}`;
  for (const status of ["pending", "running", "completed"]) {
    await emit("message.part.updated", {
      sessionID: session,
      part: { id, sessionID: session, type: "tool", tool: name, state: { status } },
      time: Date.now(),
    });
  }
}

function asks(command) {
  const rule = config.permission?.bash ?? "ask";
  const found = typeof rule === "string" ? rule : (rule[command] ?? rule["*"] ?? "ask");
  return !opts.auto && found === "ask";
}

async function step(item) {
  if ("text" in item) {
    const text = { id: `prt_${++part}`, sessionID: native, type: "text", text: item.text };
    messages.push({ info: { role: "assistant" }, parts: [text] });
    await emit("message.part.updated", { sessionID: native, part: text, time: Date.now() });
  } else if ("tool" in item) {
    await tool(native, item.tool);
  } else if ("bash" in item) {
    const session = item.child ? CHILD : native;
    if (item.child) await emit("session.created", { info: { id: CHILD, parentID: native } });
    if (asks(item.bash)) {
      await emit("permission.asked", { id: "per_1", sessionID: session, permission: "bash", patterns: [item.bash] });
      while ((await next()) !== "allow");
      await emit("permission.replied", { sessionID: session, requestID: "per_1", reply: "once" });
    }
    await tool(session, "bash");
  } else if ("error" in item) {
    await emit("session.error", { sessionID: native, error: item.error });
    if (!item.recover) {
      // halt's idle; turn() then sends the run's own (processor.ts halt, runner.ts finishRun)
      await emit("session.idle", { sessionID: native });
      return true;
    }
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
  } else if ("foreign" in item) {
    await emit("session.created", { info: { id: FOREIGN } });
    await emit("session.status", { sessionID: FOREIGN, status: { type: "busy" } });
    await tool(FOREIGN, "read");
    await emit("permission.asked", { id: "per_2", sessionID: FOREIGN, permission: "bash", patterns: [] });
    await emit("session.idle", { sessionID: FOREIGN });
  } else if ("busy" in item) {
    await emit("session.status", { sessionID: native, status: { type: "busy" } });
  } else if ("reinit" in item) {
    await init();
  } else if ("nested" in item) {
    const probe = "process.stdout.write(JSON.stringify(['AGENT_PM_OPENCODE_HOOK', 'AGENT_PM_OPENCODE_SESSION'].map((k) => k in process.env)))";
    const child = spawnSync(process.execPath, ["-e", probe], { env: process.env, encoding: "utf8" });
    log({ nested: JSON.parse(child.stdout) });
  }
  return false;
}

async function turn(text, steps, first) {
  if (first && !opts.session) await emit("session.created", { info: { id: native } });
  messages.push({ info: { role: "user" }, parts: [{ type: "text", text }] });
  await emit("session.status", { sessionID: native, status: { type: "busy" } });
  for (const item of steps) if (await step(item)) break;
  await emit("session.status", { sessionID: native, status: { type: "idle" } });
  await emit("session.idle", { sessionID: native });
}

await init();
const turns = (opts.session && scene.resume) || scene.turns || [[{ text: "first" }]];
let index = 0;
let first = true;
if (opts.prompt !== null && !opts.session) {
  await turn(opts.prompt, turns[index++] ?? [], first);
  first = false;
}
for (let line = await next(); line !== null; line = await next()) {
  await turn(line, turns[index++] ?? [], first);
  first = false;
}
