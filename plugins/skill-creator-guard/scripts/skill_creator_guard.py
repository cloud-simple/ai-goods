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

The Bash check parses the command the way a shell splits it: quotes, escapes, comments, heredocs,
separators, ( ) subshells, redirects (with their file-descriptor numbers), and $( ) and backtick
substitutions, each analyzed where it runs. It walks the simple commands in order, tracking the
working directory (cd, uv run --directory), shell and exported variables (assignment, export, unset,
env -u / -i) and folders made by mkdir. A command counts as an eval run only when it starts
skill-creator's run_eval or run_loop itself: text in an echo, a prompt, a commit message or a comment
does not, and neither does another project's own scripts/run_eval.py. Workers, bytecode and folder
are checked against that command alone; a run whose folder cannot be worked out is blocked.

This guards against mistakes, not deliberate evasion: eval, source, python -c and scripts written to
a file are not followed. Any error inside the hook lets the tool call go ahead: the hook must never
block unrelated work because of its own failure.

Self-tests:  python3 skill_creator_guard.py --test
They build a throwaway home folder and run this script as a separate process with HOME pointed at
it, exactly as Claude Code calls it, so the real ~/.claude is never read or written.
"""

import json
import os
import re
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
copies appear in every running Claude Code session. Write the folder as a literal path in the same \
command, so the hook can check it.
- Point Python at the scripts with `PYTHONPATH` and set `PYTHONDONTWRITEBYTECODE=1`, so no \
`__pycache__` folder lands in the plugin cache:

  mkdir -p <scratch dir>/.claude && cd <scratch dir> && \\
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<skill-creator skill dir> \\
    python3 -m scripts.run_eval --eval-set <evals.json> --skill-path <skill dir> --num-workers 1 --verbose

- Treat a low trigger rate from a parallel run as unproven. Confirm it with `--num-workers 1` before \
changing a description.

The skill-creator-guard plugin's PreToolUse hook blocks a run that breaks these rules."""

EVAL_SCRIPTS = ("run_eval", "run_loop")
PYTHON = re.compile(r"python[0-9.]*")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WRAPPERS = {"nohup", "time", "command", "exec", "builtin"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "\n", ";;"}
REDIRECTS = {">", ">>", "<", "<>", ">|", "&>", "&>>", ">&", "<&"}
# Operators an IO number can precede, as in 2>log or 3<&0.
FD_OPERATORS = {">", ">>", "<", "<>", ">|", ">&", "<&", "<<", "<<-", "<<<"}
# Longest first, so "<<<" wins over "<<" and "&&" over "&".
OPERATORS = ["<<<", "&>>", "<<-", "&&", "||", ";;", "|&", ">>", "<<", ">&", "<&", "&>", "<>", ">|",
             ";", "&", "|", "(", ")", "<", ">"]
UV_VALUE_OPTIONS = {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--directory",
                    "--project", "--env-file", "--extra", "--group", "--package", "--index", "--index-url",
                    "--from"}
SKILL_NAME = re.compile(r"^name:\s*['\"]?skill-creator['\"]?\s*$", re.M)
MAX_DEPTH = 6
UNKNOWN_FOLDER = ("its folder cannot be worked out from the command; write the scratch folder as a "
                  "literal path, or set the variable in the same command")


def emit(obj) -> None:
    print(json.dumps(obj))


# ---------------------------------------------------------------- shell parsing

def read_paren(text: str, i: int) -> tuple:
    """Read the body of $( ... ) starting after "$(". Return (body, index after the closing paren)."""
    depth, j, n = 1, i, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            k = text.find(c, j + 1)
            j = n if k < 0 else k + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[i:j], j + 1
        j += 1
    return text[i:], n


def substitutions(text: str) -> list:
    """The $( ) and backtick bodies in text the shell expands without word splitting, such as an
    unquoted heredoc body."""
    found, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text.startswith("$(", i):
            body, i = read_paren(text, i + 2)
            found.append(body)
        elif text[i] == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            found.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return found


