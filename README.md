# ai-goods

Claude Code skills and hooks by [cloud-simple](https://github.com/cloud-simple), each packaged as its
own plugin in the `ai-goods` plugin marketplace.

| plugin | what it does |
|---|---|
| [`plugin-freshness`](#plugin-freshness) | reports whether installed plugins and plugin marketplaces are current, without changing anything |
| [`skill-creator-guard`](#skill-creator-guard) | hooks that keep skill-creator's trigger evals honest and show what a run costs before it starts |

## Install

From inside Claude Code:

```
/plugin marketplace add cloud-simple/ai-goods
/plugin install plugin-freshness@ai-goods
/plugin install skill-creator-guard@ai-goods
```

From the command line:

```bash
claude plugin marketplace add cloud-simple/ai-goods
claude plugin install plugin-freshness@ai-goods
claude plugin install skill-creator-guard@ai-goods
```

Both install at user scope, for every project. Add `--scope project` to `claude plugin install` to install
for the current project only. The `skill-creator-guard` hooks come with the plugin: installing it is
the whole setup, with no `settings.json` change.

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
nearest `.claude/commands/` folder, then starts a `claude -p` session to see whether Claude loads it.
Four things go wrong:

- **Parallel runs score too low.** With several runs at once (the default is 10), Claude loads
  another run's copy, and the run counts as a miss. A skill that always triggers scores about 1 in
  the number of runs. With one run at a time, the same description scored 30 of 30.
- **The copies leak into other sessions.** skill-creator runs its scripts from inside the plugin
  cache. The nearest `.claude/` folder above it is your home folder's, so the copies land in
  `~/.claude/commands/` and appear in every running Claude Code session.
- **A run costs more than it looks.** Every test is a separate `claude -p` session that loads your
  whole Claude Code setup, MCP servers included, and counts toward your usage limit. With the
  defaults, 3 runs per query and 5 rounds, `run_loop` on a 20-query eval set starts up to 300 of
  them, plus 1 or 2 more for each description rewrite. In `run_loop`, `--model` sets the model of
  every test session too. Nothing shows those numbers before the run starts.
- **Failed tests count as "not triggered".** A test session that fails, for example once the usage
  limit is reached, counts as "not triggered", and `run_eval` prints no error for it. A
  should-trigger query then fails and a should-not-trigger query passes: a round cut short by the
  limit reports a collapsed recall and a perfect-looking precision, and neither says anything about
  the description.

The plugin adds two hooks:

| Hook | Fires on | What it does |
|---|---|---|
| `PostToolUse`, matcher `Skill` | skill-creator loading | adds the eval rules to Claude's context |
| `PreToolUse`, matcher `Bash` | a command that runs `run_eval` or `run_loop` | blocks it, with the reason and the rules, when it lacks `--num-workers 1`, leaves bytecode on (neither `PYTHONDONTWRITEBYTECODE=1` nor `python -B`), runs where the copies would reach `~/.claude`, a git repository or, through a linked `.claude/` folder, anywhere else, leaves MCP servers on for the test sessions, or lacks an option the script requires; otherwise asks you to confirm the run. A `--help` run starts no session and writes no skill copy, so only its bytecode is checked, and it gets no prompt |

The test sessions start in the scratch folder and read its `.claude/settings.json`. The hook requires
that file to set `allowedMcpServers`:

```json
{"allowedMcpServers": []}
```

With `[]`, no test session starts an MCP server. Tested on Claude Code 2.1.296: three user-scope
servers and a claude.ai connector all stayed off, while the other settings and the plugins still
loaded. A test session was ready for its first model call in about 1.1 s instead of 2.3 s, with 25
tools instead of 50. To keep a server the skill under test competes with, list it:
`{"allowedMcpServers": [{"serverName": "aws-pricing"}]}`.

The confirmation shows the size of the run, read from its options, its eval set file and that
settings file:

```
Confirm a skill-creator eval run. run_loop starts up to 300 test sessions (20 queries × 3 runs ×
5 rounds) and up to 8 more sessions for up to 4 description rewrites, on claude-fable-5-1, with no
MCP servers. Each test session is a separate `claude -p` run that loads your whole Claude Code
setup and counts toward your usage limit.
```

The number is a maximum: `run_loop` stops early when every training query passes. The prompt appears
in auto mode too. A session that cannot show a prompt, such as `claude -p`, denies the run, and
Claude gets the size as the reason.

The rules Claude gets when skill-creator loads cover the same ground: one worker, a scratch folder
with MCP servers off, no bytecode, the cost of a run, one `run_eval` pass before any `run_loop`, and
how to spot a round that failed sessions spoiled. They also set the `run_loop` options:

| Option | Rule | Why |
|---|---|---|
| `--max-iterations` | `3` unless you ask for more | caps the worst case at 3 rounds instead of 5; a loop that passes every training query stops earlier anyway |
| `--runs-per-query` | keep the default `3`, unless you agree to `1` for a rough look | with one run, a query that triggers only some of the time passes or fails by chance |
| `--holdout` | never `0` | the held-out queries run in every round anyway, so `0` saves no sessions; it only makes `run_loop` pick the best description by the score the rewrites were tuned against |

The `Bash` hook reads the command the way a shell splits it:
- **What it parses:** quotes, comments, heredocs, `;` `&&` `||` `|`, subshells and `$( )`.
- **What it tracks:** `cd`, `export` and `mkdir` as it goes.
- **What counts as a run:** `python -m scripts.run_eval`, a path to `run_eval.py`, or the script run
  directly, also behind `env`, `nohup`, `uv run` or `bash -c`.
- **What it checks:** workers, bytecode, folder and the folder's MCP setting, against that command
  only. The settings file must exist before the command runs, so write it in an earlier step.
- **Whose script it is:** a script that resolves to a file counts only when its skill folder's
  `SKILL.md` says `name: skill-creator`, so another project's own `scripts/run_eval.py` passes. A
  script it can't resolve still counts.
- **Folder it can't work out:** a run whose folder depends on something it can't know, such as an
  unset variable or `cd -`, is blocked. Write the scratch folder as a literal path.

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
