# Contributing

Once per clone, turn on the repository's git hooks:

```bash
git config core.hooksPath .githooks
```

Git does not carry hooks in a clone, so this is the one step nothing can do for you. Without it the
branch rules below are only enforced in CI, which is a slower way to hear about a typo.

## Branches

**`main` is merge-only.** The `pre-commit` hook refuses a commit made on `main`, `master` or `develop`.
Work on a branch and merge it:

```bash
git switch -c feature/what-you-are-doing
```

Branch names follow [Conventional Branch](https://conventional-branch.github.io/): `<type>/<description>`,
where the description is lowercase letters, digits and single hyphens.

| Type | For |
|------|-----|
| `feature/` | a new capability — `feature/archive-clips` |
| `bugfix/` | a fix — `bugfix/issue-42-chat-offset-drift` |
| `hotfix/` | a fix that can't wait for the usual path — `hotfix/gql-hash-rotation` |
| `release/` | preparing a version — `release/1-2-0` |
| `chore/` | dependencies, tooling, docs, anything with no behaviour change — `chore/bump-httpx` |

A ticket number is just another word in the description. `.githooks/check-branch-name.sh <name>` says
whether a name passes, and CI runs the same script against the branch a pull request comes from (the
`branch-name` job in [tests.yml](.github/workflows/tests.yml)).

Concluding a merge that hit conflicts is a commit on `main`, and the hook lets that one through — the
rule is about where work starts, not where it lands. `git commit --no-verify` skips the hook entirely.
It exists for the day you need it, not for the day you are in a hurry.
