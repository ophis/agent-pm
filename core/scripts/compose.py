#!/usr/bin/env python3
"""Composer: compiles principles + role + task + templates + output into one run prompt, plus its resolved run config.

compose.py --role ROLE [--task TASK] --input FILE|TEXT|- --out FILE --workdir DIR [--resume] [--json]
Prints the prompt, or with --json {"prompt": ..., "run": {...}}. Exits 2 on a config error. Needs Python 3.11+.
Principles get {{role}}, {{task}} and their anchors; role and task text get {{scripts}} (this dir, or the client's path
to it); a destination gets its output values.
"""
import argparse
import json
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT = "delegate-core"   # roles/, tasks/, templates/ and output/ live here

RUN_KEYS = {"tier", "effort", "read", "write", "commands", "templates", "output"}
GLOBAL_KEYS = RUN_KEYS | {"roles"}
ROLE_KEYS = RUN_KEYS | {"default_task", "tasks"}
LISTS = ("read", "write", "commands", "templates")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")   # {{name}} or {{name|default}}
RESUME = "Resumed run after an interruption. These rules are current; they may have changed since this session started.\n\n"


class ConfigError(Exception):
    pass


def read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise ConfigError(f"missing file {rel}")
    with open(path) as f:
        return f.read().strip() + "\n"


def title(text, rel):
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel} must start with a '# ' heading")
    return first[2:].strip()


def summary(text):
    """The first paragraph line after a file's heading."""
    return next((line.strip() for line in text.splitlines()[1:] if line.strip() and not line.startswith("#")), "")


def anchor(heading):
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def fill(text, values, where):
    def value(m):
        if m.group(1) in values:
            return str(values[m.group(1)])
        return m.group(2) if m.group(2) is not None else m.group(0)
    out = PLACEHOLDER.sub(value, text)
    if m := PLACEHOLDER.search(out):
        raise ConfigError(f"unfilled placeholder {m.group(0)} in {where}")
    return out


def fence(text):
    ticks = max([len(r) for r in re.findall(r"`+", text)] + [2]) + 1
    return "`" * ticks


def check_keys(table, allowed, where):
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


def resolve(cfg, role, task=None):
    """The run config for role/task: each RUN_KEYS value from the task, else the role, else the global table."""
    check_keys(cfg, GLOBAL_KEYS, "the global table")
    roles = cfg.get("roles", {})
    if role not in roles:
        raise ConfigError(f"unknown role {role!r}")
    r = roles[role]
    check_keys(r, ROLE_KEYS, f"roles.{role}")
    tasks = r.get("tasks", {})
    default = r.get("default_task")
    if default is not None and default not in tasks:
        raise ConfigError(f"default_task {default!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    task = task or default
    if task not in tasks:
        raise ConfigError(f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    t = tasks[task]
    check_keys(t, RUN_KEYS, f"roles.{role}.tasks.{task}")
    run = {"role": role, "task": task}
    for k in RUN_KEYS:
        for layer in (t, r, cfg):
            if k in layer:
                run[k] = layer[k]
                break
    for k in LISTS:
        run.setdefault(k, [])
    return check(run)


def check(run):
    """The run config, once its tier, effort and output are valid; raises ConfigError."""
    tier, effort = run.get("tier"), run.get("effort")
    if type(tier) is not int or not 1 <= tier <= 4:
        raise ConfigError(f"tier must be an integer 1–4, got {tier!r}")
    if effort not in EFFORTS:
        raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {effort!r}")
    if "type" not in run.get("output", {}):
        raise ConfigError("output.type is not set")
    return run


def resolve_run(root, role, task=None):
    """The run config for role/task, with the role's and task's titles and the task's summary; raises ConfigError."""
    with open(os.path.join(root, "config.toml"), "rb") as f:
        run = resolve(tomllib.load(f), role, task)
    text = os.path.join(root, TEXT)
    role_rel, task_rel = f"roles/{run['role']}.md", f"tasks/{run['task']}.md"
    role_md, task_md = read(text, role_rel), read(text, task_rel)
    return run | {"role_title": title(role_md, role_rel), "task_title": title(task_md, task_rel),
                  "task_summary": summary(task_md)}


def render(root, run, *, input=None, out=None, workdir=None, resume=False, scripts=None):
    """The prompt for a resolved run, ending with the Input/Output/Workdir tail when all three are given."""
    paths = {"scripts": scripts or os.path.join(os.path.abspath(root), "scripts")}
    text = os.path.join(root, TEXT)
    role_rel, task_rel = f"roles/{run['role']}.md", f"tasks/{run['task']}.md"
    names = {"role": run["role_title"], "task": run["task_title"]}
    names |= {"role_anchor": anchor(names["role"]), "task_anchor": anchor(names["task"])}
    parts = [fill(read(root, "principles.md"), names, "principles.md"),
             fill(read(text, role_rel), paths, role_rel), fill(read(text, task_rel), paths, task_rel)]
    for name in run["templates"]:
        rel = f"templates/{name}.md"
        body = read(text, rel)
        f = fence(body)
        parts.append(f"# Template: `{rel}`\n\n{f}markdown\n{body}{f}\n")
    output = run["output"]
    dest_rel = f"output/destinations/{output['type']}.md"
    if not os.path.isfile(os.path.join(text, dest_rel)):
        raise ConfigError(f"no destination {output['type']!r} ({dest_rel})")
    parts.append(fill(read(text, "output/output.md"), {}, "output/output.md") + "\n" + fill(read(text, dest_rel), output, dest_rel))
    prompt = (RESUME if resume else "") + "\n".join(parts)
    if None not in (input, out, workdir):
        tail = f"Output: {os.path.abspath(out)}\nWorkdir: {os.path.abspath(workdir)}"
        tail = f"Input: {os.path.abspath(input)}\n{tail}" if os.path.isfile(input) else f"{tail}\nInput:\n\n{input.strip()}"
        prompt += f"\n---\n\n{tail}\n"
    return prompt


def compose(root, role, task=None, *, input=None, out=None, workdir=None, resume=False):
    """(prompt, run config) for one run; raises ConfigError."""
    run = resolve_run(root, role, task)
    return render(root, run, input=input, out=out, workdir=workdir, resume=resume), run


def main(argv, root=ROOT):
    ap = argparse.ArgumentParser(prog="compose.py")
    ap.add_argument("--role", required=True)
    ap.add_argument("--task")
    for flag in ("--input", "--out", "--workdir"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.input == "-":
        a.input = sys.stdin.read()
    try:
        prompt, run = compose(root, a.role, a.task, input=a.input, out=a.out, workdir=a.workdir, resume=a.resume)
    except ConfigError as e:
        print(f"compose.py: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"prompt": prompt, "run": run}, ensure_ascii=False, indent=1) if a.json else prompt, end="" if not a.json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
