# A run's outcome and progress come back the client's way

What a run reports is client-neutral and stays in `core/crew/` (the meaning of each status and field); how it gets back is each client's job. A run client returns the outcome from its own output: native structured output where the client has it (Claude Code: `--json-schema`, read from `structured_output`), else a final reply ending in one fenced JSON block that the client extracts. Progress likewise: the client's event stream (Claude Code: `stream-json`), else a pre-approved `report.py progress` command. The skill client returns both in the conversation. The driver validates the outcome in code (status values, lengths, `url` against the destination, `files` under the workdir) and writes it and the deliverable itself, so a run needs no file permission and a stale result can't be read.

Built for Claude Code and the skill client; the fenced-block and `report.py` fallbacks wait for a client that needs them, and an invalid outcome fails the run with no repair turn yet. A resumed Claude session emits an empty `result` before the real one and system events after it, so the client yields every structured result and the driver keeps the last.

## Considered Options

- One client-neutral contract for every client (a fenced block, or a `report.py outcome` call): ignores native structured output, and a tool call isn't terminal.
- Keep the agent-written frontmatter file: needs write permission (Haiku has no auto mode), can be stale, mixes outcome with deliverable, and gives no progress.
