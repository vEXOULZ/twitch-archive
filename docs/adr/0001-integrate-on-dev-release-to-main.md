# ADR-0001: Integrate on dev, release to main

**Status:** Accepted — 2026-10-02
**Date:** 2026-10-02
**Deciders:** Project owner

Adapted from doomtp-bot's ADR-0021, which made the same change there.

## Context

The server deploys `main`: its update timer fetches the branch, rebuilds the images and restarts the
stack. So every pull request merged into `main` is a production deploy. Features that belong together
reach production one at a time, a half-finished series is live between its merges, and there is no name
for "what was running last week" other than a commit sha.

## Decision

- **`dev` is the integration branch.** Feature, bugfix and chore branches open their pull requests
  against `dev`, which is the repository's default branch. `dev` has the same protection as `main`: pull
  requests only, the CI checks green, up to date with its base.
- **`main` is production.** It takes pull requests only from `dev` (a release), `release/*` or `hotfix/*`.
  CI's `branch-name` check enforces this (`flow = "dev"` in `.conventions.toml`).
- **A release** is one pull request from `dev` into `main`, merged with a merge commit. The version is
  bumped on `dev` first, in a `release/x-y-z` pull request that changes `version` in the root
  `pyproject.toml`. After the merge, a `vX.Y.Z` tag on `main` names it:
  `gh release create vX.Y.Z --target main --generate-notes`. CI refuses a tag that disagrees with that
  version.
- **Images:** CI publishes `ghcr.io/vexoulz/twitch-archive-api` and `-worker`. A push to `main` publishes
  `:main`, a push to `dev` publishes `:dev`, and a tag publishes `:vX.Y.Z`; each also gets its `:<sha>`.
  Until the server pulls these images it keeps building `main` from its checkout, which is the same code.
- **Rolling back** is reverting on `main` today, and pinning `TAG` to the previous `:vX.Y.Z` once the
  server pulls images. Migrations stay additive, so a rolled-back release runs against the newer schema.
- **A hotfix** branches from `main`, merges into `main`, and then `main` merges back into `dev` in a pull
  request, so `dev` never loses it.

## Amendment (2026-10-05): releases run from release.yml

The release steps above are now a workflow, from vEXOULZ/conventions v2.0.0. **Actions → release → Run workflow** works out the next version from the Conventional Commits since the last tag, writes it into `pyproject.toml` and `uv.lock` on a `release/x-y-z` branch and opens "Release vX.Y.Z" from it into `main`. That replaces the separate version pull request into `dev` and the release pull request from `dev`. When it merges, the workflow tags the merge commit, publishes the GitHub release and opens the pull request that brings `main` back into `dev`. A required check, `conventions / version`, fails any pull request into `main` that doesn't raise the version to one with no tag yet, which closes the gap that left v0.2.0's tag without an image in doomtp-bot. CONTRIBUTING.md has the steps.

## Options Considered

### Option A: keep merging features into main
**Pros:** nothing to set up, one pull request per change. **Cons:** every merge is a deploy, and there are
no release names to roll back to.

### Option B: dev plus main, releases tagged (chosen)
**Pros:** production changes only when a release is merged, related features ship together, and every
release has a tag and images. **Cons:** a second pull request per release, and hotfixes need a merge
back into `dev`.

### Option C: keep main as the integration branch and deploy tags only
**Pros:** one long-lived branch. **Cons:** the server would have to be told about every release, since
there is no moving tag to follow, and `main` would no longer say what production runs.

## Consequences

- **Easier:** batching features into one deploy, naming releases, rolling back to a release.
- **Harder:** a release needs a version bump and a second pull request. Open pull requests have to be
  pointed at `dev`.

## Action Items

1. [x] `branch-name` check for the dev flow, and CI publishing `:dev`, `:main` and `:vX.Y.Z` images.
2. [ ] Create `dev` from `main`, protect it like `main`, and make it the default branch
   (`conventions repo-settings --apply`).
3. [ ] The first release, `v0.2.0`, once this change reaches `main`.
4. [ ] The server pulls the published images instead of building them.
