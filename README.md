# ai-goods

Claude Code skills by [cloud-simple](https://github.com/cloud-simple), each packaged as its own plugin
in the `ai-goods` plugin marketplace.

| skill | what it does |
|---|---|
| [`plugin-freshness`](#plugin-freshness) | reports whether installed plugins and plugin marketplaces are current, without changing anything |

## Install

From inside Claude Code:

```
/plugin marketplace add cloud-simple/ai-goods
/plugin install plugin-freshness@ai-goods
```

## plugin-freshness

Ask "are my plugins up to date?" or "anything stale in my Claude Code setup?". The skill compares
three layers:

| Layer | Where | Question it answers |
|---|---|---|
| Installed | `claude plugin list --json` | What is running |
| Available | `~/.claude/plugins/marketplaces/<marketplace>/` | What the last marketplace update fetched |
| Upstream | the marketplace's git remote, via `git ls-remote` | What exists now |

The report has:

- a marketplace table: local commit, remote commit, status and auto-update setting
- a plugin table: installed version, available version, marketplace and status (`current`,
  `update available` or `unknown`)
- stale cache directories: old versions of installed plugins
- orphaned cache entries: cached plugins that are no longer installed
- the commands that fix what was found

The skill never runs those commands. It never fetches, updates, installs or deletes. A cache
directory that a running session still uses gets no `rm` command.

Semver plugins are compared by version. Path-based plugins, whose version is a 12-character
marketplace commit hash, are compared by commit.

The check runs on its own too:

```bash
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --no-remote  # offline
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --test       # self-tests
```

Exit codes: `0` everything is current, `1` something needs attention, `2` the check failed.

The self-tests build throwaway marketplaces backed by local git repositories. They cover the five
spec scenarios (everything current, a stale semver plugin, a stale path-based plugin, an orphaned
cache entry, a marketplace behind its remote) and the edge cases around them. They need `git` and
Python 3.9 or newer.

The design spec is [docs/specs/plugin-freshness-spec.md](docs/specs/plugin-freshness-spec.md).
