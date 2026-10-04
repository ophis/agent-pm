## Destination

Open or update the pull request for the input's `Repo:` and `Branch:`; the body after the frontmatter is its description.

1. Write the description to `<Workdir>/pr.md`.
2. `git -C <worktree> push -u origin <branch>`. Denied → `status: failed`, `summary` `push not permitted`; stop.
3. No open PR for `<branch>` → `gh pr create --repo <owner>/<name> --head <branch> --base <default> --title '<title>' --body-file <Workdir>/pr.md`; else `gh pr edit <number> --repo <owner>/<name> --body-file <Workdir>/pr.md`.
4. Set `url:` to the PR's URL. PR creation denied → set it to `https://github.com/<owner>/<name>/compare/<default>...<branch>?expand=1` and say in `summary` that the user must open the PR.