def tokenize(text: str) -> list:
    """Split shell text into tokens (kind, value, subs):

      ("w", word, [substitution bodies])  a word with quotes removed
      ("op", operator, [])                a separator, paren or redirect
      ("heredoc", body, [subs])           the body of the heredoc started on the line before; an
                                          unquoted delimiter means its $( ) and backticks run
      ("herestring", word, [subs])        the word after <<<
    """
    tokens = []
    word, subs, in_word, quoted = [], [], False, False
    pending = []           # heredoc delimiters waiting for the next newline: (word, strip tabs)
    delim_next = None      # set when the next word is a heredoc delimiter
    herestring_next = False
    i, n = 0, len(text)

    def end_word():
        nonlocal word, subs, in_word, quoted, delim_next, herestring_next
        if in_word:
            w = "".join(word)
            if delim_next is not None:
                pending.append((w, delim_next, quoted))
                delim_next = None
            elif herestring_next:
                tokens.append(("herestring", w, subs))
                herestring_next = False
            else:
                tokens.append(("w", w, subs))
        word, subs, in_word, quoted = [], [], False, False

    def substitution(i):
        body, j = read_paren(text, i + 2)
        subs.append(body)
        word.append("$(" + body + ")")
        return j

    def backtick(i):
        j = text.find("`", i + 1)
        j = n if j < 0 else j
        subs.append(text[i + 1:j])
        word.append(text[i:j + 1])
        return j + 1

    while i < n:
        c = text[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_word()
            tokens.append(("op", "\n", []))
            i += 1
            for delim, strip, delim_quoted in pending:
                body = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j]
                    i = min(j + 1, n)
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    body.append(line)
                text_body = "\n".join(body)
                tokens.append(("heredoc", text_body, [] if delim_quoted else substitutions(text_body)))
            pending = []
        elif c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            if text.startswith("\n", i + 1):
                i += 2
            else:
                word.append(text[i + 1:i + 2])
                in_word = quoted = True
                i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            word.append(text[i + 1:j])
            in_word = quoted = True
            i = j + 1
        elif c == '"':
            in_word = quoted = True
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`\n':
                    if text[i + 1] != "\n":
                        word.append(text[i + 1])
                    i += 2
                elif text.startswith("$(", i):
                    i = substitution(i)
                elif text[i] == "`":
                    i = backtick(i)
                else:
                    word.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$(", i):
            in_word = True
            i = substitution(i)
        elif c == "`":
            in_word = True
            i = backtick(i)
        else:
            op = next((o for o in OPERATORS if text.startswith(o, i)), None)
            if op is None:
                word.append(c)
                in_word = True
                i += 1
                continue
            if op in FD_OPERATORS and in_word and not quoted and "".join(word).isdigit():
                word, subs, in_word = [], [], False  # an IO number such as the 2 in 2>log, not a word
            end_word()
            if op in ("<<", "<<-"):
                delim_next = op == "<<-"
            elif op == "<<<":
                herestring_next = True
            else:
                tokens.append(("op", op, []))
            i += len(op)
    end_word()
    return tokens


# ---------------------------------------------------------------- paths

def home_dir() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def real(path: str) -> str:
    """realpath for a path that may not exist yet: resolve its nearest existing ancestor."""
    path = os.path.abspath(path)
    rest = []
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        rest.append(os.path.basename(path))
        path = parent
    return os.path.join(os.path.realpath(path), *reversed(rest))


def nearest_existing(path: str) -> str:
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


class Ctx:
    """What the shell knows at one point: the working directory (None when unknown), shell variables,
    exported variables, inherited variables removed by unset, and the folders mkdir has made so far
    (shared across the whole command)."""

    def __init__(self, cwd, created):
        self.cwd, self.created = cwd, created
        self.vars, self.exports, self.unset = {}, {}, set()

    def copy(self):
        c = Ctx(self.cwd, self.created)
        c.vars, c.exports, c.unset = dict(self.vars), dict(self.exports), set(self.unset)
        return c

    def child_env(self, prefix=None, removed=(), clean=False) -> dict:
        """The environment a program started here sees."""
        env = {} if clean else {k: v for k, v in os.environ.items() if k not in self.unset}
        if not clean:
            env.update(self.exports)
        env.update(prefix or {})
        for name in removed:
            env.pop(name, None)
        return env


def expand(word: str, ctx: Ctx, extra=None):
    """Expand ~ and $VAR / ${VAR}. Return None when a variable is unknown."""
    values = ctx.child_env()
    values.update(ctx.vars)
    values.update(extra or {})
    unknown = []

    def sub(m):
        name = m.group(1) or m.group(2)
        if name in values:
            return values[name]
        unknown.append(name)
        return ""

    out = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, word)
    if unknown or "$" in out or "`" in out:
        return None
    return os.path.expanduser(out)


def resolve(word, ctx: Ctx, extra=None, cwd=None):
    """The absolute path a word names, relative to the tracked cwd, or None when it cannot be known."""
    if word is None:
        return home_dir()
    path = expand(word, ctx, extra)
    if path is None:
        return None
    base = cwd if cwd is not None else ctx.cwd
    if not os.path.isabs(path):
        if base is None:
            return None
        path = os.path.join(base, path)
    return real(os.path.normpath(path))


