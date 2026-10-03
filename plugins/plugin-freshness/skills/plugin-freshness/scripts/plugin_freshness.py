#!/usr/bin/env python3
"""Report whether Claude Code marketplaces and installed plugins are current. Read-only.

Three layers are compared:

  installed  what `claude plugin list --json` says is running
  available  the local marketplace copy under ~/.claude/plugins/marketplaces/<name>/
  upstream   the marketplace's remote, read with `git ls-remote` (never `git fetch`)

The report has a marketplace table, a plugin table, the stale and orphaned cache
directories, the marketplaces with auto-update off, and the commands that fix what
was found. The script never runs those commands and never writes to disk.

Exit codes: 0 everything is current, 1 something needs attention, 2 the check failed.

Self-tests:  plugin_freshness.py --test
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
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
FULL_SHA = re.compile(r"[0-9a-f]{40}")

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
    regime: str = ""
    available: Optional[str] = None
    status: str = UNKNOWN
    note: str = ""


@dataclass
class CacheEntry:
    path: str
    plugin_id: str
    version: str
    installed: list = field(default_factory=list)
    in_use: list = field(default_factory=list)  # live PIDs from <dir>/.in_use/


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
        return None, f"remote not reachable: {err.strip().splitlines()[-1] if err.strip() else 'no output'}"
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
        elif p.available != p.installed:
            p.status, p.note = UPDATE, "marketplace copy has a different version"
        elif m.status == CURRENT:
            p.status = CURRENT
        else:
            p.note = f"same version locally; {remote_clause(m)}"
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
        p.status, p.note = UPDATE, "marketplace copy is at a newer commit"
    elif m.status == CURRENT:
        p.status = CURRENT
    elif m.status == UPDATE:
        p.status, p.note = UPDATE, "marketplace copy is behind its remote"
    else:
        p.note = remote_clause(m)


def load_plugins(listing: list, markets: dict) -> list:
    result = []
    for e in listing:
        pid = e.get("id", "")
        if "@" not in pid:
            continue
        name, market = pid.rsplit("@", 1)
        p = Plugin(id=pid, name=name, marketplace=market, installed=str(e.get("version") or ""),
                   scope=e.get("scope", "user"), project_path=e.get("projectPath"),
                   enabled=bool(e.get("enabled", True)))
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
    installed: dict = {}
    for p in plugins:
        installed.setdefault((p.marketplace, p.name), set()).add(p.installed)
    root = os.path.join(plugins_dir, "cache")
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
                    stale.append(entry)
    return stale, orphans


# ---------------------------------------------------------------- output

def build_report(plugins_dir: str, plugin_list: list, market_list: list, check_remote: bool) -> Report:
    markets = load_marketplaces(market_list, plugins_dir, check_remote)
    by_name = {m.name: m for m in markets}
    plugins = load_plugins(plugin_list, by_name)
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
        if p.status != UPDATE:
            continue
        if p.scope in ("project", "local") and p.project_path:
            cmds.append(f"(cd {shlex.quote(p.project_path)} && claude plugin update {shlex.quote(p.id)} --scope {p.scope})")
        else:
            cmds.append(f"claude plugin update {shlex.quote(p.id)}")
    for c in r.stale + r.orphans:
        if not c.in_use:
            cmds.append(f"rm -rf {shlex.quote(c.path)}")
    return cmds


def cell(s) -> str:
    return str(s).replace("|", "\\|")


def in_use_note(c: CacheEntry) -> str:
    if not c.in_use:
        return ""
    return f"; in use by running session PID {', '.join(map(str, c.in_use))}"


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
    out += [f"- `{c.path}` (installed: {', '.join(c.installed)}){in_use_note(c)}" for c in r.stale] or ["None."]
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
    busy = [c for c in r.stale + r.orphans if c.in_use]
    if busy:
        out.append(f"{len(busy)} cache director{'y is' if len(busy) == 1 else 'ies are'} still in use by a running "
                   "session and left out of the commands. Close those sessions and run this check again.")
    else:
        out.append("Nothing to fix.")
    if off:
        out.append("To turn on auto-update for a marketplace: `/plugin` > Marketplaces > select it > Enable auto-update.")
    return "\n".join(out) + "\n"


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
    sys.stdout.write(render(report))
    return 1 if report.needs_attention() else 0


# ---------------------------------------------------------------- self-tests

def run_self_tests() -> int:
    import hashlib
    import io
    import shutil
    import tempfile
    import unittest
    from contextlib import redirect_stdout

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
            self.plugins, self.markets, self.known = [], [], {}

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
            self.known[name] = {"source": source, "installLocation": loc}
            if auto is not None:
                self.known[name]["autoUpdate"] = auto

        def install(self, pid, version, scope="user", project=None):
            name, market = pid.split("@")
            path = os.path.join(self.pdir, "cache", market, name, version)
            os.makedirs(path, exist_ok=True)
            e = {"id": pid, "version": version, "scope": scope, "enabled": True, "installPath": path}
            if project:
                e["projectPath"] = project
            self.plugins.append(e)

        def cache_dir(self, market, name, version) -> str:
            path = os.path.join(self.pdir, "cache", market, name, version)
            os.makedirs(path, exist_ok=True)
            return path

        def report(self, check_remote=True) -> Report:
            write(os.path.join(self.pdir, "known_marketplaces.json"), self.known)
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

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
