# ai-goods

Claude Code skills and hooks by [cloud-simple](https://github.com/cloud-simple), each packaged as its
own plugin in the `ai-goods` plugin marketplace.

| plugin | what it does |
|---|---|
| [`plugin-freshness`](#plugin-freshness) | reports whether installed plugins and plugin marketplaces are current, without changing anything |
| [`skill-creator-guard`](#skill-creator-guard) | hooks that keep skill-creator's trigger evals honest |

## Install

From inside Claude Code:

```
/plugin marketplace add cloud-simple/ai-goods
/plugin install plugin-freshness@ai-goods
/plugin install skill-creator-guard@ai-goods
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

Inside [agterm](https://github.com/umputun/agterm), the skill also opens the report as a page over the
session, with rendered tables in the terminal's theme colors. It checks that agterm can show HTML
first. Outside agterm, or when the session's overlay already shows something else, the reply carries
the Markdown report as before.

Semver plugins are compared by version. Path-based plugins, whose version is a 12-character
marketplace commit hash, are compared by commit. When the version or commit matches, the installed
files are compared with the marketplace copy too. A mismatch is reported as `unknown`: a plugin
changed without a version bump and a local edit look the same.

The check runs on its own too:

```bash
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --no-remote  # offline
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --show       # also open the page in agterm
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --html report.html  # also write the page to a file
python3 plugins/plugin-freshness/skills/plugin-freshness/scripts/plugin_freshness.py --test       # self-tests
```

Exit codes: `0` everything is current, `1` something needs attention, `2` the check failed.

The self-tests build throwaway marketplaces backed by local git repositories. They cover the five
spec scenarios (everything current, a stale semver plugin, a stale path-based plugin, an orphaned
cache entry, a marketplace behind its remote) and the edge cases around them. They need `git` and
Python 3.9 or newer.

The design spec is [docs/specs/plugin-freshness-spec.md](docs/specs/plugin-freshness-spec.md).

## skill-creator-guard

Hooks that keep [skill-creator](https://github.com/anthropics/claude-plugins-official)'s trigger evals
honest. skill-creator's `scripts/run_eval.py` and `scripts/run_loop.py` measure how often a skill
description makes Claude load the skill. For each test run they write a copy of the skill to the
nearest `.claude/commands/` folder. Two things go wrong:

- **Parallel runs score too low.** With several runs at once (the default is 10), Claude loads
  another run's copy, and the run counts as a miss. A skill that always triggers scores about 1 in
  the number of runs. With one run at a time, the same description scored 30 of 30.
- **The copies leak into other sessions.** skill-creator runs its scripts from inside the plugin
  cache. The nearest `.claude/` folder above it is your home folder's, so the copies land in
  `~/.claude/commands/` and appear in every running Claude Code session.

The plugin adds two hooks:

| Hook | Fires on | What it does |
|---|---|---|
| `PostToolUse`, matcher `Skill` | skill-creator loading | adds the eval rules to Claude's context |
| `PreToolUse`, matcher `Bash` | a command that runs `run_eval` or `run_loop` | blocks it, with the reason and the rules, when it lacks `--num-workers 1`, leaves bytecode on (neither `PYTHONDONTWRITEBYTECODE=1` nor `python -B`), or runs where the copies would reach `~/.claude` or a git repository |

The `Bash` hook reads the command the way a shell splits it:
- **What it parses:** quotes, comments, heredocs, `;` `&&` `||` `|`, subshells and `$( )`.
- **What it tracks:** `cd`, `export` and `mkdir` as it goes.
- **What counts as a run:** `python -m scripts.run_eval`, a path to `run_eval.py`, or the script run
  directly, also behind `env`, `nohup`, `uv run` or `bash -c`.
- **What it checks:** workers, bytecode and folder, against that command only.

Text that only mentions the script, such as an `echo`, a `claude -p` prompt, a commit message or a
comment, passes, and so does every other command. If the hook itself fails, the command goes ahead.
It guards against mistakes, not deliberate evasion: `eval`, `source`, `python -c` and scripts written
to a file are not followed.

The hooks need `python3` 3.9 or newer. Self-tests, which build a throwaway home folder and never
touch the real `~/.claude`:

```bash
python3 plugins/skill-creator-guard/scripts/skill_creator_guard.py --test
```

The bug is reported as anthropics/claude-plugins-official#1749 and #5685, with a proposed fix in
anthropics/skills#1261. Remove this plugin once skill-creator's `run_eval.py` gives each run its own
project folder.
