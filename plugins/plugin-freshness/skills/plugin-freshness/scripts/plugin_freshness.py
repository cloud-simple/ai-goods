#!/usr/bin/env python3
"""Report whether Claude Code marketplaces and installed plugins are current. Read-only.

Three layers are compared:

  installed  what `claude plugin list --json` says is running
  available  the local marketplace copy under ~/.claude/plugins/marketplaces/<name>/
  upstream   the marketplace's remote, read with `git ls-remote` (never `git fetch`)

The report has a marketplace table, a plugin table, the stale and orphaned cache
directories, the marketplaces with auto-update off, and the commands that fix what
was found. The script never runs those commands. It writes nothing except the HTML
page that --html or --show asks for.

--show also opens that page over this agterm session when agterm can show HTML, and
ends the output with one line saying whether it did.

Exit codes: 0 everything is current, 1 something needs attention, 2 the check failed.

Self-tests:  plugin_freshness.py --test
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import filecmp
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

CURRENT = "current"
UPDATE = "update available"
UNKNOWN = "unknown"

# Marketplaces whose auto-update is on when known_marketplaces.json has no autoUpdate key.
# Claude Code turns it on by default for official Anthropic marketplaces and off for the rest.
AUTO_UPDATE_ON_BY_DEFAULT = {"claude-plugins-official"}

# Built in to Claude Code. It has no copy under marketplaces/, so it has no local HEAD.
BUILT_IN_MARKETPLACES = {"anthropic-plugin-directory"}

# Claude Code writes a 12-character commit hash as the version of a plugin that declares none.
COMMIT_VERSION = re.compile(r"[0-9a-f]{12}|[0-9a-f]{40}")
SEMVER = re.compile(r"v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
FULL_SHA = re.compile(r"[0-9a-f]{40}")

# Not plugin content: Claude Code's in-use markers, git metadata and Python bytecode.
IGNORED_NAMES = {".in_use", ".git", "__pycache__", ".DS_Store"}

CLI_TIMEOUT = 60
REMOTE_TIMEOUT = 20


@dataclass
class Marketplace:
    name: str
    location: str
    source: dict = field(default_factory=dict)
    kind: Optional[str] = None  # "git" clone, "snapshot" with .gcs-sha, or None
    local_head: Optional[str] = None
    remote_head: Optional[str] = None
    problem: Optional[str] = None
    auto_update: Optional[bool] = None
    auto_update_explicit: bool = False
    manifest: Optional[dict] = None
    last_updated: Optional[str] = None  # when Claude Code last refreshed the local copy

    @property
    def status(self) -> str:
        if self.local_head is None or self.remote_head is None:
            return UNKNOWN
        return CURRENT if self.local_head == self.remote_head else UPDATE


@dataclass
class Plugin:
    id: str
    name: str
    marketplace: str
    installed: str
    scope: str = "user"
    project_path: Optional[str] = None
    enabled: bool = True
    install_path: Optional[str] = None
    last_updated: Optional[str] = None  # when this installation was last installed or updated
    git_commit_sha: Optional[str] = None  # marketplace commit this installation came from
    regime: str = ""
    available: Optional[str] = None
    status: str = UNKNOWN
    note: str = ""
    changed_files: int = 0  # files that differ from the marketplace copy under the same version


@dataclass
class CacheEntry:
    path: str
    plugin_id: str
    version: str
    installed: list = field(default_factory=list)
    in_use: list = field(default_factory=list)  # live PIDs from <dir>/.in_use/
    uncertain: bool = False  # the plugin's active version is unknown, so this directory may be the active one

    @property
    def deletable(self) -> bool:
        return not self.in_use and not self.uncertain


@dataclass
class Report:
    marketplaces: list
    plugins: list
    stale: list
    orphans: list
    skipped: list

    def needs_attention(self) -> bool:
        return (any(m.status != CURRENT for m in self.marketplaces)
                or any(p.status != CURRENT for p in self.plugins)
                or bool(self.stale) or bool(self.orphans))


# ---------------------------------------------------------------- helpers

def run(cmd: list, timeout: int, env: Optional[dict] = None) -> tuple:
    """Run a command and return (exit code, stdout, stderr). A timeout or a missing binary is exit code -1."""
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except OSError as e:
        return -1, "", str(e)


def git_env() -> dict:
    """Environment for git that fails instead of waiting on a password or a host-key prompt."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
    return env


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cli_json(cmd: list, override: Optional[str]):
    """Read a JSON list from a file when given, otherwise from the claude CLI."""
    if override:
        data = read_json(override)
        if data is None:
            raise SystemExit(f"error: cannot read JSON from {override}")
        return data
    rc, out, err = run(cmd, CLI_TIMEOUT)
    if rc != 0:
        raise SystemExit(f"error: `{' '.join(cmd)}` failed: {err.strip() or out.strip()}")
    try:
        return json.loads(out)
    except ValueError:
        raise SystemExit(f"error: `{' '.join(cmd)}` did not print JSON")


def parse_time(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)  # Claude Code writes UTC


def compare_versions(a: str, b: str) -> Optional[int]:
    """-1, 0 or 1 by SemVer precedence, or None when either string is not a version."""
    ma, mb = SEMVER.fullmatch(a), SEMVER.fullmatch(b)
    if not ma or not mb:
        return None
    ca, cb = [int(x) for x in ma.group(1).split(".")], [int(x) for x in mb.group(1).split(".")]
    width = max(len(ca), len(cb))
    ca, cb = ca + [0] * (width - len(ca)), cb + [0] * (width - len(cb))
    if ca != cb:
        return -1 if ca < cb else 1
    pa, pb = ma.group(2), mb.group(2)
    if pa == pb:
        return 0
    if pa is None or pb is None:
        return 1 if pa is None else -1  # a pre-release sorts before its release
    for x, y in zip(pa.split("."), pb.split(".")):
        if x == y:
            continue
        if x.isdigit() and y.isdigit():
            return -1 if int(x) < int(y) else 1
        if x.isdigit() != y.isdigit():
            return -1 if x.isdigit() else 1
        return -1 if x < y else 1
    return (len(pa.split(".")) > len(pb.split("."))) - (len(pa.split(".")) < len(pb.split(".")))


def detect_regime(version: Optional[str]) -> str:
    """'commit' for a path-based plugin, 'semver' for a manifest version, 'none' when there is no version."""
    if not version or version == "unknown":
        return "none"
    if COMMIT_VERSION.fullmatch(version):
        return "commit"
    return "semver"


# ---------------------------------------------------------------- marketplaces

def read_local_head(location: str) -> tuple:
    """Return (kind, head, problem) for a marketplace copy."""
    if not os.path.isdir(location):
        return None, None, "no local copy"
    if os.path.exists(os.path.join(location, ".git")):
        rc, out, err = run(["git", "-C", location, "rev-parse", "HEAD"], REMOTE_TIMEOUT, git_env())
        if rc != 0:
            return "git", None, f"git rev-parse failed: {err.strip()}"
        return "git", out.strip(), None
    sha_file = os.path.join(location, ".gcs-sha")
    if os.path.isfile(sha_file):
        with open(sha_file, encoding="utf-8") as f:
            sha = f.read().strip()
        if FULL_SHA.fullmatch(sha):
            return "snapshot", sha, None
        return "snapshot", None, ".gcs-sha does not hold a commit hash"
    return None, None, "local copy has no git repository and no .gcs-sha"


def remote_url(m: Marketplace) -> Optional[str]:
    src = m.source or {}
    if src.get("source") == "github" and src.get("repo"):
        return f"https://github.com/{src['repo']}.git"
    if src.get("source") == "git" and src.get("url"):
        return src["url"]
    return None


