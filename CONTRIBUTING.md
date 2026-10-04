# Contributing

<!-- conventions:begin: synced from vEXOULZ/conventions; edit it there, then run `conventions sync` -->
## Set up the hooks once per clone

```bash
git config core.hooksPath .conventions/githooks
```

Git does not carry hooks in a clone, so this is the one step nothing can do for you (`conventions sync`
does it as a side effect). Without it, the branch rules below are only enforced in CI, which is a slower
way to hear about a typo. A repo's own extra checks live in `.githooks/local/pre-commit`, which the
shared hook runs after its own.

## Branches

Branch names follow [Conventional Branch](https://conventional-branch.github.io/): `<type>/<description>`,
where the description is lowercase letters, digits and single hyphens.

| Type | For |
|------|-----|
| `feature/` | a new capability, e.g. `feature/part-mapping` |
| `bugfix/` | a fix, e.g. `bugfix/issue-42-restricted-seek` |
| `hotfix/` | a fix that can't wait for the usual path, e.g. `hotfix/broken-deploy` |
| `release/` | preparing a version, e.g. `release/1-2-0` |
| `chore/` | dependencies, tooling, docs, anything with no behaviour change, e.g. `chore/bump-vite` |

A ticket number is just another word in the description.
`.conventions/githooks/check-branch-name.sh <name>` says whether a name passes, and CI runs the same
script (the `conventions / branch-name` check) against the branch a pull request comes from.

### Release flow: dev → main

Work integrates on `dev`, and `main` is what production runs. The `pre-commit` hook refuses a commit
made on `main`, `master`, `dev` or `develop`.

- Feature and bugfix branches start from `dev`, and their pull requests target `dev`.
- A release is a pull request from `dev` (or a `release/*` branch) into `main`, then a `vX.Y.Z` tag on
  `main`.
- A `hotfix/*` branch starts from `main` and merges into `main`; `main` then merges back into `dev` so
  `dev` never loses it.
- The `conventions / branch-name` check refuses any other pull request into `main`.

```bash
git switch dev && git pull && git switch -c feature/what-you-are-doing
```

Concluding a merge that hit conflicts is a commit on the branch merged into, and the hook lets that one
through: the rule is about where work starts, not where it lands. `git commit --no-verify` skips the
hook entirely. It exists for the day you need it, not for the day you are in a hurry.

## Checks

Every pull request runs:

- **`conventions / branch-name`:** the branch name, and in a `flow = "dev"` repo whether it may merge
  into its base.
- **`conventions / check`:** the synced files match the version pinned in `.conventions.toml`, and the
  repo follows the conventions for its profile. A public repo is also checked for private
  infrastructure (addresses, server paths).
- **`ci / …`:** the repo's lint, tests and build, from the reusable workflows in
  [vEXOULZ/conventions](https://github.com/vEXOULZ/conventions).

They are required checks on `main`. The repo's settings, protection included, are set by
`conventions repo-settings --apply`.

## Shared conventions

This section, `.conventions/`, `.gitattributes` and the other synced files come from
[vEXOULZ/conventions](https://github.com/vEXOULZ/conventions), at the version pinned in
`.conventions.toml`. Don't edit them here: change them there, release, and bump the pin (Renovate opens
that pull request). After a bump, rewrite the copies and commit them:

```bash
uvx --from "git+https://github.com/vEXOULZ/conventions@$(sed -n 's/^version *= *"\(.*\)"/\1/p' .conventions.toml)" conventions sync
```
<!-- conventions:end -->
