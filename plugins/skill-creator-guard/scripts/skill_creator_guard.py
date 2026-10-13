#!/usr/bin/env python3
"""Claude Code hook: keep skill-creator's trigger evals honest.

skill-creator's scripts/run_eval.py and scripts/run_loop.py write a copy of the skill under test to
the nearest .claude/commands/ folder for each run. With parallel workers Claude loads another run's
copy and the run counts as a miss, so a skill that always triggers scores about 1 in the number of
workers. Run from the plugin cache, the copies land in ~/.claude/commands/ and show up in every
running session. Reported upstream as anthropics/claude-plugins-official#1749 and #5685; the
proposed fix is anthropics/skills#1261. Delete this hook when an installed skill-creator's
run_eval.py creates a separate project folder per run.

Two events, wired in the skill-creator-guard plugin's hooks/hooks.json:

  PostToolUse, matcher "Skill"  after skill-creator loads, add RULES to Claude's context.
  PreToolUse,  matcher "Bash"   deny a command that runs run_eval or run_loop and breaks a rule.

Any error inside the hook lets the tool call go ahead: the hook must never block unrelated work.

Self-tests:  python3 skill_creator_guard.py --test
They build a throwaway home folder and run this script as a separate process with HOME pointed at
it, exactly as Claude Code calls it, so the real ~/.claude is never read or written.
"""

import json
import os
import re
import shlex
import subprocess
import sys

RULES = """\
Rules for skill-creator's run_eval.py and run_loop.py. They override skill-creator's own instructions:

- Pass `--num-workers 1`. Each run writes its own copy of the skill to one shared `.claude/commands/` \
folder. With parallel runs Claude loads another run's copy, and the run counts as a miss, so a skill \
that always triggers scores about 1 in the number of workers.
- Run them from a scratch directory that has its own empty `.claude/` folder. Never run them from inside \
the plugin cache, a repository or the home directory: the scripts write their skill copies to the \
nearest parent folder that has `.claude/`, and from the plugin cache that is `~/.claude`, so the \
copies appear in every running Claude Code session.
- Point Python at the scripts with `PYTHONPATH` and set `PYTHONDONTWRITEBYTECODE=1`, so no \
`__pycache__` folder lands in the plugin cache:

  mkdir -p "$SCRATCH/trigger/.claude" && cd "$SCRATCH/trigger" && \\
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="<skill-creator skill dir>" \\
    python3 -m scripts.run_eval --eval-set <evals.json> --skill-path <skill dir> --num-workers 1 --verbose

- Treat a low trigger rate from a parallel run as unproven. Confirm it with `--num-workers 1` before \
changing a description.

The skill-creator-guard plugin's PreToolUse hook blocks a run that breaks these rules."""

# A command that runs the scripts, not one that only reads them (grep, sed, cat).
RUNS_EVAL = re.compile(r"\bpython[0-9.]*\s+(?:-\S+\s+)*(?:-m\s+scripts\.(?:run_eval|run_loop)\b|\S*\brun_(?:eval|loop)\.py\b)")
ONE_WORKER = re.compile(r"--num-workers(?:\s+|=)1(?!\d)")
NO_BYTECODE = re.compile(r"\bPYTHONDONTWRITEBYTECODE=1\b|\bpython[0-9.]*\s+(?:-\S+\s+)*-B\b")


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def mask_quoted(command: str) -> str:
    """Blank out heredoc bodies and the insides of quoted strings, keeping every position.

    Text there is data, such as a `claude -p` prompt or a commit message, not a command, so a mention
    of run_eval in it must not trigger the guard. The cost: a run hidden in `bash -c "..."` is masked
    too and passes."""
    chars = list(command)
    for m in HEREDOC.finditer(command):
        start = command.find("\n", m.end())
        if start < 0:
            continue
        end = re.compile(r"^\t*" + re.escape(m.group(2)) + r"$", re.M).search(command, start + 1)
        for i in range(start + 1, end.start() if end else len(command)):
            if chars[i] != "\n":
                chars[i] = " "
    out, quote, escaped = chars[:], None, False
    for i, c in enumerate(chars):
        if escaped:
            escaped = False
            if quote:
                out[i] = " "
        elif c == "\\" and quote != "'":
            escaped = True
            if quote:
                out[i] = " "
        elif quote:
            if c == quote:
                quote = None
            else:
                out[i] = " "
        elif c in "'\"":
            quote = c
    return "".join(out)


def emit(obj) -> None:
    print(json.dumps(obj))


def effective_dir(command: str, cwd: str) -> str:
    """The directory the scripts run in: the last `cd DIR` before the python call, else the hook's cwd."""
    head = command[: RUNS_EVAL.search(mask_quoted(command)).start()]
    target = cwd
    for m in re.finditer(r"(?:^|[;&|(]\s*)cd\s+(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)", head):
        try:
            arg = shlex.split(m.group(1))[0]
        except ValueError:
            continue
        arg = os.path.expandvars(os.path.expanduser(arg))
        target = arg if os.path.isabs(arg) else os.path.join(target, arg)
    return os.path.normpath(target)