def parse_ls_remote(out: str, ref: str) -> Optional[str]:
    refs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    if ref == "HEAD":
        return refs.get("HEAD")
    for name in (f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if name in refs:
            return refs[name]
    return None


def read_remote_head(m: Marketplace) -> tuple:
    """Return (head, problem). Uses `git ls-remote` only, which does not change the clone."""
    ref = (m.source or {}).get("ref") or "HEAD"
    if m.kind == "git":
        cmd = ["git", "-C", m.location, "ls-remote", "origin", ref]
    else:
        url = remote_url(m)
        if url is None:
            return None, "source is not a git repository"
        cmd = ["git", "ls-remote", url, ref]
    rc, out, err = run(cmd, REMOTE_TIMEOUT, git_env())
    if rc != 0:
        lines = [x.strip() for x in err.splitlines() if x.strip()]
        detail = " ".join(lines)[:200] if lines else "no output"
        return None, f"remote not reachable: {detail}"
    head = parse_ls_remote(out, ref)
    if head is None:
        return None, f"remote has no {ref}"
    return head, None


def load_marketplaces(listing: list, plugins_dir: str, check_remote: bool) -> list:
    known = read_json(os.path.join(plugins_dir, "known_marketplaces.json")) or {}
    result = []
    for entry in listing:
        name = entry.get("name")
        if not name:
            continue
        k = known.get(name, {})
        location = entry.get("installLocation") or k.get("installLocation") or os.path.join(plugins_dir, "marketplaces", name)
        m = Marketplace(name=name, location=location, source=k.get("source") or {
            key: entry[key] for key in ("source", "repo", "url", "ref") if key in entry})
        m.last_updated = k.get("lastUpdated")
        if "autoUpdate" in k:
            m.auto_update, m.auto_update_explicit = bool(k["autoUpdate"]), True
        else:
            m.auto_update = name in AUTO_UPDATE_ON_BY_DEFAULT
        m.kind, m.local_head, m.problem = read_local_head(location)
        m.manifest = read_json(os.path.join(location, ".claude-plugin", "marketplace.json"))
        result.append(m)

    if check_remote:
        todo = [m for m in result if m.local_head is not None]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for m, (head, problem) in zip(todo, pool.map(read_remote_head, todo)):
                m.remote_head = head
                m.problem = m.problem or problem
    else:
        for m in result:
            m.problem = m.problem or "remote not checked (--no-remote)"
    return result


# ---------------------------------------------------------------- plugins

def find_entry(m: Marketplace, name: str) -> Optional[dict]:
    for e in (m.manifest or {}).get("plugins", []) or []:
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def local_plugin_dir(m: Marketplace, entry: dict) -> Optional[str]:
    """The plugin's directory inside the marketplace copy, or None for a plugin hosted elsewhere."""
    src = entry.get("source")
    if not isinstance(src, str):
        return None
    root = ((m.manifest or {}).get("metadata") or {}).get("pluginRoot")
    if root and not src.startswith(("./", "../")):
        src = os.path.join(root, src)
    base = os.path.realpath(m.location)
    path = os.path.realpath(os.path.join(base, src))
    if path != base and not path.startswith(base + os.sep):
        return None
    return path


def available_version(m: Marketplace, entry: dict) -> Optional[str]:
    """plugin.json wins over the marketplace entry, as in Claude Code."""
    pdir = local_plugin_dir(m, entry)
    if pdir:
        manifest = read_json(os.path.join(pdir, ".claude-plugin", "plugin.json")) or {}
        if manifest.get("version"):
            return str(manifest["version"])
    return str(entry["version"]) if entry.get("version") else None


def raise_error(err: OSError) -> None:
    raise err


def plugin_files(root: str) -> dict:
    """Map relative path -> full path. Raises OSError for a directory it cannot read."""
    out = {}
    for d, dirs, files in os.walk(root, onerror=raise_error):
        dirs[:] = [x for x in dirs if x not in IGNORED_NAMES]
        for f in files:
            if f not in IGNORED_NAMES and not f.endswith(".pyc"):
                full = os.path.join(d, f)
                out[os.path.relpath(full, root)] = full
    return out


def count_changed_files(installed_dir: str, market_dir: str) -> Optional[int]:
    """Number of files added, removed or changed between two plugin directories, or None when one is missing."""
    if not (os.path.isdir(installed_dir) and os.path.isdir(market_dir)):
        return None
    try:
        a, b = plugin_files(installed_dir), plugin_files(market_dir)
        changed = len(set(a) ^ set(b))
        for rel in set(a) & set(b):
            if not filecmp.cmp(a[rel], b[rel], shallow=False):
                changed += 1
    except OSError:
        return None  # a file or directory could not be read, so no comparison ran
    return changed


def files_differ_note(p: Plugin, m: Marketplace, n: int) -> str:
    """State what was observed. A local edit and an upstream change look the same, so no cause is claimed."""
    note = f"same version, but {n} file{' differs' if n == 1 else 's differ'} from the marketplace copy"
    if p.git_commit_sha and m.local_head and p.git_commit_sha != m.local_head:
        note += f"; installed from commit {p.git_commit_sha[:12]}, marketplace copy is at {m.local_head[:12]}"
    return note


def fetched_after_install(p: Plugin, m: Marketplace) -> bool:
    """True when the marketplace copy was refreshed after this installation, from recorded timestamps."""
    market, plugin = parse_time(m.last_updated), parse_time(p.last_updated)
    return bool(market and plugin and market > plugin)


def remote_clause(m: Marketplace) -> str:
    if m.status == UPDATE:
        return "marketplace copy is behind its remote"
    return m.problem or "marketplace remote unknown"


def judge(p: Plugin, markets: dict) -> None:
    m = markets.get(p.marketplace)
    p.regime = detect_regime(p.installed)
    if m is None or m.kind is None:
        p.note = ("built in; no local copy to compare" if p.marketplace in BUILT_IN_MARKETPLACES
                  else "marketplace has no local copy")
        return
    entry = find_entry(m, p.name)
    if entry is None:
        p.note = "no longer listed in the marketplace copy"
        return
    if p.regime == "none":
        p.note = "installed version is not recorded"
        return

    if p.regime == "semver":
        p.available = available_version(m, entry)
        if p.available is None:
            p.note = "marketplace copy declares no version"
            return
        order = 0 if p.available == p.installed else compare_versions(p.installed, p.available)
        if order is None:
            p.note = "versions differ and cannot be ordered"
            return
        if order > 0:
            p.note = "installed version is newer than the marketplace copy"
            return
        if order == 0 and p.available != p.installed:
            p.note = "versions are written differently but rank equal (build metadata or format)"
            return
        if order < 0:
            p.status, p.note = UPDATE, "marketplace copy has a newer version"
            return
        n = count_changed_files(p.install_path or "", local_plugin_dir(m, entry) or "")
        if n is None:
            p.note = "installed files could not be compared with the marketplace copy"
        elif n:
            p.changed_files, p.note = n, files_differ_note(p, m, n)
        elif m.status == CURRENT:
            p.status = CURRENT
        else:
            p.note = f"same version and files locally; {remote_clause(m)}"
        return

    # Path-based: the version is the marketplace commit the plugin was installed from.
    if local_plugin_dir(m, entry) is None:
        p.note = "hosted outside the marketplace; its own repository is not checked"
        return
    if m.local_head is None:
        p.note = m.problem or "marketplace commit unknown"
        return
    p.available = m.local_head[:len(p.installed)]
    if p.available != p.installed:
        # "Update" here means the marketplace now offers another commit, which `claude plugin update` installs.
        # Shallow copies cannot show whether that commit is a descendant of the installed one.
        if fetched_after_install(p, m):
            p.status, p.note = UPDATE, "marketplace offers another commit, refreshed after the install"
        else:
            p.note = "commits differ, and the marketplace copy is not known to be the later one"
        return
    n = count_changed_files(p.install_path or "", local_plugin_dir(m, entry) or "")
    if n is None:
        p.note = "installed files could not be compared with the marketplace copy"
    elif n:
        p.changed_files, p.note = n, files_differ_note(p, m, n)
    elif m.status == CURRENT:
        p.status = CURRENT
    elif m.status == UPDATE:
        p.status, p.note = UPDATE, "marketplace copy is behind its remote"
    else:
        p.note = remote_clause(m)


def load_provenance(plugins_dir: str) -> dict:
    """installPath -> gitCommitSha from installed_plugins.json. The CLI listing does not carry the commit."""
    data = read_json(os.path.join(plugins_dir, "installed_plugins.json")) or {}
    out = {}
    for entries in (data.get("plugins") or {}).values():
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and e.get("installPath") and e.get("gitCommitSha"):
                out[os.path.realpath(e["installPath"])] = e["gitCommitSha"]
    return out


def load_plugins(listing: list, markets: dict, provenance: Optional[dict] = None) -> list:
    provenance = provenance or {}
    result = []
    for e in listing:
        pid = e.get("id", "")
        if "@" not in pid:
            continue
        name, market = pid.rsplit("@", 1)
        p = Plugin(id=pid, name=name, marketplace=market, installed=str(e.get("version") or ""),
                   scope=e.get("scope", "user"), project_path=e.get("projectPath"),
                   enabled=bool(e.get("enabled", True)), install_path=e.get("installPath"),
                   last_updated=e.get("lastUpdated") or e.get("installedAt"))
        if p.install_path:
            p.git_commit_sha = provenance.get(os.path.realpath(p.install_path))
        judge(p, markets)
        result.append(p)
    return result


# ---------------------------------------------------------------- cache

def list_dirs(path: str) -> list:
    try:
        return sorted(d for d in os.listdir(path)
                      if not d.startswith(".") and os.path.isdir(os.path.join(path, d)))
    except OSError:
        return []


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the process exists but belongs to someone else, or a sandbox blocks the probe
    except OSError:
        return False
    return True


def live_users(path: str) -> list:
    """PIDs of running sessions that marked this cache directory in use (<dir>/.in_use/<pid>)."""
    try:
        names = os.listdir(os.path.join(path, ".in_use"))
    except OSError:
        return []
    return sorted(int(n) for n in names if n.isdigit() and pid_alive(int(n)))


def scan_cache(plugins_dir: str, plugins: list) -> tuple:
    """Stale: a version directory of an installed plugin that no installation uses.
    Orphan: a cached plugin with no installation at all. Directory mtimes are never read."""
    root = os.path.join(plugins_dir, "cache")
    installed: dict = {}
    uncertain = set()
    for p in plugins:
        versions = installed.setdefault((p.marketplace, p.name), set())
        if p.installed:
            versions.add(p.installed)
        if p.install_path:
            # The directory the CLI says is active is never stale, whatever its name.
            versions.add(os.path.basename(os.path.normpath(p.install_path)))
        elif detect_regime(p.installed) == "none":
            uncertain.add((p.marketplace, p.name))
    stale, orphans = [], []
    for market in list_dirs(root):
        for name in list_dirs(os.path.join(root, market)):
            versions = installed.get((market, name))
            for ver in list_dirs(os.path.join(root, market, name)):
                entry = CacheEntry(os.path.join(root, market, name, ver), f"{name}@{market}", ver,
                                   sorted(versions or []), live_users(os.path.join(root, market, name, ver)))
                if versions is None:
                    orphans.append(entry)
                elif ver not in versions:
                    entry.uncertain = (market, name) in uncertain
                    stale.append(entry)
    return stale, orphans


# ---------------------------------------------------------------- output

def build_report(plugins_dir: str, plugin_list: list, market_list: list, check_remote: bool) -> Report:
    markets = load_marketplaces(market_list, plugins_dir, check_remote)
    by_name = {m.name: m for m in markets}
    plugins = load_plugins(plugin_list, by_name, load_provenance(plugins_dir))
    stale, orphans = scan_cache(plugins_dir, plugins)
    skipped = sorted(BUILT_IN_MARKETPLACES - set(by_name))
    for p in plugins:
        if p.marketplace not in by_name and p.marketplace not in skipped:
            skipped.append(p.marketplace)
    return Report(markets, plugins, stale, orphans, skipped)


def short(sha: Optional[str]) -> str:
    return sha[:12] if sha else "-"


def fix_commands(r: Report) -> list:
    cmds = []
    for m in r.marketplaces:
        if m.status == UPDATE:
            cmds.append(f"claude plugin marketplace update {shlex.quote(m.name)}")
    for p in r.plugins:
        if p.status != UPDATE and not p.changed_files:
            continue
        pid = shlex.quote(p.id)
        scoped = p.scope in ("project", "local") and p.project_path
        flag = f" --scope {p.scope}" if scoped else ""
        if p.changed_files:
            # The version did not change, so `claude plugin update` can report it as current. Reinstall instead.
            cmd = f"claude plugin uninstall --keep-data{flag} {pid} && claude plugin install{flag} {pid}"
        else:
            cmd = f"claude plugin update {pid}{flag}"
        if scoped:
            cmd = f"(cd {shlex.quote(p.project_path)} && {cmd})"
        if p.changed_files:
            cmd += "  # replaces the installed files, local edits included"
        cmds.append(cmd)
    for c in r.stale + r.orphans:
        if c.deletable:
            cmds.append(f"rm -rf {shlex.quote(c.path)}")
    return cmds


def cell(s) -> str:
    return str(s).replace("|", "\\|")


def in_use_note(c: CacheEntry) -> str:
    notes = []
    if c.in_use:
        notes.append(f"in use by running session PID {', '.join(map(str, c.in_use))}")
    if c.uncertain:
        notes.append("the active version is unknown, so this may be the active directory")
    return "".join(f"; {n}" for n in notes)


def render(r: Report) -> str:
    out = ["## Marketplaces", "",
           "| marketplace | local | remote | status | auto-update | note |",
           "|---|---|---|---|---|---|"]
    for m in r.marketplaces:
        auto = ("on" if m.auto_update else "off") + ("" if m.auto_update_explicit else " (default)")
        note = "" if m.status == CURRENT else (m.problem or "local copy differs from remote")
        out.append(f"| {cell(m.name)} | {short(m.local_head)} | {short(m.remote_head)} | {m.status} | {auto} | {cell(note)} |")

    out += ["", "## Plugins", "",
            "| id | installed | available | marketplace | status | note |",
            "|---|---|---|---|---|---|"]
    for p in sorted(r.plugins, key=lambda p: (p.status == CURRENT, p.id)):
        extra = []
        if p.scope != "user":
            extra.append(f"{p.scope} scope" + (f": {p.project_path}" if p.project_path else ""))
        if not p.enabled:
            extra.append("disabled")
        note = "; ".join([x for x in [p.note] + extra if x])
        out.append(f"| {cell(p.id)} | {cell(p.installed or '-')} | {cell(p.available or '-')} | "
                   f"{cell(p.marketplace)} | {p.status} | {cell(note)} |")

    out += ["", "## Stale cache directories", ""]
    out += [f"- `{c.path}` (installed: {', '.join(c.installed) or 'unknown'}){in_use_note(c)}" for c in r.stale] or ["None."]
    out += ["", "## Orphaned cache entries", ""]
    out += [f"- `{c.path}` ({c.plugin_id} is not installed){in_use_note(c)}" for c in r.orphans] or ["None."]

    off = [m for m in r.marketplaces if not m.auto_update]
    out += ["", "## Auto-update off", ""]
    out += [f"- {m.name}" + ("" if m.auto_update_explicit else " (default for a third-party marketplace)")
            for m in off] or ["None."]

    if r.skipped:
        out += ["", "## Not checked", ""]
        for s in r.skipped:
            why = ("built in to Claude Code; it has no local copy under marketplaces/, so there is no local HEAD to compare"
                   if s in BUILT_IN_MARKETPLACES else "no local copy under marketplaces/")
            out.append(f"- {s}: {why}")

    cmds = fix_commands(r)
    out += ["", "## Commands to fix (not run)", ""]
    if cmds:
        out += ["```bash"] + cmds + ["```", ""]
        if any(m.status == UPDATE for m in r.marketplaces):
            out.append("Run the marketplace updates first, then the plugin updates, then this check again: "
                       "a refreshed marketplace can change the available versions.")
        if any(p.status == UPDATE for p in r.plugins):
            out.append("Restart Claude Code after `claude plugin update` to load the new version.")
        if any(p.changed_files for p in r.plugins):
            out.append("A row marked \"same version, but N files differ\" is `unknown`: the plugin changed in its "
                       "marketplace without a version bump, or someone edited the installed copy. The files alone "
                       "cannot tell which. `claude plugin update` compares versions and can report it as current, "
                       "so its command reinstalls it from the marketplace copy and keeps its data directory. "
                       "Skip it if the installed copy holds local edits you want to keep.")
    else:
        out.append("Nothing to fix.")
    busy = [c for c in r.stale + r.orphans if c.in_use]
    if busy:
        out.append(f"{len(busy)} cache director{'y is' if len(busy) == 1 else 'ies are'} still in use by a running "
                   "session and left out of the commands. Close those sessions and run this check again.")
    unsure = [c for c in r.stale if c.uncertain and not c.in_use]
    if unsure:
        out.append(f"{len(unsure)} cache director{'y is' if len(unsure) == 1 else 'ies are'} left out of the commands "
                   "because the plugin's active version is unknown.")
    if off:
        out.append("To turn on auto-update for a marketplace: `/plugin` > Marketplaces > select it > Enable auto-update.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- html page

# The page is built from render()'s Markdown, so it never says anything the Markdown does not.
# Every piece of text goes through html.escape: an agterm page can run agterm commands, so no
# report text may become markup.

STATUS_CLASS = {CURRENT: "ok", UPDATE: "update", UNKNOWN: "unknown"}
TABLE_RULE = re.compile(r"\|(\s*:?-+:?\s*\|)+")

PAGE_CSS = """
body { background: var(--agterm-background, Canvas); color: var(--agterm-foreground, CanvasText);
       font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; margin: 0; }
.page { --ok: var(--agterm-color-2, green); --update: var(--agterm-color-3, darkorange);
        --unknown: var(--agterm-color-5, purple); --accent: var(--agterm-color-4, blue);
        --line: color-mix(in srgb, var(--agterm-foreground, CanvasText) 15%, transparent);
        --tint: color-mix(in srgb, var(--agterm-foreground, CanvasText) 5%, transparent);
        max-width: 1200px; margin: 0 auto; padding: 16px 28px 40px; }
.summary { font-size: 15px; margin: 0 0 .3em; }
.meta { opacity: .6; font-size: 12px; margin: 0 0 1em; }
h2 { font-size: 1.1em; font-weight: 600; color: var(--accent); margin: 1.4em 0 .5em;
     border-bottom: 1px solid var(--line); padding-bottom: .25em; }
table { border-collapse: collapse; width: 100%; margin: .4em 0 1em; font-size: 13px; }
th, td { border: 1px solid var(--line); padding: 4px 9px; text-align: left; vertical-align: top;
         overflow-wrap: anywhere; }
th { background: var(--tint); font-weight: 600; }
tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--agterm-foreground, CanvasText) 3%, transparent); }
.status { font-weight: 600; white-space: nowrap; }
.status.ok { color: var(--ok); } .status.update { color: var(--update); } .status.unknown { color: var(--unknown); }
code, pre { font-family: ui-monospace, Menlo, monospace; }
code { background: var(--tint); padding: 1px 4px; border-radius: 4px; font-size: 12px; }
pre { background: var(--tint); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;
      overflow-x: auto; font-size: 12px; line-height: 1.45; }
pre code { background: none; padding: 0; }
li { margin: .15em 0; overflow-wrap: anywhere; }
"""


def inline_html(text: str) -> str:
    """Escape text and render the inline Markdown the report uses: `code` spans and \\| escapes."""
    out = []
    for part in re.split(r"(`[^`]*`)", text.replace("\\|", "|")):
        if len(part) >= 2 and part[0] == part[-1] == "`":
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            out.append(html.escape(part))
    return "".join(out)


def split_row(line: str) -> list:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def table_html(lines: list) -> str:
    rows = [split_row(l) for l in lines if not TABLE_RULE.fullmatch(l.strip())]
    head, body = rows[0], rows[1:]
    status_col = head.index("status") if "status" in head else -1
    out = ["<table><thead><tr>" + "".join(f"<th>{inline_html(h)}</th>" for h in head) + "</tr></thead><tbody>"]
    for row in body:
        cells = []
        for i, c in enumerate(row):
            if i == status_col and c in STATUS_CLASS:
                cells.append(f'<td><span class="status {STATUS_CLASS[c]}">{html.escape(c)}</span></td>')
            else:
                cells.append(f"<td>{inline_html(c)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """Convert the Markdown subset render() writes: ## headings, pipe tables, - lists, ``` blocks, paragraphs."""
    lines, out, i = md.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("## "):
            out.append(f"<h2>{inline_html(line[3:])}</h2>")
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
        elif line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out.append(table_html(lines[i:j]))
            i = j
        elif line.startswith("- "):
            j = i
            while j < len(lines) and lines[j].startswith("- "):
                j += 1
            out.append("<ul>" + "".join(f"<li>{inline_html(l[2:])}</li>" for l in lines[i:j]) + "</ul>")
            i = j
        else:
            out.append(f"<p>{inline_html(line)}</p>")
            i += 1
    return "\n".join(out)


def summary_line(r: Report) -> str:
    def n(status):
        return sum(1 for p in r.plugins if p.status == status)
    dirs = len(r.stale) + len(r.orphans)
    return (f"{n(CURRENT)} of {len(r.plugins)} plugins current, {n(UPDATE)} with an update, "
            f"{n(UNKNOWN)} unknown; {len(r.stale)} stale and {len(r.orphans)} orphaned cache "
            f"director{'y' if dirs == 1 else 'ies'}.")


def render_html(r: Report, md: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Plugin freshness</title>'
            f"<style>{PAGE_CSS}</style></head><body><div class=\"page\">"
            f'<p class="summary">{html.escape(summary_line(r))}</p>'
            f'<p class="meta">plugin-freshness · {html.escape(stamp)}</p>\n'
            f"{markdown_to_html(md)}\n</div></body></html>\n")


def write_private(path: str, text: str) -> None:
    """Write a file readable only by this user, replacing it in one step.

    mkstemp creates a new mode-0600 file under an unpredictable name and refuses to follow a link,
    so nothing planted in the directory beforehand is written through or changes the mode."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- agterm page

AGTERM_TIMEOUT = 10


def agterm_unavailable() -> Optional[str]:
    """Why the report cannot be shown as an agterm page, or None when it can."""
    if os.environ.get("AGTERM_ENABLED") != "1":
        return "not running inside agterm"
    if not os.environ.get("AGTERM_SOCKET") or not os.environ.get("AGTERM_SESSION_ID"):
        return "the agterm session variables are not set"
    if not shutil.which("agtermctl"):
        return "agtermctl is not on PATH"
    rc, out, err = run(["agtermctl", "session", "overlay", "open", "--help"], AGTERM_TIMEOUT)
    if rc != 0 or "--html" not in out + err:
        return "this agtermctl cannot show HTML pages"
    return None


def agterm_page_path(session_id: str) -> str:
    # One file per session, so a second run reloads the page it opened before.
    return os.path.join(tempfile.gettempdir(), f"plugin-freshness-{session_id}.html")


def find_session(tree, session_id: str) -> Optional[dict]:
    stack = [tree]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if str(o.get("id", "")).lower() == session_id.lower() and "overlay" in o:
                return o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def same_file(reported: Optional[str], path: str) -> bool:
    if not reported:
        return False
    if reported.startswith("file://"):
        reported = urllib.parse.unquote(urllib.parse.urlparse(reported).path)
    return os.path.realpath(reported) == os.path.realpath(path)


AGTERM_LOAD_WAIT = 5.0  # seconds to wait for the page to report loaded
AGTERM_POLL = 0.25


def agterm_error(out: str, err: str) -> str:
    return (err or out).strip().splitlines()[-1] if (err or out).strip() else "no output"


def read_session(sid: str, args: list) -> tuple:
    """Return (session node or None, reason when None)."""
    rc, out, err = run(["agtermctl", "tree", "--json"] + args, AGTERM_TIMEOUT)
    if rc != 0:
        return None, f"agtermctl tree failed: {agterm_error(out, err)}"
    try:
        node = find_session(json.loads(out), sid)
    except ValueError:
        return None, "agtermctl tree did not print JSON"
    return (node, "") if node is not None else (None, "this session is not in the agterm tree")


def our_page(node: dict, path: str) -> Optional[dict]:
    for h in node.get("htmlOverlays") or []:
        if isinstance(h, dict) and not h.get("pane") and same_file(h.get("file"), path):
            return h
    return None


def show_in_agterm(page: str) -> tuple:
    """Open the page over this agterm session. Return (shown, detail): the file path, or the reason
    it was not shown.

    Leaves the slot alone when it holds a page from another tool, a running program or a HUD, so the
    caller falls back to Markdown. The check and the open are two agtermctl calls, so something that
    takes the slot in between can still be replaced; agtermctl has no "open only if empty".
    Never switches the user to this session. Counts the page as shown only once agterm reports it
    loaded."""
    reason = agterm_unavailable()
    if reason:
        return False, reason
    sid = os.environ["AGTERM_SESSION_ID"]
    # The same window and socket on every call: without --window, agtermctl acts on the frontmost
    # window, which need not hold this session.
    args = (["--window", os.environ["AGTERM_WINDOW_ID"]] if os.environ.get("AGTERM_WINDOW_ID") else []) \
        + ["--socket", os.environ["AGTERM_SOCKET"]]
    node, reason = read_session(sid, args)
    if node is None:
        return False, reason
    path = agterm_page_path(sid)
    ours = our_page(node, path) is not None
    other = any(isinstance(h, dict) and not h.get("pane") for h in node.get("htmlOverlays") or [])
    if not ours and (other or node.get("overlay") or node.get("hud")):
        return False, "the session's overlay slot is in use"
    try:
        write_private(path, page)
    except OSError as e:
        return False, f"cannot write {path}: {e}"
    verb = ["reload"] if ours else ["open", "--html", path]
    rc, out, err = run(["agtermctl", "session", "overlay"] + verb + ["--target", sid] + args, AGTERM_TIMEOUT)
    if rc != 0:
        return False, f"agtermctl failed: {agterm_error(out, err)}"

    # A zero exit means agterm accepted the page, not that it loaded.
    deadline = time.monotonic() + AGTERM_LOAD_WAIT
    while True:
        node, reason = read_session(sid, args)
        if node is None:
            return False, reason
        h = our_page(node, path)
        state = (h or {}).get("state")
        if state == "loaded":
            where = "" if node.get("active") else " (on this session, which is not the one selected in agterm)"
            return True, path + where
        if state == "failed":
            return False, f"the page failed to load: {h.get('error') or 'no error given'}"
        if time.monotonic() >= deadline:
            return False, ("the page did not finish loading" if h else "the page did not appear in the overlay slot")
        time.sleep(AGTERM_POLL)


# ---------------------------------------------------------------- main

def parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="plugin_freshness", description=__doc__.split("\n\n")[0])
    p.add_argument("--plugins-dir", default=os.path.expanduser("~/.claude/plugins"),
                   help="Claude Code plugins directory (default: ~/.claude/plugins)")
    p.add_argument("--plugin-list", metavar="FILE",
                   help="read `claude plugin list --json` output from FILE instead of running it")
    p.add_argument("--marketplace-list", metavar="FILE",
                   help="read `claude plugin marketplace list --json` output from FILE instead of running it")
    p.add_argument("--no-remote", action="store_true",
                   help="skip `git ls-remote`; marketplace freshness becomes unknown")
    p.add_argument("--html", metavar="FILE", help="also write the report as an HTML page to FILE")
    p.add_argument("--show", action="store_true",
                   help="also open the report as a page over this agterm session when agterm can show HTML")
    p.add_argument("--test", action="store_true", help="run the self-tests and exit")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.test:
        return run_self_tests()
    try:
        plugin_list = cli_json(["claude", "plugin", "list", "--json"], args.plugin_list)
        market_list = cli_json(["claude", "plugin", "marketplace", "list", "--json"], args.marketplace_list)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    report = build_report(args.plugins_dir, plugin_list, market_list, not args.no_remote)
    md = render(report)
    sys.stdout.write(md)
    page = render_html(report, md) if (args.html or args.show) else ""
    if args.html:
        try:
            write_private(args.html, page)
        except OSError as e:
            print(f"error: cannot write {args.html}: {e}", file=sys.stderr)
            return 2
    if args.show:
        shown, detail = show_in_agterm(page)
        print(f"\n[agterm overlay] {'shown' if shown else 'not shown'}: {detail}")
    return 1 if report.needs_attention() else 0


# ---------------------------------------------------------------- self-tests

def run_self_tests() -> int:
    import hashlib
    import io
    import shutil
    import tempfile
    import unittest
    from contextlib import redirect_stdout
    from unittest import mock

    genv = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(*a, cwd=None) -> str:
        return subprocess.run(["git", *a], cwd=cwd, env=genv, check=True, capture_output=True, text=True).stdout.strip()

    def write(path, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def tree_state(root) -> dict:
        state = {}
        for d, dirs, files in os.walk(root):
            for n in dirs + files:
                full = os.path.join(d, n)
                st = os.lstat(full)
                digest = ""
                if os.path.isfile(full):
                    with open(full, "rb") as f:
                        digest = hashlib.sha256(f.read()).hexdigest()
                state[full] = (st.st_mtime_ns, digest)
        return state

    class Fixture:
        """A fake ~/.claude/plugins with real git upstreams under file://."""

        def __init__(self, tmp):
            self.tmp = tmp
            self.pdir = os.path.join(tmp, "plugins")
            self.up = os.path.join(tmp, "upstream")
            self.plugins, self.markets, self.known, self.provenance = [], [], {}, {}

        def upstream(self, name, files: dict) -> str:
            repo = os.path.join(self.up, name)
            os.makedirs(repo)
            git("init", "-q", "-b", "main", cwd=repo)
            return self.commit(name, files)

        def commit(self, name, files: dict) -> str:
            repo = os.path.join(self.up, name)
            for rel, data in files.items():
                write(os.path.join(repo, rel), data)
            git("add", "-A", cwd=repo)
            git("commit", "-q", "--allow-empty", "-m", "c", cwd=repo)
            return git("rev-parse", "HEAD", cwd=repo)

        def clone(self, name, auto=None):
            dest = os.path.join(self.pdir, "marketplaces", name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            git("clone", "-q", "--depth", "1", "file://" + os.path.join(self.up, name), dest)
            self.register(name, {"source": "git", "url": "file://" + os.path.join(self.up, name)}, auto)

        def snapshot(self, name, sha, auto=None):
            dest = os.path.join(self.pdir, "marketplaces", name)
            shutil.copytree(os.path.join(self.up, name), dest, ignore=shutil.ignore_patterns(".git"))
            write(os.path.join(dest, ".gcs-sha"), sha + "\n")
            self.register(name, {"source": "git", "url": "file://" + os.path.join(self.up, name)}, auto)

        def register(self, name, source, auto):
            loc = os.path.join(self.pdir, "marketplaces", name)
            self.markets.append({"name": name, "source": source["source"], "installLocation": loc})
            # Refreshed after every fixture install unless a test says otherwise.
            self.known[name] = {"source": source, "installLocation": loc, "lastUpdated": "2026-01-02T00:00:00.000Z"}
            if auto is not None:
                self.known[name]["autoUpdate"] = auto

        def install(self, pid, version, scope="user", project=None, sha=None):
            name, market = pid.split("@")
            path = os.path.join(self.pdir, "cache", market, name, version)
            loc = os.path.join(self.pdir, "marketplaces", market)
            m = Marketplace(market, loc, manifest=read_json(os.path.join(loc, ".claude-plugin", "marketplace.json")))
            entry = find_entry(m, name)
            src = local_plugin_dir(m, entry) if entry else None
            if src and os.path.isdir(src):
                shutil.copytree(src, path, dirs_exist_ok=True)
            os.makedirs(path, exist_ok=True)
            e = {"id": pid, "version": version, "scope": scope, "enabled": True, "installPath": path,
                 "lastUpdated": "2026-01-01T00:00:00.000Z"}
            if project:
                e["projectPath"] = project
            self.plugins.append(e)
            if sha:
                self.provenance.setdefault(pid, []).append({"installPath": path, "version": version, "gitCommitSha": sha})

        def cache_dir(self, market, name, version) -> str:
            path = os.path.join(self.pdir, "cache", market, name, version)
            os.makedirs(path, exist_ok=True)
            return path

        def report(self, check_remote=True) -> Report:
            write(os.path.join(self.pdir, "known_marketplaces.json"), self.known)
            write(os.path.join(self.pdir, "installed_plugins.json"), {"version": 2, "plugins": self.provenance})
            return build_report(self.pdir, self.plugins, self.markets, check_remote)

    def mkt(*plugins) -> dict:
        return {".claude-plugin/marketplace.json": {"name": "x", "plugins": list(plugins)}}

    class Tests(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.mkdtemp(prefix="plugin-freshness-test-")
            self.f = Fixture(self.tmp)

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def base(self):
            """mkt-a holds semver plugin alpha 1.0.0; mkt-b holds path-based plugin beta."""
            self.f.upstream("mkt-a", {**mkt({"name": "alpha", "source": "./plugins/alpha"}),
                                      "plugins/alpha/.claude-plugin/plugin.json": {"name": "alpha", "version": "1.0.0"}})
            self.b1 = self.f.upstream("mkt-b", {**mkt({"name": "beta", "source": "./plugins/beta"}),
                                                "plugins/beta/.claude-plugin/plugin.json": {"name": "beta"}})
            self.b2 = self.f.commit("mkt-b", {"plugins/beta/README.md": "two"})
            self.f.clone("mkt-a")
            self.f.clone("mkt-b")

        def plugin(self, r, pid) -> Plugin:
            return next(p for p in r.plugins if p.id == pid)

        def market(self, r, name) -> Marketplace:
            return next(m for m in r.marketplaces if m.name == name)

        # The five eval scenarios from the spec.

        def test_everything_current(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            r = self.f.report()
            self.assertEqual([m.status for m in r.marketplaces], [CURRENT, CURRENT])
            self.assertEqual([p.status for p in r.plugins], [CURRENT, CURRENT])
            self.assertEqual(self.plugin(r, "beta@mkt-b").regime, "commit")
            self.assertEqual(self.plugin(r, "alpha@mkt-a").regime, "semver")
            self.assertFalse(r.stale or r.orphans)
            self.assertFalse(r.needs_attention())
            self.assertIn("Nothing to fix.", render(r))

        def test_stale_semver_plugin(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            r = self.f.report()
            a = self.plugin(r, "alpha@mkt-a")
            self.assertEqual((a.status, a.installed, a.available), (UPDATE, "0.9.0", "1.0.0"))
            self.assertIn("claude plugin update alpha@mkt-a", fix_commands(r))
            self.assertNotIn("claude plugin update beta@mkt-b", fix_commands(r))

        def test_stale_path_based_plugin(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.install("beta@mkt-b", self.b1[:12])
            r = self.f.report()
            b = self.plugin(r, "beta@mkt-b")
            self.assertEqual((b.status, b.available), (UPDATE, self.b2[:12]))
            self.assertEqual(self.market(r, "mkt-b").status, CURRENT)
            self.assertEqual(fix_commands(r), ["claude plugin update beta@mkt-b"])

        def test_orphaned_cache_entry(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            gone = self.f.cache_dir("mkt-a", "gone", "1.0.0")
            r = self.f.report()
            self.assertEqual([c.path for c in r.orphans], [gone])
            self.assertEqual(r.stale, [])
            self.assertIn(f"rm -rf {shlex.quote(gone)}", fix_commands(r))

        def test_marketplace_behind_remote(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            self.f.commit("mkt-a", {"x": "1"})
            self.f.commit("mkt-b", {"y": "1"})
            r = self.f.report()
            self.assertEqual(self.market(r, "mkt-a").status, UPDATE)
            # Semver: the local versions match, but the newer upstream may change them.
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, UNKNOWN)
            # Path-based: every new marketplace commit is a new version.
            self.assertEqual(self.plugin(r, "beta@mkt-b").status, UPDATE)
            cmds = fix_commands(r)
            self.assertEqual(cmds[:2], ["claude plugin marketplace update mkt-a",
                                        "claude plugin marketplace update mkt-b"])
            self.assertIn("then this check again", render(r))

        # Edge cases.

        def test_same_version_changed_files(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0", sha="0" * 40)
            cached = os.path.join(self.f.pdir, "cache", "mkt-a", "alpha", "1.0.0")
            write(os.path.join(cached, ".in_use", "1"), "")
            write(os.path.join(cached, "scripts", "__pycache__", "x.pyc"), "bytecode")
            r = self.f.report()
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, CURRENT)  # ignored names do not count
            write(os.path.join(cached, "SKILL.md"), "old text")
            r = self.f.report()
            a = self.plugin(r, "alpha@mkt-a")
            # A local edit and an upstream change look the same, so the status is unknown either way.
            self.assertEqual((a.status, a.changed_files), (UNKNOWN, 1))
            self.assertIn("same version, but 1 file differs from the marketplace copy; installed from commit 000000000000", a.note)
            self.assertEqual(fix_commands(r), ["claude plugin uninstall --keep-data alpha@mkt-a && claude plugin install "
                                               "alpha@mkt-a  # replaces the installed files, local edits included"])
            self.assertIn("Skip it if the installed copy holds local edits", render(r))
            self.assertTrue(r.needs_attention())

        def test_unverifiable_files_are_unknown(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.plugins[0]["installPath"] = os.path.join(self.tmp, "nonexistent")
            r = self.f.report()
            a = self.plugin(r, "alpha@mkt-a")
            self.assertEqual(a.status, UNKNOWN)
            self.assertIn("could not be compared", a.note)
            del self.f.plugins[0]["installPath"]
            self.assertEqual(self.plugin(self.f.report(), "alpha@mkt-a").status, UNKNOWN)

        def test_equal_rank_different_string_is_unknown(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0+old")
            a = self.plugin(self.f.report(), "alpha@mkt-a")
            self.assertEqual(a.status, UNKNOWN)
            self.assertIn("rank equal", a.note)

        def test_unreadable_directory_is_unknown(self):
            if os.geteuid() == 0:
                self.skipTest("root reads mode-000 directories")
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            locked = os.path.join(self.f.pdir, "cache", "mkt-a", "alpha", "1.0.0", "locked")
            write(os.path.join(locked, "p.txt"), "x")
            os.chmod(locked, 0)
            try:
                a = self.plugin(self.f.report(), "alpha@mkt-a")
            finally:
                os.chmod(locked, 0o755)
            self.assertEqual(a.status, UNKNOWN)
            self.assertIn("could not be compared", a.note)

        def test_newer_installed_version_is_not_an_update(self):
            self.base()
            self.f.install("alpha@mkt-a", "2.0.0")
            r = self.f.report()
            a = self.plugin(r, "alpha@mkt-a")
            self.assertEqual(a.status, UNKNOWN)
            self.assertIn("installed version is newer", a.note)
            self.assertEqual(fix_commands(r), [])
            self.f.plugins[0]["version"] = "nightly-7"
            self.assertIn("cannot be ordered", self.plugin(self.f.report(), "alpha@mkt-a").note)

        def test_scoped_reinstall_command_parses(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0", scope="project", project="/work/my app")
            write(os.path.join(self.f.pdir, "cache", "mkt-a", "alpha", "1.0.0", "SKILL.md"), "edited")
            (cmd,) = fix_commands(self.f.report())
            self.assertEqual(cmd, "(cd '/work/my app' && claude plugin uninstall --keep-data --scope project alpha@mkt-a"
                                  " && claude plugin install --scope project alpha@mkt-a)"
                                  "  # replaces the installed files, local edits included")
            for c in fix_commands(self.f.report()):
                self.assertEqual(subprocess.run(["sh", "-n", "-c", c]).returncode, 0, c)  # parse only, never run

        def test_path_based_files_are_checked(self):
            self.base()
            self.f.install("beta@mkt-b", self.b2[:12])
            write(os.path.join(self.f.pdir, "cache", "mkt-b", "beta", self.b2[:12], "README.md"), "edited")
            b = self.plugin(self.f.report(), "beta@mkt-b")
            self.assertEqual((b.status, b.changed_files), (UNKNOWN, 1))
            self.f.plugins[0]["installPath"] = os.path.join(self.tmp, "nonexistent")
            b = self.plugin(self.f.report(), "beta@mkt-b")
            self.assertEqual(b.status, UNKNOWN)
            self.assertIn("could not be compared", b.note)

        def test_mixed_timestamp_formats(self):
            self.base()
            self.f.install("beta@mkt-b", self.b1[:12])
            self.f.plugins[0]["lastUpdated"] = "2026-01-01T00:00:00"  # no offset
            self.assertEqual(self.plugin(self.f.report(), "beta@mkt-b").status, UPDATE)
            self.assertEqual(parse_time("2026-01-01T00:00:00"), parse_time("2026-01-01T00:00:00.000Z"))

        def test_path_based_order_needs_proof(self):
            self.base()
            self.f.install("beta@mkt-b", self.b1[:12])
            self.f.known["mkt-b"]["lastUpdated"] = "2025-12-31T00:00:00.000Z"  # refreshed before the install
            r = self.f.report()
            b = self.plugin(r, "beta@mkt-b")
            self.assertEqual(b.status, UNKNOWN)
            self.assertIn("not known to be the later one", b.note)
            self.assertEqual(fix_commands(r), [])

        def test_active_cache_dir_is_never_stale(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.plugins[0]["version"] = ""  # the CLI still names the active directory
            other = self.f.cache_dir("mkt-a", "alpha", "0.9.0")
            r = self.f.report()
            self.assertEqual([c.path for c in r.stale], [other])
            # No version and no installPath: the active directory is unknown, so nothing gets an rm line.
            del self.f.plugins[0]["installPath"]
            r = self.f.report()
            self.assertEqual(len(r.stale), 2)
            self.assertTrue(all(c.uncertain for c in r.stale))
            self.assertFalse(any(c.startswith("rm ") for c in fix_commands(r)))
            self.assertIn("may be the active directory", render(r))

        def test_nothing_to_fix_only_without_commands(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0")
            text = render(self.f.report())
            self.assertIn("```bash", text)
            self.assertNotIn("Nothing to fix.", text)

        def test_compare_versions(self):
            self.assertEqual(compare_versions("1.0.0", "1.0.1"), -1)
            self.assertEqual(compare_versions("1.10.0", "1.9.9"), 1)
            self.assertEqual(compare_versions("1.0", "1.0.0"), 0)
            self.assertEqual(compare_versions("v2.0.0", "2.0.0"), 0)
            self.assertEqual(compare_versions("1.0.0-beta.2", "1.0.0"), -1)
            self.assertEqual(compare_versions("1.0.0-beta.2", "1.0.0-beta.10"), -1)
            self.assertEqual(compare_versions("1.0.0-alpha", "1.0.0-alpha.1"), -1)
            self.assertEqual(compare_versions("1.0.0+build.5", "1.0.0"), 0)
            self.assertIsNone(compare_versions("nightly", "1.0.0"))

        def test_stale_cache_dir_ignores_mtime(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            old = self.f.cache_dir("mkt-a", "alpha", "0.9.0")
            os.utime(old, (4_000_000_000, 4_000_000_000))  # newer than the active directory
            r = self.f.report()
            self.assertEqual([(c.path, c.installed) for c in r.stale], [(old, ["1.0.0"])])
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, CURRENT)

        def test_in_use_cache_dir_gets_no_rm(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            busy = self.f.cache_dir("mkt-a", "alpha", "0.9.0")
            write(os.path.join(busy, ".in_use", str(os.getpid())), "")
            dead = self.f.cache_dir("mkt-a", "alpha", "0.8.0")
            write(os.path.join(dead, ".in_use", "999999999"), "")
            r = self.f.report()
            self.assertEqual({c.path: c.in_use for c in r.stale}, {busy: [os.getpid()], dead: []})
            cmds = fix_commands(r)
            self.assertIn(f"rm -rf {shlex.quote(dead)}", cmds)
            self.assertNotIn(f"rm -rf {shlex.quote(busy)}", cmds)
            self.assertIn(f"in use by running session PID {os.getpid()}", render(r))
            self.assertIn("1 cache directory is still in use", render(r))

        def test_snapshot_marketplace_uses_gcs_sha(self):
            head = self.f.upstream("official", {**mkt({"name": "ctx", "source": "./ext/ctx"}),
                                                "ext/ctx/.claude-plugin/plugin.json": {"name": "ctx"}})
            self.f.snapshot("official", head)
            self.f.install("ctx@official", head[:12])
            r = self.f.report()
            self.assertEqual((self.market(r, "official").kind, self.market(r, "official").status), ("snapshot", CURRENT))
            self.assertEqual(self.plugin(r, "ctx@official").status, CURRENT)
            self.f.commit("official", {"z": "1"})
            r = self.f.report()
            self.assertEqual(self.market(r, "official").status, UPDATE)
            self.assertEqual(self.plugin(r, "ctx@official").status, UPDATE)

        def test_unreachable_remote_is_unknown(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            git("-C", os.path.join(self.f.pdir, "marketplaces", "mkt-a"), "remote", "set-url", "origin",
                "file://" + os.path.join(self.tmp, "nowhere"))
            r = self.f.report()
            m = self.market(r, "mkt-a")
            self.assertEqual(m.status, UNKNOWN)
            self.assertIn("remote not reachable", m.problem)
            self.assertIn("nowhere", m.problem)  # the whole git error, not only its last line
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, UNKNOWN)

        def test_no_remote_flag(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            r = self.f.report(check_remote=False)
            self.assertEqual(self.market(r, "mkt-a").status, UNKNOWN)
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, UNKNOWN)

        def test_built_in_marketplace_is_skipped(self):
            self.base()
            self.f.plugins.append({"id": "tool@anthropic-plugin-directory", "version": "2.0.0"})
            r = self.f.report()
            self.assertEqual(self.plugin(r, "tool@anthropic-plugin-directory").status, UNKNOWN)
            self.assertIn("anthropic-plugin-directory", r.skipped)
            self.assertIn("built in to Claude Code", render(r))

        def test_unknown_version_and_unlisted_plugin(self):
            self.base()
            self.f.plugins.append({"id": "alpha@mkt-a", "version": "unknown"})
            self.f.plugins.append({"id": "removed@mkt-a", "version": "1.0.0"})
            r = self.f.report()
            self.assertEqual(self.plugin(r, "alpha@mkt-a").status, UNKNOWN)
            self.assertIn("no longer listed", self.plugin(r, "removed@mkt-a").note)

        def test_version_from_marketplace_entry_and_plugin_root(self):
            self.f.upstream("mkt-c", {".claude-plugin/marketplace.json": {
                "name": "mkt-c", "metadata": {"pluginRoot": "./plugins"},
                "plugins": [{"name": "lsp", "source": "lsp", "version": "1.0.0"},
                            {"name": "both", "source": "both", "version": "9.9.9"}]},
                "plugins/lsp/README.md": "x",
                "plugins/both/.claude-plugin/plugin.json": {"name": "both", "version": "2.0.0"}})
            self.f.clone("mkt-c")
            self.f.install("lsp@mkt-c", "1.0.0")
            self.f.install("both@mkt-c", "2.0.0")
            r = self.f.report()
            self.assertEqual(self.plugin(r, "lsp@mkt-c").status, CURRENT)
            self.assertEqual(self.plugin(r, "both@mkt-c").available, "2.0.0")  # plugin.json wins

        def test_external_path_based_plugin_is_unknown(self):
            self.f.upstream("mkt-d", mkt({"name": "ext", "source": {"source": "github", "repo": "o/r"}}))
            self.f.clone("mkt-d")
            self.f.install("ext@mkt-d", "abcdef012345")
            r = self.f.report()
            self.assertEqual(self.plugin(r, "ext@mkt-d").status, UNKNOWN)
            self.assertIn("hosted outside", self.plugin(r, "ext@mkt-d").note)

        def test_auto_update(self):
            self.base()
            self.f.known["mkt-a"]["autoUpdate"] = True
            self.f.known["mkt-b"]["autoUpdate"] = False
            r = self.f.report()
            self.assertEqual((self.market(r, "mkt-a").auto_update, self.market(r, "mkt-b").auto_update), (True, False))
            del self.f.known["mkt-b"]["autoUpdate"]
            r = self.f.report()
            self.assertIn("mkt-b (default for a third-party marketplace)", render(r))
            m = Marketplace("claude-plugins-official", "/nonexistent")
            self.assertTrue(load_marketplaces([{"name": m.name, "installLocation": m.location}],
                                              self.f.pdir, False)[0].auto_update)

        def test_project_scope_fix_command(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0", scope="project", project="/work/my app")
            r = self.f.report()
            self.assertIn("(cd '/work/my app' && claude plugin update alpha@mkt-a --scope project)", fix_commands(r))

        def test_detect_regime(self):
            self.assertEqual(detect_regime("d182ca456ca0"), "commit")
            self.assertEqual(detect_regime("d182ca456ca09d31d139f7d3818d1d333b103cce"), "commit")
            self.assertEqual(detect_regime("1.0.0"), "semver")
            self.assertEqual(detect_regime("2024.01"), "semver")
            self.assertEqual(detect_regime("unknown"), "none")
            self.assertEqual(detect_regime(""), "none")

        def test_parse_ls_remote(self):
            out = "a" * 40 + "\tHEAD\n" + "b" * 40 + "\trefs/tags/v1\n" + "c" * 40 + "\trefs/tags/v1^{}\n"
            self.assertEqual(parse_ls_remote(out, "HEAD"), "a" * 40)
            self.assertEqual(parse_ls_remote(out, "v1"), "c" * 40)
            self.assertIsNone(parse_ls_remote(out, "main"))

        def test_read_only(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0")
            self.f.install("beta@mkt-b", self.b1[:12])
            self.f.cache_dir("mkt-a", "gone", "1.0.0")
            self.f.commit("mkt-a", {"x": "1"})
            write(os.path.join(self.f.pdir, "known_marketplaces.json"), self.f.known)
            before = tree_state(self.f.pdir)
            build_report(self.f.pdir, self.f.plugins, self.f.markets, True)
            self.assertEqual(tree_state(self.f.pdir), before)

        def test_cli_end_to_end(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0")
            write(os.path.join(self.tmp, "pl.json"), self.f.plugins)
            write(os.path.join(self.tmp, "ml.json"), self.f.markets)
            write(os.path.join(self.f.pdir, "known_marketplaces.json"), self.f.known)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--plugins-dir", self.f.pdir, "--plugin-list", os.path.join(self.tmp, "pl.json"),
                           "--marketplace-list", os.path.join(self.tmp, "ml.json")])
            text = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("| id | installed | available | marketplace | status | note |", text)
            self.assertIn("| alpha@mkt-a | 0.9.0 | 1.0.0 | mkt-a | update available |", text)
            self.assertIn("## Commands to fix (not run)", text)
            self.assertEqual(main(["--plugin-list", os.path.join(self.tmp, "missing.json")]), 2)

        # HTML page and the agterm overlay.

        def test_html_page_renders_tables_and_status(self):
            self.base()
            self.f.install("alpha@mkt-a", "0.9.0")
            self.f.install("beta@mkt-b", self.b2[:12])
            r = self.f.report()
            page = render_html(r, render(r))
            self.assertEqual(page.count("<table>"), 2)
            self.assertIn("<th>status</th>", page)
            self.assertIn('<span class="status update">update available</span>', page)
            self.assertIn('<span class="status ok">current</span>', page)
            self.assertIn("<pre><code>claude plugin update alpha@mkt-a</code></pre>", page)
            self.assertIn("1 of 2 plugins current, 1 with an update, 0 unknown", page)
            self.assertNotIn("| alpha@mkt-a |", page)  # no table left as raw Markdown

        def test_html_escapes_report_text(self):
            md = ("## a <b>\n\n| col | status |\n|---|---|\n"
                  "| <button data-agterm=\"session.close\">x</button> \\| y | current |\n\n"
                  "- `c<d>` & e\n\n```bash\nrm -rf '<p>'\n```\n")
            out = markdown_to_html(md)
            self.assertNotIn("<button", out)
            self.assertNotIn("<b>", out)
            self.assertIn("&lt;button data-agterm=&quot;session.close&quot;&gt;x&lt;/button&gt; | y", out)
            self.assertIn("<li><code>c&lt;d&gt;</code> &amp; e</li>", out)
            self.assertIn("<pre><code>rm -rf &#x27;&lt;p&gt;&#x27;</code></pre>", out)

        def fake_agterm(self, tree=None, html=True, after=None, tree_fails=False):
            """Put a fake agtermctl first on PATH. It logs each call and answers help and tree.

            `after` is the tree it answers once an overlay open or reload has run, which is how a test
            makes the page load, fail or never appear. `tree_fails` makes tree exit 1."""
            bin_dir = os.path.join(self.tmp, "bin")
            os.makedirs(bin_dir, exist_ok=True)
            log = os.path.join(self.tmp, "agtermctl.log")
            tree_file, after_file = os.path.join(self.tmp, "tree.json"), os.path.join(self.tmp, "tree-after.json")
            write(tree_file, tree if tree is not None else {})
            if after is not None:
                write(after_file, after)
            elif os.path.exists(after_file):
                os.remove(after_file)
            help_text = "  --html <html>  Show this local HTML file" if html else "usage"
            tree_cmd = 'echo "Operation not permitted" >&2; exit 1' if tree_fails else f'cat "{tree_file}"'
            script = os.path.join(bin_dir, "agtermctl")
            write(script, "#!/bin/sh\n"
                          f'echo "$*" >> "{log}"\n'
                          'case "$*" in\n'
                          f'  *--help*) echo "{help_text}";;\n'
                          f'  tree*) {tree_cmd};;\n'
                          f'  "session overlay open"*|"session overlay reload"*)\n'
                          f'    [ -f "{after_file}" ] && cp "{after_file}" "{tree_file}"; echo ok;;\n'
                          '  *) echo ok;;\n'
                          'esac\n')
            os.chmod(script, 0o755)
            env = {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""), "AGTERM_ENABLED": "1",
                   "AGTERM_SOCKET": "/tmp/fake.sock", "AGTERM_SESSION_ID": "SID-1", "AGTERM_WINDOW_ID": "WIN-1"}
            return env, log

        def calls(self, log):
            return open(log).read().splitlines() if os.path.exists(log) else []

        def show(self, env, wait=0.6):
            saved = tempfile.tempdir
            tempfile.tempdir = self.tmp
            try:
                with mock.patch.dict(os.environ, env), mock.patch.dict(globals(), {"AGTERM_LOAD_WAIT": wait}):
                    return show_in_agterm("<p>page</p>")
            finally:
                tempfile.tempdir = saved

        def page_path(self):
            return os.path.join(self.tmp, "plugin-freshness-SID-1.html")

        def node(self, **fields):
            return {"workspaces": [{"id": "W", "sessions": [dict({"id": "sid-1", "overlay": False, "active": True},
                                                                **fields)]}]}

        def loaded(self, state="loaded", **fields):
            return self.node(overlay=True, htmlOverlays=[dict({"file": self.page_path(), "state": state}, **fields)])

        def test_show_outside_agterm(self):
            with mock.patch.dict(os.environ, {"AGTERM_ENABLED": ""}):
                self.assertEqual(show_in_agterm("<p/>"), (False, "not running inside agterm"))
            env, log = self.fake_agterm(html=False)
            self.assertEqual(self.show(env), (False, "this agtermctl cannot show HTML pages"))
            env["PATH"] = self.tmp  # no agtermctl here
            self.assertEqual(self.show(env), (False, "agtermctl is not on PATH"))

        def test_show_opens_page_on_free_slot(self):
            env, log = self.fake_agterm(self.node(), after=self.loaded())
            shown, path = self.show(env)
            self.assertEqual((shown, path), (True, self.page_path()))
            self.assertEqual(open(path).read(), "<p>page</p>")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertIn("tree --json --window WIN-1 --socket /tmp/fake.sock", self.calls(log))
            # The action names the same window as the tree read, not agterm's frontmost one.
            self.assertIn(f"session overlay open --html {path} --target SID-1 --window WIN-1 --socket /tmp/fake.sock",
                          self.calls(log))
            self.assertFalse(any("--follow" in c for c in self.calls(log)))  # never pulls the user over

        def test_show_reloads_its_own_page(self):
            env, log = self.fake_agterm(self.loaded(), after=self.loaded())
            self.assertEqual(self.show(env), (True, self.page_path()))
            self.assertIn("session overlay reload --target SID-1 --window WIN-1 --socket /tmp/fake.sock", self.calls(log))
            self.assertFalse(any(" open --html" in c for c in self.calls(log)))

        def test_show_counts_only_a_loaded_page(self):
            env, log = self.fake_agterm(self.node(), after=self.loaded("failed", error="boom"))
            self.assertEqual(self.show(env), (False, "the page failed to load: boom"))
            env, log = self.fake_agterm(self.node(), after=self.loaded("loading"))
            self.assertEqual(self.show(env, wait=0.3), (False, "the page did not finish loading"))
            env, log = self.fake_agterm(self.node(), after=self.node())
            self.assertEqual(self.show(env, wait=0.3), (False, "the page did not appear in the overlay slot"))
            after = self.loaded()
            after["workspaces"][0]["sessions"][0]["active"] = False
            env, log = self.fake_agterm(self.node(), after=after)
            shown, detail = self.show(env)
            self.assertTrue(shown)
            self.assertIn("not the one selected in agterm", detail)

        def test_show_leaves_a_busy_slot_alone(self):
            for fields in ({"overlay": True},
                           {"overlay": True, "htmlOverlays": [{"file": "/elsewhere/other.html"}]},
                           {"hud": {"message": "working"}}):
                env, log = self.fake_agterm(self.node(**fields))
                open(log, "w").close()
                self.assertEqual(self.show(env), (False, "the session's overlay slot is in use"))
                self.assertFalse(any(c.startswith("session overlay") and "--help" not in c for c in self.calls(log)))
            env, log = self.fake_agterm({"workspaces": []})
            self.assertEqual(self.show(env), (False, "this session is not in the agterm tree"))
            env, log = self.fake_agterm(tree_fails=True)
            self.assertEqual(self.show(env), (False, "agtermctl tree failed: Operation not permitted"))

        def test_write_private_ignores_planted_files(self):
            target = os.path.join(self.tmp, "victim.txt")
            write(target, "keep")
            path = os.path.join(self.tmp, "page.html")
            # The old fixed temp name, planted as a link to another file.
            os.symlink(target, f"{path}.{os.getpid()}.tmp")
            write(path, "old")
            os.chmod(path, 0o644)
            write_private(path, "new")
            self.assertEqual(open(target).read(), "keep")
            self.assertEqual(open(path).read(), "new")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            # A link at the destination is replaced, never written through.
            os.remove(path)
            os.symlink(target, path)
            write_private(path, "newer")
            self.assertFalse(os.path.islink(path))
            self.assertEqual(open(target).read(), "keep")
            leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".page.html.")]
            self.assertEqual(leftovers, [])

        def test_cli_html_and_show_flags(self):
            self.base()
            self.f.install("alpha@mkt-a", "1.0.0")
            write(os.path.join(self.tmp, "pl.json"), self.f.plugins)
            write(os.path.join(self.tmp, "ml.json"), self.f.markets)
            write(os.path.join(self.f.pdir, "known_marketplaces.json"), self.f.known)
            page = os.path.join(self.tmp, "report.html")
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch.dict(os.environ, {"AGTERM_ENABLED": ""}):
                main(["--plugins-dir", self.f.pdir, "--plugin-list", os.path.join(self.tmp, "pl.json"),
                      "--marketplace-list", os.path.join(self.tmp, "ml.json"), "--html", page, "--show"])
            self.assertIn("<table>", open(page).read())
            self.assertTrue(buf.getvalue().rstrip().endswith("[agterm overlay] not shown: not running inside agterm"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["--plugins-dir", self.f.pdir, "--plugin-list", os.path.join(self.tmp, "pl.json"),
                      "--marketplace-list", os.path.join(self.tmp, "ml.json")])
            self.assertNotIn("[agterm overlay]", buf.getvalue())  # without --show the output is unchanged

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
