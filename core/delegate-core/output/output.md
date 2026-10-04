# Output

Produce one Markdown document for `Output:` (end of this prompt), starting with frontmatter:

```yaml
---
status: done          # done | needs_input | failed
title: <one line>
summary: |            # 3–5 lines
  ...
questions:            # needs_input only: 1–4, numbered
  - ...
url: <link>           # the delivered link, if Output › Destination gives one; else empty
---
```

- **done** → the deliverable follows the frontmatter; deliver it per Output › Destination, nowhere else.
- **needs_input** (**too vague**): the input lacks a clear question, scope or deliverable, or anything else your task requires. The deliverable may be empty.
- **failed**: nothing usable; `summary` says what failed.
- `Output:` already holds a document, or the input gives an earlier version → revise it, keeping what still holds.
- Your task may add frontmatter fields.