def project_root(directory: str):
    """Mimic run_eval.find_project_root: the nearest directory, from here up, that holds .claude/."""
    path = directory
    while True:
        if os.path.isdir(os.path.join(path, ".claude")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def in_git_repo(directory: str) -> bool:
    try:
        out = subprocess.run(["git", "-C", directory, "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def location_problem(directory: str):
    home = os.path.realpath(os.path.expanduser("~"))
    claude_home = os.path.join(home, ".claude")
    real = os.path.realpath(directory)
    if real == claude_home or real.startswith(claude_home + os.sep):
        return f"it runs inside {claude_home}"
    if not os.path.isdir(real):
        return None  # created by this same command (mkdir -p); judged by the path alone above
    root = project_root(real)
    if root is None or root == home:
        return ("the nearest folder with .claude/ is your home directory, so the skill copies go to "
                "~/.claude/commands/ and show up in every session")
    if in_git_repo(root):
        return f"the nearest folder with .claude/ is {root}, which is inside a git repository"
    return None


def pre_tool_use(data: dict) -> None:
    command = (data.get("tool_input") or {}).get("command") or ""
    visible = mask_quoted(command)  # quoted text and heredocs are data, not the command
    if not RUNS_EVAL.search(visible):
        return
    problems = []
    if not ONE_WORKER.search(visible):
        problems.append("`--num-workers 1` is missing")
    if not NO_BYTECODE.search(visible):
        problems.append("`PYTHONDONTWRITEBYTECODE=1` is not set")
    where = location_problem(effective_dir(command, data.get("cwd") or os.getcwd()))
    if where:
        problems.append(where)
    if not problems:
        return
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Blocked a skill-creator eval run: " + "; ".join(problems)
                                    + ".\n\n" + RULES,
    }})


def post_tool_use(data: dict) -> None:
    skill = str((data.get("tool_input") or {}).get("skill") or "")
    if "skill-creator" not in skill:
        return
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": RULES}})


