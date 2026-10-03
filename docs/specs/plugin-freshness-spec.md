# Build a skill: check Claude Code marketplace and plugin freshness

Use `skill-creator` to build this skill. Name it `plugin-freshness`. Install it
at `~/.claude/skills/plugin-freshness/`.

## What the skill does

Report whether every marketplace and every installed plugin is current, without
changing anything. Read-only by default. Updating is a separate, explicit step.

## The three layers to compare

| Layer | Location | Question it answers |
|---|---|---|
| Installed | `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` | What is running |
| Available | `~/.claude/plugins/marketplaces/<marketplace>/` | What the last marketplace update fetched |
| Upstream | the marketplace's GitHub remote | What actually exists now |

Report each layer separately. "Plugin is current" means installed == available;
"marketplace is current" means local HEAD == remote HEAD. Both must hold before
the skill calls a plugin up to date.

## Two version regimes — handle both

**Semver plugins.** Version comes from `plugin.json`. Compare the installed
version against the version in the marketplace clone.

**Path-based plugins.** The version string is the marketplace's git commit hash,
injected at install time. It appears in no manifest, so manifest comparison
cannot work for them. Judge these by whether the marketplace clone is at
upstream HEAD. Known examples: `context7` and `skill-creator`, both from
`claude-plugins-official`.

Detect the regime from the version string, not from a hardcoded plugin list.

## Required checks

1. Each marketplace: local HEAD vs `git ls-remote origin HEAD`. Use
   `git ls-remote`, not `git fetch` — the check must not mutate anything.
2. Each semver plugin: installed version vs marketplace manifest version.
3. Each path-based plugin: owning marketplace's HEAD vs its remote.
4. Stale cache directories: any `cache/<marketplace>/<plugin>/<version>/` whose
   version is not the installed one.
5. Orphaned cache entries: a cached plugin that no longer appears in the
   installed list.
6. Marketplaces with auto-update off, read from
   `~/.claude/plugins/plugins/known_marketplaces.json`. Verify the exact path and
   key names before relying on them.

## Output

One table, one row per plugin: id, installed version, available version,
marketplace, status (`current` / `update available` / `unknown`). Then a short
list of stale cache directories and orphans. End with the exact commands to fix
what was found — do not run them.

Report `unknown` rather than guessing when a comparison is not possible.

## Environment constraints — verify each before relying on it

These were observed on macOS with Claude Code 2.1.288. Confirm them at build
time rather than trusting this list.

- `claude plugin list` and `claude plugin marketplace update` appear to hang when
  piped, while `> file` works. `claude plugin list --json` and
  `claude plugin marketplace list --json` pipe correctly. Prefer the `--json`
  forms everywhere.
- Directory mtimes are unreliable for identifying the current version. An update
  can touch the superseded directory and leave it newer than the active one.
  Take the installed version from `claude plugin list --json` only.
- `anthropic-plugin-directory` is built in. It has no clone under
  `marketplaces/`, so it has no comparable local HEAD. Skip it and say so.
- `claude plugin marketplace update` with no argument takes roughly 95 seconds
  across 8 marketplaces, against roughly 4 seconds for a single named one. The
  skill must not call it.
- `claude config` no longer exists in this version. Read settings from
  `~/.claude/settings.json` directly.
- Glob `cache/<plugin>/*/` matches many version directories. Never assume one.

## Out of scope

Do not update, install, uninstall, prune or delete. Do not modify settings.
Print commands for the user to run.

## Build process

1. Draft `SKILL.md` plus a script under `scripts/`.
2. Verify every assumption above against the real filesystem on this machine
   before writing logic that depends on it. Report any that turned out wrong.
3. Write eval cases: current-everything, one stale semver plugin, one stale
   path-based plugin, an orphaned cache entry, a marketplace behind its remote.
4. Run the evals, show the results, and iterate.

## Description field

The skill must fire on phrasings like "are my plugins up to date", "check
marketplace freshness", "anything stale in my Claude Code setup". It must not
fire on requests to actually update. Tune the description against both sets.

Use `skill-eval` to check which skills fire for each test phrasing. Test the
near misses: "update my plugins" and "check my plugins" are one word apart and
must route differently.