def has_dir(path: str, created: set) -> bool:
    return os.path.isdir(path) or any(c == path or c.startswith(path + os.sep) for c in created)


def project_root(directory: str, created: set):
    """Mimic run_eval.find_project_root: the nearest directory, from here up, that holds .claude/.
    Folders made by an earlier mkdir in the same command count as existing."""
    path = directory
    while True:
        if has_dir(os.path.join(path, ".claude"), created):
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


def location_problem(directory: str, created: set):
    home = home_dir()
    claude_home = os.path.join(home, ".claude")
    if directory == claude_home or directory.startswith(claude_home + os.sep):
        return f"it runs inside {claude_home}"
    root = project_root(directory, created)
    if root is None or root == home:
        return ("the nearest folder with .claude/ is your home directory, so the skill copies go to "
                "~/.claude/commands/ and show up in every session")
    if in_git_repo(nearest_existing(root)):
        return f"the nearest folder with .claude/ is {root}, which is inside a git repository"
    return None


def is_skill_creator(script_path: str) -> bool:
    """Whether a run_eval.py / run_loop.py file belongs to skill-creator: its skill folder (the parent
    of scripts/) holds a SKILL.md whose frontmatter says name: skill-creator."""
    skill_md = os.path.join(os.path.dirname(os.path.dirname(script_path)), "SKILL.md")
    try:
        with open(skill_md, encoding="utf-8") as f:
            return bool(SKILL_NAME.search(f.read(4096)))
    except OSError:
        return False


# ---------------------------------------------------------------- command analysis

def workers_value(args: list):
    """The value given to --num-workers, last one winning, as argparse does. argparse also accepts
    unambiguous prefixes such as --num-w."""
    value = None
    for k, a in enumerate(args):
        key, eq, v = a.partition("=")
        if len(key) >= 5 and "--num-workers".startswith(key):
            value = v if eq else (args[k + 1] if k + 1 < len(args) else None)
    return value


def eval_target(argv: list):
    """When argv starts a run_eval / run_loop script, return (kind, target, args, python -B set),
    where kind is "file" (target is the path as written) or "module" (target is the module name).
    Otherwise None. Provenance is checked separately."""
    name = os.path.basename(argv[0])
    if name.endswith(".py") and name[:-3] in EVAL_SCRIPTS:
        return "file", argv[0], argv[1:], False
    if not PYTHON.fullmatch(name):
        return None
    no_bytecode, j = False, 1
    while j < len(argv):
        a = argv[j]
        if a == "--" or not a.startswith("-") or a == "-":
            break
        if a.startswith("--"):
            j += 1
            continue
        flags, k = a[1:], 0
        while k < len(flags):
            f = flags[k]
            if f == "B":
                no_bytecode = True
            elif f in "mcWX":
                value = flags[k + 1:] or (argv[j + 1] if j + 1 < len(argv) else "")
                consumed = 1 if flags[k + 1:] else 2
                if f == "c":
                    return None
                if f == "m":
                    if value.rsplit(".", 1)[-1] in EVAL_SCRIPTS:
                        return "module", value, argv[j + consumed:], no_bytecode
                    return None
                j += consumed - 1
                break
            k += 1
        j += 1
    if j < len(argv) and argv[j] == "--":
        j += 1
    if j < len(argv):
        base = os.path.basename(argv[j])
        if base.endswith(".py") and base[:-3] in EVAL_SCRIPTS:
            return "file", argv[j], argv[j + 1:], no_bytecode
    return None