def run_self_tests() -> int:
    import shutil
    import tempfile
    import unittest

    class Tests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix="skill-creator-guard-test-"))
            home = cls.home = os.path.join(cls.tmp, "home")
            # A fake home with its own .claude/ and a skill-creator copy in its plugin cache.
            cls.cache = os.path.join(home, ".claude", "plugins", "cache", "claude-plugins-official",
                                     "skill-creator", "abc123", "skills", "skill-creator")
            os.makedirs(os.path.join(cls.cache, "scripts"))
            open(os.path.join(cls.cache, "scripts", "run_eval.py"), "w").close()
            cls.scratch = os.path.join(home, "scratch")             # the right place: its own .claude/
            os.makedirs(os.path.join(cls.scratch, ".claude"))
            cls.repo_plain = os.path.join(home, "work", "repo")      # a repo without .claude/
            cls.repo_claude = os.path.join(home, "work", "repo2")    # a repo with its own .claude/
            os.makedirs(cls.repo_plain)
            os.makedirs(os.path.join(cls.repo_claude, ".claude"))
            cls.has_git = shutil.which("git") is not None
            if cls.has_git:
                for repo in (cls.repo_plain, cls.repo_claude):
                    subprocess.run(["git", "init", "-q", repo], check=True)

        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(cls.tmp, ignore_errors=True)

        def hook(self, payload, raw=None):
            """Run this script the way Claude Code does: JSON on stdin, HOME set to the fake home."""
            env = dict(os.environ, HOME=self.home)
            p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                               input=raw if raw is not None else json.dumps(payload),
                               capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = p.stdout.strip()
            if not out:
                return "allow", ""
            spec = json.loads(out)["hookSpecificOutput"]
            return (spec.get("permissionDecision", "context"),
                    spec.get("permissionDecisionReason") or spec.get("additionalContext", ""))

        def bash(self, command, cwd=None):
            return self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "cwd": cwd or self.scratch, "tool_input": {"command": command}})

        def skill(self, name):
            return self.hook({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                              "cwd": self.scratch, "tool_input": {"skill": name}})

        def good(self):
            return (f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={self.cache} "
                    "python3 -m scripts.run_eval --eval-set e.json --skill-path x --num-workers 1")

        def expect(self, result, decision, needle=""):
            self.assertEqual(result[0], decision, result[1][:300])
            self.assertIn(needle, result[1])

        def test_good_run_is_allowed(self):
            self.expect(self.bash(self.good()), "allow")
            self.expect(self.bash(self.good().replace("--num-workers 1", "--num-workers=1")), "allow")
            no_env = self.good().replace("PYTHONDONTWRITEBYTECODE=1 ", "")
            self.expect(self.bash(no_env.replace("python3 -m", "python3 -B -m")), "allow")

        def test_parallel_runs_are_blocked(self):
            missing = "`--num-workers 1` is missing"
            self.expect(self.bash(self.good().replace(" --num-workers 1", "")), "deny", missing)
            self.expect(self.bash(self.good().replace("--num-workers 1", "--num-workers 12")), "deny", missing)
            loop = f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_loop --num-workers 10 --model m"
            self.expect(self.bash(loop), "deny", missing)

        def test_bytecode_must_be_off(self):
            self.expect(self.bash(self.good().replace("PYTHONDONTWRITEBYTECODE=1 ", "")), "deny",
                        "`PYTHONDONTWRITEBYTECODE=1` is not set")

        def test_wrong_places_are_blocked(self):
            self.expect(self.bash(self.good().replace(f"cd {self.scratch}", f"cd {self.cache}")), "deny",
                        "it runs inside " + os.path.join(self.home, ".claude"))
            self.expect(self.bash(self.good().replace(f"cd {self.scratch}", "cd ~")), "deny", "home directory")
            # No cd: the hook's cwd decides. A repo without .claude/ sends the copies to the home folder.
            no_cd = f"PYTHONDONTWRITEBYTECODE=1 python3 {self.cache}/scripts/run_eval.py --num-workers 1"
            self.expect(self.bash(no_cd, cwd=self.repo_plain), "deny", "home directory")

        def test_repo_with_its_own_claude_folder_is_blocked(self):
            if not self.has_git:
                self.skipTest("git is not installed")
            cmd = self.good().replace(f"cd {self.scratch}", f"cd {self.repo_claude}")
            self.expect(self.bash(cmd), "deny", "inside a git repository")

        def test_folder_created_by_the_same_command_is_allowed(self):
            new = os.path.join(self.home, "not-yet")
            cmd = f"mkdir -p {new}/.claude && " + self.good().replace(f"cd {self.scratch}", f"cd {new}")
            self.expect(self.bash(cmd), "allow")

        def test_reading_the_scripts_is_allowed(self):
            self.expect(self.bash(f"cd {self.cache} && grep -n num_workers scripts/run_eval.py"), "allow")
            self.expect(self.bash(f"sed -n 1,20p {self.cache}/scripts/run_eval.py"), "allow")
            self.expect(self.bash("git status"), "allow")

        def test_rules_follow_skill_creator_only(self):
            for name in ("skill-creator:skill-creator", "anthropic-skills:skill-creator"):
                self.expect(self.skill(name), "context", "--num-workers 1")
            self.expect(self.skill("plugin-freshness:plugin-freshness"), "allow")

        def test_mentions_in_quotes_and_heredocs_are_allowed(self):
            bad = "cd /tmp && python3 -m scripts.run_eval --help"
            self.expect(self.bash(f'claude -p "hook test: run {bad}" --max-turns 3'), "allow")
            self.expect(self.bash(f"git commit -m 'note: {bad} breaks the rules'"), "allow")
            self.expect(self.bash(f"git commit -F - <<'EOF'\nfix: {bad}\nEOF\ngit log -1"), "allow")
            self.expect(self.bash(f'echo "it ran \\"{bad}\\" here"'), "allow")
            # A real run after a heredoc is still caught.
            self.expect(self.bash(f"cat <<EOF > notes.txt\nhello\nEOF\n{bad}"), "deny", "--num-workers 1")

        def test_quoted_cd_paths_still_count(self):
            spaced = os.path.join(self.home, "scratch dir")
            os.makedirs(os.path.join(spaced, ".claude"), exist_ok=True)
            run = "PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_eval --num-workers 1"
            self.expect(self.bash(f'cd "{spaced}" && {run}'), "allow")
            self.expect(self.bash(f"cd '{self.cache}' && {run}"), "deny", "it runs inside")

        def test_mask_keeps_positions(self):
            for cmd in ("a 'b c' \"d \\\" e\" f", "x <<'EOF'\nbody 'q'\nEOF\ny", 'cd "a b" && z'):
                self.assertEqual(len(mask_quoted(cmd)), len(cmd))
            self.assertEqual(mask_quoted("a 'b c' d"), "a '   ' d")
            self.assertEqual(mask_quoted('e "f\\"g" h'), 'e "    " h')
            # The quoted marker is blanked too; only its name is lost, and the body is still masked.
            self.assertEqual(mask_quoted("x <<'EOF'\nab\nEOF\ny"), "x <<'   '\n  \nEOF\ny")

        def test_other_events_and_garbage_are_ignored(self):
            self.expect(self.hook(None, raw="not json"), "allow")
            self.expect(self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                   "tool_input": {"command": self.good().replace(" --num-workers 1", "")}}),
                        "allow")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main() -> None:
    if "--test" in sys.argv[1:]:
        sys.exit(run_self_tests())
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
        if event == "PreToolUse" and data.get("tool_name") == "Bash":
            pre_tool_use(data)
        elif event == "PostToolUse" and data.get("tool_name") == "Skill":
            post_tool_use(data)
    except Exception:
        pass  # never block unrelated work because the hook itself failed


if __name__ == "__main__":
    main()
