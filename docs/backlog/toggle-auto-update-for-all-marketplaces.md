---
worth: yes
added: 2026-10-12
---
# turn plugin auto-update on or off for all marketplaces at once

The report lists marketplaces with auto-update off, but changing that means visiting each one in
`/plugin` > Marketplaces. Add one switch that sets auto-update on, or off, for every marketplace at once.
Per-marketplace control is not needed.

Auto-update in Claude Code is per marketplace, and it updates the marketplace and its installed plugins at
startup. So "all plugins" means every marketplace. State on 2026-10-12, Claude Code 2.1.296:

- **Command line:** `claude plugin marketplace` has no auto-update option. The UI is the only built-in way.
- **Where the value lives:** `~/.claude/settings.json` → `extraKnownMarketplaces.<name>.autoUpdate`, a
  documented settings key. `~/.claude/plugins/known_marketplaces.json` mirrors it. That file is Claude
  Code's internal state, so write `settings.json` only.
- **Missing key:** the default applies: on for `claude-plugins-official`, off for third-party
  marketplaces. On this machine 4 of 9 marketplaces set it explicitly.
- **`claude-plugins-official`:** not declared in `extraKnownMarketplaces`. Turning it off needs a
  declaration. Whether Claude Code accepts that declaration for a reserved official name is unverified.

The skill is read-only by design. Its spec forbids changing settings, and its description routes "turn on
auto-update for the agterm marketplace" away from it. So build this as a second skill in the same plugin,
for example `plugin-autoupdate`, with its own script and description. Keep `plugin-freshness` read-only.

What the fix needs:

- **Commands:** `on`, `off` and `status`. Show the planned change and ask before writing.
- **The edit itself:** change only the `autoUpdate` keys, keep the rest of `settings.json` byte-for-byte,
  back the file up first, and validate the JSON after.
- **Timing:** say the change applies at the next Claude Code start.
- **Self-tests:** a `--test` flag running against a fixture `settings.json`, like `plugin_freshness.py`.
- **Trigger eval:** for both skills, with the near misses swapped, run with `run_eval.py --num-workers 1`.
