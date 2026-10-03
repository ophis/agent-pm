# Output

Supplied by the delegate, not a rule set.

Write one Markdown file at `Output:`, starting with frontmatter:

```yaml
---
status: done          # done | needs_input | failed
title: <one line>
summary: |            # 3–5 lines
  ...
questions:            # needs_input only: 2–4, numbered
  - ...
url: <link>           # set by Output › Destination once delivered
---
```

- **done** → the document follows the frontmatter; deliver it per Output › Destination, nowhere else.
- **needs_input** (**too vague**): the input lacks a clear question, scope or deliverable, or what your task also requires. Body may be empty.
- **failed**: nothing usable; `summary` says what failed.
- `Output:` already has content → revise it, keeping what still holds.
- Your task may add frontmatter fields.