def belongs_to_skill_creator(kind: str, target: str, ctx: Ctx, cwd, env: dict) -> bool:
    """False only when every file the script can resolve to sits outside a skill-creator skill
    folder, so another project's own run_eval.py passes. A script that cannot be resolved counts."""
    if kind == "file":
        path = resolve(target, ctx, env, cwd)
        candidates = [path] if path else []
    else:
        rel = target.replace(".", os.sep) + ".py"
        roots = [cwd] + [resolve(p, ctx, env, cwd) for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        candidates = [os.path.join(r, rel) for r in roots if r]
    # Python's import rules (a regular package beats a namespace portion found earlier, among
    # others) are not modeled: count the run when any existing candidate is skill-creator's.
    found = [p for p in candidates if os.path.isfile(p)]
    return not found or any(is_skill_creator(p) for p in found)


def shell_script(argv: list):
    """For bash -c / -lc / -ec ... return the script text, else None."""
    for k, a in enumerate(argv[1:], start=1):
        if a == "--" or not a.startswith("-"):
            return None
        if not a.startswith("--") and "c" in a[1:]:
            return argv[k + 1] if k + 1 < len(argv) else None
    return None


class Analyzer:
    def __init__(self):
        self.problems = []

    def add(self, problem: str) -> None:
        if problem not in self.problems:
            self.problems.append(problem)

    def script(self, text: str, ctx: Ctx, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            return
        stack = [ctx]
        argv, redirect_next, herestring = [], False, None
        last = None  # (shell reading stdin?, ctx) of the command a following heredoc belongs to

        def finish():
            nonlocal argv, herestring, last
            if argv:
                last = self.command(argv, stack[-1], depth)
                if herestring is not None and last and last[0]:
                    self.script(herestring, last[1].copy(), depth + 1)
            argv, herestring = [], None

        for kind, value, subs in tokenize(text):
            # A substitution runs where its word is, before the command around it.
            for body in subs:
                self.script(body, stack[-1].copy(), depth + 1)
            if kind == "w":
                if redirect_next:
                    redirect_next = False
                else:
                    argv.append(value)
            elif kind == "herestring":
                herestring = value
            elif kind == "heredoc":
                if last and last[0]:
                    self.script(value, last[1].copy(), depth + 1)
            elif value in SEPARATORS:
                finish()
            elif value == "(":
                finish()
                stack.append(stack[-1].copy())
            elif value == ")":
                finish()
                if len(stack) > 1:
                    stack.pop()
            elif value in REDIRECTS:
                redirect_next = True
        finish()

    def command(self, argv: list, ctx: Ctx, depth: int, env=None, removed=(), clean=False):
        """Analyze one simple command. Return (is a shell reading stdin, ctx) for heredoc routing."""
        env, removed = dict(env or {}), set(removed)
        i = 0
        while i < len(argv) and (ASSIGN.fullmatch(argv[i]) or argv[i] in KEYWORDS):
            if ASSIGN.fullmatch(argv[i]):
                key, _, value = argv[i].partition("=")
                env[key] = expand(value, ctx, env) or value
            i += 1
        argv = argv[i:]
        if not argv:
            # A bare assignment sets a shell variable; it reaches children only if exported.
            for key, value in env.items():
                ctx.vars[key] = value
                if key in ctx.exports:
                    ctx.exports[key] = value
            return None
        while argv:
            name = os.path.basename(argv[0])
            if name == "env":
                j = 1
                while j < len(argv) and (argv[j].startswith("-") or ASSIGN.fullmatch(argv[j])):
                    a = argv[j]
                    if ASSIGN.fullmatch(a):
                        key, _, value = a.partition("=")
                        env[key] = value
                    elif a in ("-i", "--ignore-environment", "-"):
                        clean = True
                        env = {}  # env -i also drops assignments made before it
                    elif a in ("-u", "--unset") and j + 1 < len(argv):
                        removed.add(argv[j + 1])
                        env.pop(argv[j + 1], None)
                        j += 1
                    elif a.startswith("--unset="):
                        removed.add(a.split("=", 1)[1])
                    elif a in ("-C", "--chdir") or a.startswith("--chdir="):
                        target = a.split("=", 1)[1] if "=" in a else (argv[j + 1] if j + 1 < len(argv) else None)
                        ctx = ctx.copy()  # the folder change lasts for this command only
                        ctx.cwd = resolve(target, ctx, env)
                        j += 0 if "=" in a else 1
                    j += 1
                argv = argv[j:]
            elif name in WRAPPERS:
                argv = argv[1:]
            elif name == "nice":
                argv = argv[3:] if argv[1:2] == ["-n"] else argv[1:]
            elif name == "timeout":
                j = 1
                while j < len(argv) and argv[j].startswith("-"):
                    j += 2 if argv[j] in ("-s", "--signal", "-k", "--kill-after") else 1
                argv = argv[j + 1:]
            else:
                break
        if not argv:
            return None
        name = os.path.basename(argv[0])

        if name in ("cd", "pushd"):
            args = [a for a in argv[1:] if not (a.startswith("-") and a != "-")]
            ctx.cwd = None if args[:1] == ["-"] else resolve(args[0] if args else None, ctx, env)
            return None
        if name == "export":
            for a in argv[1:]:
                key, eq, value = a.partition("=")
                if eq:
                    ctx.exports[key] = ctx.vars[key] = expand(value, ctx, env) or value
                elif key in ctx.vars:
                    ctx.exports[key] = ctx.vars[key]
                ctx.unset.discard(key)
            return None
        if name == "unset":
            for a in argv[1:]:
                ctx.exports.pop(a, None)
                ctx.vars.pop(a, None)
                ctx.unset.add(a)
            return None
        if name == "mkdir":
            j = 1
            while j < len(argv):
                a = argv[j]
                if a in ("-m", "--mode"):
                    j += 2
                    continue
                if not a.startswith("-"):
                    path = resolve(a, ctx, env)
                    if path:
                        ctx.created.add(path)
                j += 1
            return None
        if name in SHELLS:
            body = shell_script(argv)
            if body is not None:
                self.script(body, ctx.copy(), depth + 1)
                return None
            return (True, ctx.copy())
        if name == "uv" and argv[1:2] == ["run"]:
            sub, j = ctx.copy(), 2
            while j < len(argv) and argv[j].startswith("-") and argv[j] != "--":
                key, eq, value = argv[j].partition("=")
                if key == "--directory":
                    sub.cwd = resolve(value if eq else (argv[j + 1] if j + 1 < len(argv) else None), ctx, env)
                j += 1 if eq or key not in UV_VALUE_OPTIONS else 2
            if j < len(argv) and argv[j] == "--":
                j += 1
            return self.command(argv[j:], sub, depth, env, removed, clean) if j < len(argv) else None

        found = eval_target(argv)
        if found is None:
            return None
        kind, target, args, no_bytecode = found
        child = ctx.child_env(env, removed, clean)
        if ctx.cwd is not None and not belongs_to_skill_creator(kind, target, ctx, ctx.cwd, child):
            return None  # another project's own run_eval.py
        value = workers_value(args)
        try:
            one = value is not None and int(value) == 1
        except ValueError:
            one = False
        if not one:
            self.add("`--num-workers 1` is missing")
        if not (no_bytecode or child.get("PYTHONDONTWRITEBYTECODE")):
            self.add("`PYTHONDONTWRITEBYTECODE=1` is not set")
        if ctx.cwd is None:
            self.add(UNKNOWN_FOLDER)
        else:
            where = location_problem(ctx.cwd, ctx.created)
            if where:
                self.add(where)
        return None


# ---------------------------------------------------------------- hook events

def pre_tool_use(data: dict) -> None:
    command = (data.get("tool_input") or {}).get("command") or ""
    if not re.search(r"run_(?:eval|loop)", command):
        return  # fast path: nothing to parse
    analyzer = Analyzer()
    analyzer.script(command, Ctx(real(data.get("cwd") or os.getcwd()), set()))
    if not analyzer.problems:
        return
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Blocked a skill-creator eval run: " + "; ".join(analyzer.problems)
                                    + ".\n\n" + RULES,
    }})


