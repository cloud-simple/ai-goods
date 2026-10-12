---
worth: yes
added: 2026-10-12
---
# report where each plugin and marketplace comes from

`plugin-freshness` says whether things are current but not where they come from. Asked "can you provide
origins for all the plugins/marketplaces?", the answer had to be assembled by hand from four sources the
script already reads or sits next to. Add it to the report.

What the manual answer used:

| Field | Source |
|---|---|
| Marketplace source as recorded | `known_marketplaces.json` → `source` (`github` repo, `git` url, …) |
| What the local copy fetches from | `git -C <installLocation> remote get-url origin`; `.gcs-sha` snapshot for `claude-plugins-official`, which has no remote |
| Plugin path in the marketplace repo | `marketplace.json` entry `source` (relative path such as `./plugins/x`, or `./` for a repo-root plugin) |
| Plugin hosted elsewhere | `marketplace.json` entry `source` as an object (`github`, `url`, `git-subdir`, `archive`, `npm`) |
| Plugin's own repo claim | `plugin.json` → `repository`, else `homepage`; the official marketplace's plugins set neither |

Worth showing beside the URL: the protocol (SSH or HTTPS). On 2026-10-12 the clones were a mix that
Claude Code chose at clone time, which mattered when switching all of them to HTTPS.

Open design choices for the fix:

- A separate `--origins` mode or a section in the default report. The default report is already long;
  a mode keeps the freshness answer short.
- How to show a plugin path: repo plus path, or a GitHub `tree/<ref>/<path>` link.
- `SKILL.md` needs a trigger phrase for this ("where do my plugins come from", "plugin origins") without
  pulling in requests to change sources. Re-run the trigger eval with `run_eval.py --num-workers 1`;
  parallel workers give false misses (see `evals/plugin-freshness/trigger-evals.json`).

Must stay read-only like the rest of the script: `git remote get-url` and file reads only, no fetch.
