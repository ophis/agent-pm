## Destination

Publish the document (the body, without frontmatter) to `{{repo}}`, branch `{{branch}}`, folder `{{dir}}`. `<pub>` is `<Workdir>/publish`.

1. `<pub>` missing → `gh repo clone {{host|github.com}}/{{repo}} <pub> -- --branch {{branch}}`; else `git -C <pub> fetch origin` and `git -C <pub> rebase --autostash origin/{{branch}}`, after `git -C <pub> rebase --abort` if `git -C <pub> status` shows a rebase in progress.
2. **File**: reuse `{{dir}}*-<Reference>-*.md`, else create `{{dir}}<date +%Y-%m-%d-%H%M>-<Reference>-<kebab-slug>.md` (`<Reference>`: the id the input gives, e.g. `TASK-142`; none → drop that part).
3. `git -C <pub> add <file>`, `git -C <pub> commit -m "<Add|Update> <Reference>: <title>" -- <file>`; then `git -C <pub> fetch origin`, `git -C <pub> rebase origin/{{branch}}` and `git -C <pub> push origin HEAD:{{branch}}`, repeating those three on rejection.
4. Set `url: https://{{host|github.com}}/{{repo}}/blob/{{branch}}/<path>` (spaces as `%20`) in `Output:`.

**Published**: `git -C <pub> status --porcelain -- <file>` and, after a fetch, `git -C <pub> log origin/{{branch}}..HEAD` print nothing.
