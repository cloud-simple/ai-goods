---
name: plugin-freshness
description: Check whether Claude Code plugins and plugin marketplaces are up to date, without changing anything. Use this skill whenever the user asks if their plugins or marketplaces are current, outdated, stale or have updates waiting, for example "are my plugins up to date", "check my plugins", "check marketplace freshness", "anything stale in my Claude Code setup", "is there a newer version of skill-creator", "is context7 outdated", or asks which old versions in ~/.claude/plugins/cache are leftovers. Use it instead of reading `claude plugin list` or ~/.claude/plugins by hand, because a manual look misses things: versions mix semver and commit hashes, marketplace copies lag their git remotes, and plugins change without a version bump. Do not use it when the user asks to change something (update, upgrade, install, uninstall, refresh marketplaces, turn on auto-update, delete cache directories); run those commands directly.
---

# Plugin freshness

Report whether every marketplace and every installed plugin is current. The check changes nothing.
Updating is a separate step that the user takes.

## Run the check

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/plugin_freshness.py" --show
```

It takes about 5 seconds. It runs `claude plugin list --json`, `claude plugin marketplace list --json`
and one `git ls-remote` per marketplace, in parallel.

`--show` also opens the report as a page over this session when it runs inside agterm and agterm can
show HTML. The page has the same content as the Markdown, with rendered tables. The script decides this
itself, and the last line of its output says what happened:

- `[agterm overlay] shown: <file>`: the page is open.
- `[agterm overlay] not shown: <reason>`: no page, for example outside agterm, or when the session's
  overlay slot already holds another page, a program or a HUD. The script never replaces those.

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
2. Show the details. Which way depends on the last output line:
   - **`shown`**: the user sees the full report on the page. Say it is open in the agterm overlay,
     and that ⌘W closes it. Do not repeat the tables in the reply. Give only the rows that are not
     `current`, and the `bash` block of fix commands, so the user can copy them from the reply.
   - **`not shown`**, or no such line: paste the script's output into your reply as Markdown,
     unchanged, without the `[agterm overlay]` line. It holds a marketplace table, a plugin table,
     stale cache directories, orphaned cache entries, marketplaces with auto-update off, what was not
     checked, and the commands to fix. Do not wrap the whole output in a code block: a code block shows
     the tables as raw `|` text. The fix commands already sit in their own `bash` block. Mention the
     reason only when the user expected the page.
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
  none. `plugin.json` wins when both are set. Versions are ordered by SemVer rules. A newer
  marketplace version is `update available`. An older one, or two versions that cannot be ordered,
  is `unknown`, so the report never suggests a downgrade.
- **Same version, different files**: when the versions match, the script compares the installed
  files with the marketplace copy. Authors sometimes change a plugin without bumping its version,
  but an edit to the installed copy looks the same, so the row is `unknown` with "same version, but
  N files differ". The fix command reinstalls with `--keep-data`, because `claude plugin update`
  compares versions and can report the plugin as current. Tell the user it replaces local edits.
- **Path-based plugin** (a 12-character commit hash, for example `context7` and `skill-creator`):
  Claude Code writes the marketplace commit as the version because no manifest has one. Here
  `update available` means the marketplace offers another commit, which `claude plugin update`
  installs. The script says so only when the marketplace was refreshed after the install; otherwise
  the row is `unknown`. The local copies are shallow, so git history cannot show which commit is
  newer, and a force-pushed rollback also reads as `update available`. When the commit matches, the
  installed files are compared as for semver plugins.

A plugin is `current` only when its version and files match the marketplace copy and the
marketplace is current. Any check that cannot run gives `unknown`, never `current`.

The cache under `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` is checked too:

- **Stale**: a version directory of an installed plugin that no installation uses.
- **Orphaned**: a cached plugin that is no longer installed.
- The directory named by the plugin's `installPath` is never stale.
- A directory with a live process ID in its `.in_use/` folder is still used by a running session.
  The script lists it but prints no `rm` command for it. It also prints none when the plugin's
  active version is unknown.

`anthropic-plugin-directory` is built in to Claude Code and has no local copy, so it is not checked.

When `known_marketplaces.json` has no `autoUpdate` key, Claude Code's default applies: on for
`claude-plugins-official`, off for third-party marketplaces.
