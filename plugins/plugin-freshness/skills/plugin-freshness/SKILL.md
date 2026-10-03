---
name: plugin-freshness
description: Read-only check of whether Claude Code plugins and plugin marketplaces are up to date. Compares each installed plugin with its local marketplace copy and each marketplace with its git remote, then lists stale and orphaned plugin cache directories and marketplaces with auto-update off. Use when the user asks "are my plugins up to date", "check my plugins", "check marketplace freshness", "anything stale in my Claude Code setup", "is there a newer version of plugin X", "which plugins have updates", or wants an audit of plugin versions or the plugin cache. Do not use when the user asks to actually update, upgrade, install, uninstall or clean up plugins or marketplaces ("update my plugins", "upgrade revmux", "refresh the marketplaces", "delete the old plugin versions"); run those commands directly instead.
---

# Plugin freshness

Report whether every marketplace and every installed plugin is current. The check changes nothing.
Updating is a separate step that the user takes.

## Run the check

```bash
python3 <this skill's base directory>/scripts/plugin_freshness.py
```

It takes about 5 seconds. It runs `claude plugin list --json`, `claude plugin marketplace list --json`
and one `git ls-remote` per marketplace, in parallel.

| Exit code | Meaning |
|---|---|
| 0 | Everything is current. |
| 1 | Something needs attention. This is a normal result, not a failure. |
| 2 | The check failed. Show the error from stderr. |

Use `--no-remote` when the user is offline or asks for a local-only check. Marketplace freshness is
then `unknown`.

## Report the result

1. Lead with one sentence of counts: how many plugins are current, how many have an update, how many
   are `unknown`, and how many cache directories are stale or orphaned.
2. Show the script's output as printed. It is Markdown: a marketplace table, a plugin table, stale
   cache directories, orphaned cache entries, marketplaces with auto-update off, what was not
   checked, and the commands to fix.
3. Explain each `unknown` row with the note the script gives. Do not replace `unknown` with a guess.

## Rules

- Do not run the fix commands. Print them for the user. Run one only when the user asks for it in a
  later message.
- Do not run `git fetch`, `git pull` or `claude plugin marketplace update` to get fresher data. They
  change the marketplace copies. With no name, `claude plugin marketplace update` also takes about
  95 seconds.
- Take installed versions from `claude plugin list --json` only. Directory mtimes do not show which
  version is active: an update can touch the old directory and leave it newer than the active one.

## How the statuses are decided

Three layers are compared:

| Layer | Where | Question it answers |
|---|---|---|
| Installed | `claude plugin list --json` | What is running |
| Available | `~/.claude/plugins/marketplaces/<marketplace>/` | What the last marketplace update fetched |
| Upstream | the marketplace's git remote, via `git ls-remote` | What exists now |

A marketplace is `current` when its local commit equals the remote HEAD. Most local copies are git
clones. `claude-plugins-official` is a downloaded snapshot without `.git`; its commit is in
`.gcs-sha`.

A plugin's version string sets how it is judged:

- **Semver plugin** (for example `1.4.2`): the available version comes from the plugin's
  `plugin.json` in the marketplace copy, or from its `marketplace.json` entry when `plugin.json` has
  none. `plugin.json` wins when both are set. The plugin is `current` only when installed equals
  available and the marketplace is current. When the versions match but the marketplace is behind
  or unreachable, the status is `unknown`: the newer upstream may carry a new version.
- **Path-based plugin** (a 12-character commit hash, for example `context7` and `skill-creator`):
  Claude Code writes the marketplace commit as the version because no manifest has one. The plugin
  is `current` only when that commit equals the marketplace's local commit and the marketplace is
  current. Any newer marketplace commit is a new version.

The cache under `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` is checked too:

- **Stale**: a version directory of an installed plugin that no installation uses.
- **Orphaned**: a cached plugin that is no longer installed.
- A directory with a live process ID in its `.in_use/` folder is still used by a running session.
  The script lists it but prints no `rm` command for it.

`anthropic-plugin-directory` is built in to Claude Code and has no local copy, so it is not checked.

When `known_marketplaces.json` has no `autoUpdate` key, Claude Code's default applies: on for
`claude-plugins-official`, off for third-party marketplaces.