def post_tool_use(data: dict) -> None:
    skill = str((data.get("tool_input") or {}).get("skill") or "")
    if "skill-creator" not in skill:
        return
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": RULES}})


# ---------------------------------------------------------------- self-tests

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
            for script in ("run_eval.py", "run_loop.py"):
                open(os.path.join(cls.cache, "scripts", script), "w").close()
            open(os.path.join(cls.cache, "scripts", "__init__.py"), "w").close()
            with open(os.path.join(cls.cache, "SKILL.md"), "w") as f:
                f.write("---\nname: skill-creator\ndescription: x\n---\n")
            cls.scratch = os.path.join(home, "scratch")             # the right place: its own .claude/
            os.makedirs(os.path.join(cls.scratch, ".claude"))
            cls.repo_plain = os.path.join(home, "work", "repo")      # a repo without .claude/
            cls.repo_claude = os.path.join(home, "work", "repo2")    # a repo with its own .claude/
            cls.other = os.path.join(home, "work", "other")          # another project's own run_eval.py
            os.makedirs(cls.repo_plain)
            os.makedirs(os.path.join(cls.repo_claude, ".claude"))
            os.makedirs(os.path.join(cls.other, "scripts"))
            open(os.path.join(cls.other, "scripts", "run_eval.py"), "w").close()
            with open(os.path.join(cls.other, "SKILL.md"), "w") as f:
                f.write("---\nname: something-else\n---\n")
            cls.has_git = shutil.which("git") is not None
            if cls.has_git:
                for repo in (cls.repo_plain, cls.repo_claude, cls.other):
                    subprocess.run(["git", "init", "-q", repo], check=True)

        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(cls.tmp, ignore_errors=True)

        def hook(self, payload, raw=None, extra_env=None):
            """Run this script the way Claude Code does: JSON on stdin, HOME set to the fake home."""
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH")}
            env["HOME"] = self.home
            env.update(extra_env or {})
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

        def bash(self, command, cwd=None, extra_env=None):
            return self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "cwd": cwd or self.scratch, "tool_input": {"command": command}},
                             extra_env=extra_env)

        def skill(self, name):
            return self.hook({"hook_event_name": "PostToolUse", "tool_name": "Skill",
                              "cwd": self.scratch, "tool_input": {"skill": name}})

        RUN = "PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_eval --eval-set e.json --skill-path x --num-workers 1"
        MISSING = "`--num-workers 1` is missing"
        BYTECODE = "`PYTHONDONTWRITEBYTECODE=1` is not set"
        GIT = "inside a git repository"

        def good(self, where=None):
            return f"cd {where or self.scratch} && {self.RUN}"

        def expect(self, result, decision, needle=""):
            self.assertEqual(result[0], decision, result[1][:300])
            self.assertIn(needle, result[1])

        def need_git(self):
            if not self.has_git:
                self.skipTest("git is not installed")

        # Allowed runs.

        def test_good_run_is_allowed(self):
            self.expect(self.bash(self.good()), "allow")
            self.expect(self.bash(self.good().replace("--num-workers 1", "--num-workers=1")), "allow")
            self.expect(self.bash(self.good().replace("--num-workers 1", '--num-workers "1"')), "allow")
            self.expect(self.bash(self.good().replace("--num-workers 1", "--num-w 1")), "allow")
            no_env = self.good().replace("PYTHONDONTWRITEBYTECODE=1 ", "")
            self.expect(self.bash(no_env.replace("python3 -m", "python3 -B -m")), "allow")
            self.expect(self.bash(no_env.replace("python3 -m", "python3 -Bm")), "allow")
            self.expect(self.bash(f"export PYTHONDONTWRITEBYTECODE=1\n{no_env}"), "allow")
            self.expect(self.bash(no_env.replace("python3", "env PYTHONDONTWRITEBYTECODE=1 python3")), "allow")
            self.expect(self.bash(f'cd "{self.scratch}" && PYTHONDONTWRITEBYTECODE=1 '
                                  f'python3 "{self.cache}/scripts/run_eval.py" --num-workers 1'), "allow")
            self.expect(self.bash(f"S={self.scratch}; cd \"$S\" && {self.RUN}"), "allow")

        def test_folder_created_by_the_same_command_is_allowed(self):
            new = os.path.join(self.home, "not-yet")
            self.expect(self.bash(f"mkdir -p {new}/.claude && {self.good(new)}"), "allow")

        def test_quoted_cd_paths_count(self):
            spaced = os.path.join(self.home, "scratch dir")
            os.makedirs(os.path.join(spaced, ".claude"), exist_ok=True)
            self.expect(self.bash(f'cd "{spaced}" && {self.RUN}'), "allow")
            self.expect(self.bash(f"cd '{self.cache}' && {self.RUN}"), "deny", "it runs inside")

        def test_another_projects_run_eval_is_not_skill_creators(self):
            self.need_git()
            self.expect(self.bash(f"cd {self.other} && ./scripts/run_eval.py --num-workers 10"), "allow")
            self.expect(self.bash(f"cd {self.other} && python3 -m scripts.run_eval"), "allow")
            # With skill-creator's copy on PYTHONPATH, Python can pick it over a namespace portion in the
            # cwd, so the run counts.
            self.expect(self.bash(f"cd {self.other} && PYTHONPATH={self.cache} python3 -m scripts.run_eval"),
                        "deny", self.MISSING)
            self.expect(self.bash(f"cd {self.repo_plain} && PYTHONPATH={self.cache} python3 -m scripts.run_eval"),
                        "deny", self.MISSING)

        # Blocked runs.

        def test_parallel_runs_are_blocked(self):
            self.expect(self.bash(self.good().replace(" --num-workers 1", "")), "deny", self.MISSING)
            self.expect(self.bash(self.good().replace("--num-workers 1", "--num-workers 12")), "deny", self.MISSING)
            self.expect(self.bash(self.good() + " --num-workers 4"), "deny", self.MISSING)  # the last one wins
            loop = f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_loop --num-workers 10"
            self.expect(self.bash(loop), "deny", self.MISSING)

        def test_bytecode_must_be_off_for_the_eval_itself(self):
            no_env = self.good().replace("PYTHONDONTWRITEBYTECODE=1 ", "")
            self.expect(self.bash(no_env), "deny", self.BYTECODE)
            # Set for another command only, or as a plain shell variable that is not exported.
            self.expect(self.bash(f"PYTHONDONTWRITEBYTECODE=1 echo x; {no_env}"), "deny", self.BYTECODE)
            self.expect(self.bash(f"PYTHONDONTWRITEBYTECODE=1\n{no_env}"), "deny", self.BYTECODE)
            # Inherited from the environment, then removed.
            inherited = {"PYTHONDONTWRITEBYTECODE": "1"}
            self.expect(self.bash(no_env, extra_env=inherited), "allow")
            self.expect(self.bash(no_env.replace("python3", "env -u PYTHONDONTWRITEBYTECODE python3"),
                                  extra_env=inherited), "deny", self.BYTECODE)
            self.expect(self.bash(no_env.replace("python3", "env -i python3"), extra_env=inherited),
                        "deny", self.BYTECODE)
            self.expect(self.bash(f"unset PYTHONDONTWRITEBYTECODE; {no_env}", extra_env=inherited),
                        "deny", self.BYTECODE)

        def test_flags_elsewhere_do_not_count(self):
            result = self.bash(f"echo --num-workers 1 PYTHONDONTWRITEBYTECODE=1; cd {self.scratch} && "
                               "python3 -m scripts.run_eval")
            self.expect(result, "deny", self.MISSING)
            self.assertIn(self.BYTECODE, result[1])

        def test_every_way_of_starting_the_script_is_seen(self):
            self.expect(self.bash(f"cd {self.cache} && ./scripts/run_eval.py --num-workers 10"), "deny", self.MISSING)
            self.expect(self.bash(f'PYTHONDONTWRITEBYTECODE=1 python3 "{self.cache}/scripts/run_eval.py"'),
                        "deny", self.MISSING)
            self.expect(self.bash(f'cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 python3 -m "scripts.run_eval"'),
                        "deny", self.MISSING)
            bad = f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_eval"
            for wrapped in (f'bash -c "{bad}"', f'bash -lc "{bad}"', f'zsh -lc "{bad}"', f'sh -ec "{bad}"',
                            f"bash <<'EOF'\n{bad}\nEOF", f"x=$({bad})",
                            f"cd {self.scratch} && nohup python3 -m scripts.run_eval > log 2>&1 &"):
                with self.subTest(wrapped=wrapped):
                    self.expect(self.bash(wrapped), "deny", self.MISSING)
            uv = (f"PYTHONDONTWRITEBYTECODE=1 uv run --with anthropic --directory {self.cache} "
                  "python -m scripts.run_loop --num-workers 1")
            self.expect(self.bash(uv), "deny", "it runs inside")

        def test_wrong_places_are_blocked(self):
            self.expect(self.bash(self.good(self.cache)), "deny", "it runs inside " + os.path.join(self.home, ".claude"))
            self.expect(self.bash(self.good("~")), "deny", "home directory")
            self.expect(self.bash(self.good(os.path.join(self.home, "nowhere"))), "deny", "home directory")
            # No cd: the hook's cwd decides. A repo without .claude/ sends the copies to the home folder.
            self.expect(self.bash(self.RUN, cwd=self.repo_plain), "deny", "home directory")

        def test_unknown_folder_is_blocked(self):
            self.expect(self.bash(f'cd "$NOT_SET" && {self.RUN}'), "deny", "cannot be worked out")
            self.expect(self.bash(f"cd - && {self.RUN}"), "deny", "cannot be worked out")

        def test_repo_cases_are_blocked(self):
            self.need_git()
            self.expect(self.bash(self.good(self.repo_claude)), "deny", self.GIT)
            # A cd inside a subshell does not move the eval.
            self.expect(self.bash(f"(cd {self.scratch}) && {self.RUN}", cwd=self.repo_claude), "deny", self.GIT)
            # A cd on a later line does.
            self.expect(self.bash(f"echo ok\ncd {self.repo_claude}\n{self.RUN}"), "deny", self.GIT)
            # A folder made inside a repo is still inside the repo.
            new = os.path.join(self.repo_claude, "new")
            self.expect(self.bash(f"mkdir -p {new}/.claude && {self.good(new)}"), "deny", self.GIT)
            # A shell variable carries the folder.
            self.expect(self.bash(f'BAD={self.repo_claude}; cd "$BAD"; {self.RUN}'), "deny", self.GIT)
            # An IO number before the command is not the command.
            self.expect(self.bash(f"2>log {self.RUN}", cwd=self.repo_claude), "deny", self.GIT)

        def test_unquoted_heredoc_body_expands(self):
            run10 = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={self.cache} python3 -m scripts.run_eval --num-workers 10"
            self.expect(self.bash(f"cat <<EOF\n$({run10})\nEOF"), "deny", self.MISSING)
            self.expect(self.bash(f"cat <<EOF\n`{run10}`\nEOF"), "deny", self.MISSING)
            # A quoted delimiter keeps the body literal.
            self.expect(self.bash(f"cat <<'EOF'\n$({run10})\nEOF"), "allow")

        def test_env_options_are_modeled(self):
            run = "PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_eval --num-workers 1"
            self.expect(self.bash(f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 env -i python3 -m scripts.run_eval "
                                  "--num-workers 1"), "deny", self.BYTECODE)
            self.expect(self.bash(f"cd {self.scratch} && env -i {run}"), "allow")
            self.need_git()
            self.expect(self.bash(f"env -C {self.repo_claude} {run}"), "deny", self.GIT)
            self.expect(self.bash(f"env --chdir={self.repo_claude} {run}"), "deny", self.GIT)
            self.expect(self.bash(f"env -C {self.scratch} {run}", cwd=self.repo_claude), "allow")

        def test_substitution_runs_where_it_is(self):
            self.need_git()
            sub = f"cd {self.repo_claude}; x=$({self.RUN}); cd {self.scratch}"
            self.expect(self.bash(sub), "deny", self.GIT)
            self.expect(self.bash(f"cd {self.repo_claude}; echo \"`{self.RUN}`\"; cd {self.scratch}"), "deny", self.GIT)

        # Text that only mentions the script.

        def test_mentions_are_allowed(self):
            bad = "cd /tmp && python3 -m scripts.run_eval --help"
            for cmd in (f'claude -p "hook test: run {bad}" --max-turns 3',
                        f"git commit -m 'note: {bad} breaks the rules'",
                        f"git commit -F - <<'EOF'\nfix: {bad}\nEOF\ngit log -1",
                        f'echo "it ran \\"{bad}\\" here"',
                        "echo python3 -m scripts.run_eval --help",
                        f"ls  # {bad}",
                        f"cd {self.cache} && grep -n num_workers scripts/run_eval.py",
                        f"sed -n 1,20p {self.cache}/scripts/run_eval.py",
                        "git status"):
                with self.subTest(cmd=cmd):
                    self.expect(self.bash(cmd), "allow")

        def test_quoted_heredoc_marker_hides_nothing(self):
            bad = f"cd {self.scratch} && PYTHONDONTWRITEBYTECODE=1 python3 -m scripts.run_eval"
            self.expect(self.bash(f'echo "<<EOF"\n{bad}'), "deny", self.MISSING)
            self.expect(self.bash(f"cat <<EOF > notes.txt\nhello\nEOF\n{bad}"), "deny", self.MISSING)

        # The Skill hook and the rest.

        def test_rules_follow_skill_creator_only(self):
            for name in ("skill-creator:skill-creator", "anthropic-skills:skill-creator"):
                self.expect(self.skill(name), "context", "--num-workers 1")
            self.expect(self.skill("plugin-freshness:plugin-freshness"), "allow")

        def test_other_events_and_garbage_are_ignored(self):
            self.expect(self.hook(None, raw="not json"), "allow")
            self.expect(self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                   "tool_input": {"command": self.good().replace(" --num-workers 1", "")}}),
                        "allow")

        def test_tokenizer(self):
            def words(s):
                return [v for k, v, _ in tokenize(s) if k == "w"]
            self.assertEqual(words("a 'b c' \"d \\\" e\" f\\ g"), ["a", "b c", 'd " e', "f g"])
            self.assertEqual(words("a # b c"), ["a"])
            self.assertEqual(words("x=$(echo 'a b') y"), ["x=$(echo 'a b')", "y"])
            self.assertEqual(words("2>log cmd 3<&0 '4'>x"), ["log", "cmd", "0", "4", "x"])
            toks = tokenize("cat <<-EOF > f\n\tbody\n\tEOF\nnext")
            self.assertIn(("heredoc", "\tbody", []), toks)
            self.assertEqual(words("cat <<-EOF > f\n\tbody\n\tEOF\nnext"), ["cat", "f", "next"])
            self.assertEqual([t[2] for t in tokenize("a `b c` d")], [[], ["b c"], []])

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
