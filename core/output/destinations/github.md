## Destination

Publish the deliverable to `{{repo}}`, branch `{{branch}}`, folder `{{dir}}`. `<pub>` is `<Workdir>/publish`.

1. `<pub>` missing → `gh repo clone {{host|github.com}}/{{repo}} <pub> -- --branch {{branch}}`; else `git -C <pub> fetch origin` and `git -C <pub> rebase --autostash origin/{{branch}}`, after `git -C <pub> rebase --abort` if `git -C <pub> status` shows a rebase in progress.
2. **File**: reuse `{{dir}}*-<Reference>-*.md`, else create `{{dir}}<date +%Y-%m-%d-%H%M>-<Reference>-<kebab-slug>.md`.
3. `git -C <pub> add <file>`, `git -C <pub> commit -F <Workdir>/tmp/msg -- <file>`, `<Workdir>/tmp/msg` holding `<Add|Update> <Reference>: <title>`; then `git -C <pub> fetch origin`, `git -C <pub> rebase origin/{{branch}}` and `git -C <pub> push origin HEAD:{{branch}}`, repeating those three on rejection.
4. Set the outcome's `url` to `https://{{host|github.com}}/{{repo}}/blob/{{branch}}/<path>` (spaces as `%20`).

**Published**: `git -C <pub> status --porcelain -- <file>` and, after a fetch, `git -C <pub> log origin/{{branch}}..HEAD` print nothing.
