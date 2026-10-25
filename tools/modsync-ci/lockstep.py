#!/usr/bin/env python3
"""Install one guide twice, side by side, mod by mod, and diff the two game folders after every mod.

Both installs start from byte-identical copies of the same game folder and differ only in the
patcher that runs their patcher steps (for example HoloPatcher in one, OdyPatcher in the other).
For every step (one mod) of --steps both commands run at the same time; when both have finished
the two folders are compared, and only then does the next step start. The first step that fails
on either side or leaves the two folders different stops the run: from there on every later
diff would only repeat the first one, and a patcher that disagrees is a bug in one of them.

Diffs are incremental. After every step each folder is re-scanned with stat only (no reads); a
path whose (kind, size, mtime, ctime, inode) changed in either folder since the last step is
compared, everything else is known equal from the previous step. Files of equal size are hashed
(SHA-256, in parallel) and hashes are cached by stat signature, so an untouched file is never read
twice. ctime cannot be set from user space, so a rewrite that keeps size and mtime is still seen.
Paths are matched case-insensitively, as on Windows; a path that exists in both folders with
different case is a discrepancy of its own (kind "case"), not a missing plus an extra file, and a
path with two spellings in one folder that it did not have at the start is a "case_collision".

  lockstep.py run  --steps FILE    --tree NAME=DIR --tree NAME=DIR --work-dir DIR --report FILE
                   [--evidence-dir DIR] [--summary FILE] [--step-timeout SECONDS]
  lockstep.py pair --commands FILE (same options) [--first-step-timeout SECONDS]
  lockstep.py wrapper --real PATCHER --barrier-dir WORK/NAME/barrier --out FILE
  lockstep.py barrier --id ID [--label TEXT] [--exit N]

`run` starts each step's commands itself. `pair` starts two long-running installers once
({"commands": {NAME: {"argv", "cwd"?, "env"?}}}, "{barrier}" filled in) and diffs whenever both
have reached their next barrier (see Barrier): manual.py reaches one after every mod, ModSync reaches
one after every patcher run through the executable `wrapper` writes. The patcher is the only part
that differs between the two sides, so that is where they can diverge first. A barrier step that
failed on both sides with equal folders is recorded (failed_on_both) and both go on, so the
installer's own failure handling decides; a failure on one side stops the pair, and so does a pair
that ends without reaching a single barrier. When the pair stops, every later barrier answers stop
at once (the wrapper then no longer runs the patcher) and each installer's process group gets
SIGTERM, then SIGKILL, each after STOP_GRACE. Each installer's output is in WORK/NAME/install.log.

--steps is JSON: {"steps": [{"id": str, "label": str, "commands": {NAME: {"argv": [...],
"cwd": str?, "env": {str: str}?}}}]}, with one command per --tree NAME. A step may omit a command
for a tree only by giving every tree none (a step with no commands is a no-op and is skipped).
Each command's output goes to WORK/step-NNNN/NAME.log. The report lists every step with both exit
codes, durations, the number of changed and hashed paths and, at the stop, every discrepancy (sizes,
SHA-256 and the first differing byte offset). With --evidence-dir the diverging files of both sides
are copied there (at most EVIDENCE_LIMIT bytes in total) for a sealed diagnostics upload.

Exit status: 0 when the folders never differed and no step failed on one side only (`pair`: and
both installers exited 0), 1 otherwise.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time

DEFAULT_STEP_TIMEOUT = 1800
EVIDENCE_LIMIT = 256 * 1024 * 1024
# File timestamps come from the kernel's coarse clock (one tick, a few milliseconds). Waiting one
# tick after a scan means a write by the next step can never carry the timestamp the scan saw.
CLOCK_TICK = 0.02
HASH_WORKERS = max(2, min(16, (os.cpu_count() or 2) * 2))


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def first_difference(left, right):
    """Byte offset of the first difference between two files, or None when they are equal."""
    offset = 0
    with open(left, "rb") as a, open(right, "rb") as b:
        while True:
            x, y = a.read(1 << 20), b.read(1 << 20)
            if x != y:
                for index, (p, q) in enumerate(zip(x, y)):
                    if p != q:
                        return offset + index
                return offset + min(len(x), len(y))
            if not x:
                return None
            offset += len(x)


def kind_of(mode):
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def scan(root, ignore=frozenset()):
    """({casefolded relative path: (relative path, kind, size, mtime_ns, ctime_ns, inode)}, collisions).

    collisions maps a casefolded path to every spelling of it in the tree when there is more than one:
    on Windows those are one file, so two of them on Linux mean a patcher missed an existing file.
    ignore holds casefolded relative paths left out with everything under them (an installer's own
    bookkeeping, such as ModSync.ts checkpoints in .modsync)."""
    entries, spellings = {}, {}
    pending = [""]
    while pending:
        relative = pending.pop()
        with os.scandir(os.path.join(root, relative) if relative else root) as listing:
            for entry in listing:
                info = entry.stat(follow_symlinks=False)
                path = relative + "/" + entry.name if relative else entry.name
                kind = kind_of(info.st_mode)
                size = info.st_size if kind == "file" else 0
                key = path.casefold()
                if key in ignore:
                    continue
                if key in entries:
                    spellings.setdefault(key, [entries[key][0]]).append(path)
                entries[key] = (path, kind, size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
                if kind == "dir":
                    pending.append(path)
    return entries, {key: sorted(paths) for key, paths in spellings.items()}


class Tree:
    def __init__(self, name, root, ignore=frozenset()):
        self.name = name
        self.root = Path(root)
        self.ignore = ignore
        self.entries = {}
        self.collisions = {}
        self.hashes = {}

    def rescan(self):
        previous, (self.entries, self.collisions) = self.entries, scan(self.root, self.ignore)
        changed = {key for key, value in self.entries.items() if previous.get(key) != value}
        changed.update(key for key in previous if key not in self.entries)
        # A colliding path keeps whichever spelling scandir listed last, so it is always rechecked.
        changed.update(self.collisions)
        # Drop cached hashes of entries that are gone or changed; the signature keys the cache.
        self.hashes = {key: value for key, value in self.hashes.items()
                       if key not in changed and key in self.entries}
        return changed

    def path(self, key):
        return self.root / self.entries[key][0]

    def sha256(self, key):
        if key not in self.hashes:
            self.hashes[key] = digest(self.path(key))
        return self.hashes[key]


def describe(tree, key, with_hash=False):
    entry = tree.entries.get(key)
    if entry is None:
        return None
    path, kind, size = entry[:3]
    described = {"path": path, "kind": kind}
    if kind == "file":
        described["size"] = size
        if with_hash:
            described["sha256"] = tree.sha256(key)
    elif kind == "symlink":
        described["target"] = os.readlink(tree.root / path)
    return described


def leaf(path):
    return path.rpartition("/")[2]


def spelling_differs(left, right, path):
    a, b = left.root / path, right.root / path
    if a.is_file() != b.is_file():
        return True
    return a.is_file() and digest(a) != digest(b)


def compare(left, right, keys, known_collisions=None):
    """Discrepancies between two trees over the given casefolded keys, in path order.

    A path that has two spellings in one tree and had not at the baseline is a "case_collision" even
    when both trees have it: on Windows the second write would have replaced the first."""
    known_collisions = known_collisions or {}
    to_hash, found, collisions = [], [], {}
    for key in sorted(keys):
        spellings = {tree.name: tree.collisions.get(key) for tree in (left, right)}
        if any(spellings.values()):
            new = any(value and value != known_collisions.get((name, key)) for name, value in spellings.items())
            if new or spellings[left.name] != spellings[right.name]:
                collisions[key] = spellings
                found.append((key, "case_collision"))
            elif any(spelling_differs(left, right, path) for path in spellings[left.name]):
                # The same collision in both, inherited from the baseline: every spelling is compared.
                collisions[key] = spellings
                found.append((key, "collision_content"))
            continue
        a, b = left.entries.get(key), right.entries.get(key)
        if a is None and b is None:
            continue
        if a is None or b is None:
            found.append((key, "only_in_" + (left.name if b is None else right.name)))
        elif a[1] != b[1]:
            found.append((key, "type"))
        elif leaf(a[0]) != leaf(b[0]):
            # Only the last component: a folder in different case is one discrepancy, and the files in it
            # are still compared by content.
            found.append((key, "case"))
        elif a[1] == "file":
            if a[2] != b[2]:
                found.append((key, "content"))
            else:
                to_hash.append(key)
        elif a[1] == "symlink" and os.readlink(left.path(key)) != os.readlink(right.path(key)):
            found.append((key, "symlink"))
    if to_hash:
        with ThreadPoolExecutor(HASH_WORKERS) as pool:
            hashed = list(pool.map(lambda key: (left.sha256(key), right.sha256(key)), to_hash))
        found.extend((key, "content") for key, (a, b) in zip(to_hash, hashed) if a != b)
    found.sort()
    discrepancies = []
    for key, kind in found:
        item = {"kind": kind, left.name: describe(left, key, True), right.name: describe(right, key, True)}
        if key in collisions:
            item["spellings"] = collisions[key]
        if kind == "content":
            item["first_difference"] = first_difference(left.path(key), right.path(key))
        discrepancies.append(item)
    return discrepancies, len(to_hash)


def keep_evidence(left, right, discrepancies, directory):
    budget = EVIDENCE_LIMIT
    for item in discrepancies:
        for tree in (left, right):
            side = item[tree.name]
            if not side or side["kind"] != "file":
                continue
            if side["size"] > budget:
                side["evidence"] = "not kept: over the evidence limit"
                continue
            destination = Path(directory) / tree.name / side["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tree.root / side["path"], destination)
            budget -= side["size"]
            side["evidence"] = str(Path(tree.name) / side["path"])


def launch(command, log):
    env = dict(os.environ)
    env.update(command.get("env") or {})
    stream = open(log, "wb")
    try:
        process = subprocess.Popen(command["argv"], cwd=command.get("cwd"), env=env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        stream.write(("lockstep: cannot start %s: %s\n" % (command["argv"][0], error)).encode())
        stream.close()
        return None, stream
    return process, stream


def stop(process):
    """SIGTERM first, so the install can stop the programs it started in their own sessions, then SIGKILL."""
    for sig, grace in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            pass
    process.wait()


def run_step(step, names, work, timeout):
    """Start every side's command at once and wait for all of them; {name: result}."""
    started = time.monotonic()
    running = {}
    for name in names:
        log = work / (name + ".log")
        process, stream = launch(step["commands"][name], log)
        running[name] = (process, stream, log)
    results = {}
    deadline = started + timeout
    for name, (process, stream, log) in running.items():
        result = {"log": str(log)}
        if process is None:
            result.update(exit=None, error="could not start")
        else:
            try:
                result["exit"] = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                stop(process)
                result.update(exit=None, error="timed out after %d seconds" % timeout)
            # A patcher may leave helpers behind (wineserver, a GUI); none may write into the next step.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stream.close()
        result["seconds"] = round(time.monotonic() - started, 1)
        results[name] = result
    return results


def load_steps(path, names):
    steps = json.loads(Path(path).read_text(encoding="utf-8"))["steps"]
    seen = set()
    for index, step in enumerate(steps):
        identifier = step.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise SystemExit("lockstep: step %d needs a unique id" % index)
        seen.add(identifier)
        commands = step.get("commands") or {}
        if commands and set(commands) != set(names):
            raise SystemExit("lockstep: step %s must have a command for each of %s, has %s"
                             % (identifier, ", ".join(names), ", ".join(sorted(commands)) or "none"))
        for name, command in commands.items():
            argv = command.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
                raise SystemExit("lockstep: step %s, %s: argv must be a non-empty list of strings" % (identifier, name))
    return steps




def summarize(report, path):
    left, right = report["trees"]
    lines = ["### lockstep: %s vs %s" % (left["name"], right["name"]), "",
             "%s after %d step(s)%s." % (report["status"], len(report["steps"]),
                                         " of %d" % report["planned_steps"] if "planned_steps" in report else "")]
    stopped = report.get("stopped_at")
    if stopped:
        lines += ["", "Stopped at step `%s` (%s): %s" % (stopped["id"], stopped["label"], stopped["reason"])]
        for item in stopped.get("discrepancies", [])[:50]:
            sides = [item.get(left["name"]), item.get(right["name"])]
            path_text = next(side["path"] for side in sides if side)
            lines.append("- `%s` %s" % (path_text, item["kind"]))
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


class Session:
    """The two trees, the report and the verdict shared by both modes."""

    def __init__(self, args):
        named = []
        for value in args.tree:
            name, _, root = value.partition("=")
            if not name or not root or not Path(root).is_dir():
                raise SystemExit("lockstep: --tree %s must be NAME=existing directory" % value)
            named.append((name, root))
        if len(named) != 2 or named[0][0] == named[1][0]:
            raise SystemExit("lockstep: give exactly two --tree options with different names")
        self.args = args
        self.names = [name for name, _ in named]
        ignore = frozenset(path.replace("\\", "/").strip("/").casefold() for path in args.ignore or ())
        self.left, self.right = (Tree(name, root, ignore) for name, root in named)
        self.work = Path(args.work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.known_collisions = {}
        self.report = {"status": "running",
                       "trees": [{"name": tree.name, "root": str(tree.root)} for tree in (self.left, self.right)],
                       "ignored": sorted(ignore),
                       "steps": []}

    @property
    def running(self):
        return self.report["status"] == "running"

    def write(self):
        Path(self.args.report).write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")

    def stop(self, identifier, label, reason, discrepancies=(), status=None):
        self.report["status"] = status or ("discrepancy" if discrepancies else "failed")
        self.report["stopped_at"] = {"id": identifier, "label": label, "reason": reason,
                                     "discrepancies": list(discrepancies)}
        for item in list(discrepancies)[:20]:
            side = item.get(self.left.name) or item.get(self.right.name)
            print("::error title=lockstep discrepancy::%s: %s differs (%s) between %s and %s"
                  % (label, side["path"], item["kind"], self.left.name, self.right.name), flush=True)
        if not discrepancies:
            print("::error title=lockstep stopped::%s: %s" % (label, reason), flush=True)

    def baseline(self):
        """Both copies must start equal, so the first comparison covers every path once."""
        keys = self.left.rescan() | self.right.rescan()
        # Collisions both copies start with are the game's, not a patcher's; they are still compared.
        self.known_collisions = {(tree.name, key): spellings for tree in (self.left, self.right)
                                 for key, spellings in tree.collisions.items()}
        discrepancies, hashed = compare(self.left, self.right, keys, self.known_collisions)
        self.report["baseline"] = {"paths": len(keys), "hashed": hashed,
                                   "case_collisions": sorted({key for _, key in self.known_collisions})}
        if discrepancies:
            self.stop("baseline", "the two starting copies", "the two game folders differ before any step ran",
                      discrepancies)
        self.write()

    def checkpoint(self, number, entry, failures=()):
        """Diff what changed since the last checkpoint; stop on a failure or a difference."""
        started = time.monotonic()
        changed = self.left.rescan() | self.right.rescan()
        discrepancies, hashed = compare(self.left, self.right, changed, self.known_collisions)
        entry.update(changed=len(changed), hashed=hashed, diff_seconds=round(time.monotonic() - started, 2))
        reasons = list(failures)
        if discrepancies:
            reasons.append("%d path(s) differ" % len(discrepancies))
            if self.args.evidence_dir:
                keep_evidence(self.left, self.right, discrepancies,
                              Path(self.args.evidence_dir) / ("step-%04d" % number))
        if reasons:
            self.stop(entry["id"], entry["label"], "; ".join(reasons), discrepancies)
        self.write()
        time.sleep(CLOCK_TICK)
        return not reasons

    def finish(self):
        if self.running:
            self.report["status"] = "equivalent"
        self.write()
        if self.args.summary:
            summarize(self.report, self.args.summary)
        print("lockstep: %s (%d step(s))" % (self.report["status"], len(self.report["steps"])), flush=True)
        return 0 if self.report["status"] == "equivalent" else 1


def command_run(args):
    session = Session(args)
    steps = load_steps(args.steps, session.names)
    session.report["planned_steps"] = len(steps)
    session.baseline()
    for number, step in enumerate(steps, 1):
        if not session.running:
            break
        label = step.get("label") or step["id"]
        entry = {"id": step["id"], "label": label}
        session.report["steps"].append(entry)
        if not step.get("commands"):
            entry["skipped"] = "no commands"
            continue
        print("lockstep: step %d/%d %s" % (number, len(steps), label), flush=True)
        step_work = session.work / ("step-%04d" % number)
        step_work.mkdir(parents=True, exist_ok=True)
        entry["results"] = results = run_step(step, session.names, step_work, args.step_timeout)
        failures = ["failed on %s (%s)" % (name, results[name].get("error") or "exit %s" % results[name]["exit"])
                    for name in session.names if results[name].get("exit") != 0]
        session.checkpoint(number, entry, failures)
    return session.finish()


BARRIER_POLL = 0.2
# After a stop the item has failed: give each side a moment to write its report, then kill it.
STOP_GRACE = 60
STOPPED_EXIT = 3  # `lockstep.py barrier` was told to stop
STOP_ALL = "STOP"  # in a barrier dir: every later barrier answers stop at once (retries, later mods)


def barrier_file(directory, number, suffix):
    return Path(directory) / ("%06d.%s" % (number, suffix))


def signal_barrier(directory, number, suffix):
    """Answer a waiting side: "go" lets it start the next step, "stop" makes it halt."""
    temporary = barrier_file(directory, number, suffix + ".tmp")
    temporary.write_text(suffix + "\n", encoding="utf-8")
    os.replace(temporary, barrier_file(directory, number, suffix))


def read_ready(directory, number):
    path = barrier_file(directory, number, "ready")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # written by rename, so a partial read is only a race with the writer; poll again


class Barrier:
    """The installer's side of the barrier protocol.

    After every mod the installer calls reach(): it writes NNNNNN.ready ({"id", "label", "exit", "error"?})
    into LOCKSTEP_BARRIER_DIR and blocks until the coordinator answers NNNNNN.go (True: start the next mod)
    or NNNNNN.stop (False: halt now). Without LOCKSTEP_BARRIER_DIR it returns True at once, so an
    installer calls it unconditionally and runs unchanged outside a pair."""

    def __init__(self, directory=None):
        directory = directory or os.environ.get("LOCKSTEP_BARRIER_DIR")
        self.directory = Path(directory) if directory else None
        # Count what earlier processes reached, so one process per mod works as well as one for all.
        self.number = len(list(self.directory.glob("*.ready"))) if self.directory else 0

    def reach(self, identifier, label=None, exit=0, error=None, timeout=None):
        if self.directory is None:
            return True
        if (self.directory / STOP_ALL).exists():
            return False  # the pair already stopped; nobody will answer another barrier
        self.number += 1
        ready = {"id": identifier, "label": label or identifier, "exit": exit}
        if error:
            ready["error"] = error
        temporary = barrier_file(self.directory, self.number, "ready.tmp")
        temporary.write_text(json.dumps(ready) + "\n", encoding="utf-8")
        os.replace(temporary, barrier_file(self.directory, self.number, "ready"))
        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is None or time.monotonic() < deadline:
            if barrier_file(self.directory, self.number, "stop").exists() or (self.directory / STOP_ALL).exists():
                return False
            if barrier_file(self.directory, self.number, "go").exists():
                return True
            time.sleep(BARRIER_POLL)
        return False


def command_barrier(args):
    return 0 if Barrier(args.barrier_dir).reach(args.id, args.label, args.exit, args.error) else STOPPED_EXIT


def flag_value(argv, flag):
    for index, part in enumerate(argv):
        if part == flag and index + 1 < len(argv):
            return argv[index + 1]
        if part.startswith(flag + "="):
            return part[len(flag) + 1:]
    return None


def patcher_step(argv):
    """(id, label) of one patcher install, the same on both sides of a pair.

    The id hashes the tslpatchdata folder's .ini files and the namespace option, not its path: each
    side extracts into its own work dir, so only the instructions identify the install."""
    tslpatchdata = flag_value(argv, "--tslpatchdata") or ""
    option = flag_value(argv, "--namespace-option-index")
    hasher = hashlib.sha256()
    folder = Path(tslpatchdata)
    # Patchers also take the mod folder that holds tslpatchdata/ (ModSync.rs passes that); hash the inner one.
    if folder.is_dir() and folder.name.casefold() != "tslpatchdata":
        inner = [path for path in folder.iterdir() if path.is_dir() and path.name.casefold() == "tslpatchdata"]
        if len(inner) == 1:
            folder = inner[0]
    if folder.is_dir():
        for ini in sorted((path for path in folder.iterdir() if path.suffix.lower() == ".ini" and path.is_file()),
                          key=lambda path: path.name.casefold()):
            hasher.update(ini.name.casefold().encode() + b"\0" + ini.read_bytes() + b"\0")
    parts = [part for part in tslpatchdata.replace("\\", "/").split("/") if part]
    label = "/".join(parts[-3:]) or "patcher"
    if option is not None:
        label += " (option %s)" % option
        hasher.update(b"option\0" + option.encode())
    return "patch-" + hasher.hexdigest()[:16], label


def command_patch(args):
    """Run the real patcher with the caller's arguments and output, then wait at the barrier."""
    argv = [args.real] + [part for part in args.args if part != "--"]
    if "--install" not in argv[1:]:
        os.execv(argv[0], argv)  # a probe (--help, --validate, …) never pauses the pair
    identifier, label = patcher_step(argv[1:])  # before the run, so nothing it writes can change the id
    if (Path(args.barrier_dir) / STOP_ALL).exists():
        print("lockstep: the paired install already stopped; not running %s" % label, file=sys.stderr, flush=True)
        return STOPPED_EXIT
    try:
        code = subprocess.call(argv)
    except OSError as error:
        print("lockstep: cannot start %s: %s" % (args.real, error), file=sys.stderr, flush=True)
        code = 127
    if not Barrier(args.barrier_dir).reach(identifier, label, code):
        print("lockstep: the paired install stopped at %s; failing this patcher run" % label, file=sys.stderr,
              flush=True)
        return code or STOPPED_EXIT
    return code


def command_wrapper(args):
    """Write an executable that ModSync can run as its patcher: `lockstep.py patch` around --real."""
    import shlex
    script = "#!/bin/sh\nexec %s %s patch --real %s --barrier-dir %s -- \"$@\"\n" % (
        shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve())),
        shlex.quote(str(Path(args.real).resolve())), shlex.quote(str(Path(args.barrier_dir).resolve())))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")
    out.chmod(0o755)
    print(out)
    return 0


def load_pair(path, names):
    commands = json.loads(Path(path).read_text(encoding="utf-8"))["commands"]
    if set(commands) != set(names):
        raise SystemExit("lockstep: --commands must have one command for each of %s" % ", ".join(names))
    for name, command in commands.items():
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise SystemExit("lockstep: %s: argv must be a non-empty list of strings" % name)
    return commands


def bind_barrier(command, directory):
    """The command with {barrier} filled in and LOCKSTEP_BARRIER_DIR set."""
    def fill(value):
        return value.replace("{barrier}", str(directory))
    bound = {"argv": [fill(part) for part in command["argv"]],
             "env": {key: fill(value) for key, value in (command.get("env") or {}).items()}}
    if command.get("cwd"):
        bound["cwd"] = fill(command["cwd"])
    bound["env"]["LOCKSTEP_BARRIER_DIR"] = str(directory)
    return bound


def command_pair(args):
    """Two long-running installers that pause at every barrier (see the barrier protocol)."""
    session = Session(args)
    commands = load_pair(args.commands, session.names)
    sides = {}
    for name in session.names:
        # Fixed under WORK so `wrapper --barrier-dir WORK/NAME/barrier` can be written before the pair starts.
        directory = session.work / name / "barrier"
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise SystemExit("lockstep: %s is not empty; give the pair a fresh --work-dir" % directory)
        sides[name] = {"barrier": directory, "log": session.work / name / "install.log"}
    session.baseline()
    if not session.running:
        return session.finish()
    for name in session.names:
        process, stream = launch(bind_barrier(commands[name], sides[name]["barrier"]), sides[name]["log"])
        sides[name].update(process=process, stream=stream)
        if process is None:
            session.stop("start", "starting %s" % name, "could not start the %s install" % name)
    number = 1
    timeout = args.first_step_timeout or args.step_timeout
    deadline = time.monotonic() + timeout
    while session.running:
        states = {}
        for name in session.names:
            ready = read_ready(sides[name]["barrier"], number)
            if ready is not None:
                states[name] = ("ready", ready)
            elif sides[name]["process"].poll() is not None:
                states[name] = ("exited", sides[name]["process"].returncode)
        if len(states) < 2:
            if time.monotonic() > deadline:
                session.stop("step-%d" % number, "step %d" % number,
                             "no barrier within %d seconds" % timeout)
                break
            time.sleep(BARRIER_POLL)
            continue
        kinds = {state[0] for state in states.values()}
        if kinds == {"ready"}:
            first, second = (states[name][1] for name in session.names)
            label = first.get("label") or first.get("id") or "step %d" % number
            entry = {"id": first.get("id") or "step-%d" % number, "label": label}
            session.report["steps"].append(entry)
            print("lockstep: barrier %d %s" % (number, label), flush=True)
            entry["results"] = {name: states[name][1] for name in session.names}
            # A side reports its own step failure in the barrier ("exit" other than 0, or an "error"); the
            # folders are still diffed so the report shows what the failing side left behind. A step that
            # failed on both sides is no disagreement between them: if the folders match, both go on and
            # the install's own failure handling (ModSync quarantine, manual.py halting) decides.
            failed = {name: states[name][1].get("error") or "exit %s" % states[name][1].get("exit")
                      for name in session.names
                      if states[name][1].get("error") or states[name][1].get("exit", 0) != 0}
            if len(failed) == len(session.names):
                entry["failed_on_both"] = failed
                failures = []
            else:
                failures = ["failed on %s (%s)" % item for item in failed.items()]
            if first.get("id") != second.get("id"):
                failures.append("the two installs reached different steps: %s and %s"
                                % (first.get("id"), second.get("id")))
            if session.checkpoint(number, entry, failures):
                for name in session.names:
                    signal_barrier(sides[name]["barrier"], number, "go")
                number += 1
                timeout = args.step_timeout
                deadline = time.monotonic() + timeout
        elif kinds == {"exited"}:
            # Both ended without another barrier; whatever they wrote after the last one is diffed too.
            entry = {"id": "end", "label": "after the last step",
                     "results": {name: {"exit": states[name][1]} for name in session.names}}
            session.report["steps"].append(entry)
            failures = ["%s exited %s" % (name, states[name][1]) for name in session.names if states[name][1] != 0]
            if number == 1:
                # Nothing was compared along the way: the wrapper or barrier was never reached.
                failures.append("no step reached a barrier")
            session.checkpoint(number, entry, failures)
            break  # nothing is left to wait for; a clean end keeps the status running for finish()
        else:
            early = next(name for name in session.names if states[name][0] == "exited")
            waiting = next(name for name in session.names if states[name][0] == "ready")
            session.report["steps"].append({"id": states[waiting][1].get("id") or "step-%d" % number,
                                            "label": states[waiting][1].get("label") or "step %d" % number,
                                            "results": {early: {"exit": states[early][1]}}})
            session.stop("step-%d" % number, states[waiting][1].get("label") or "step %d" % number,
                         "%s exited %s before this barrier, which %s reached" % (early, states[early][1], waiting))
    for name in session.names:
        (sides[name]["barrier"] / STOP_ALL).touch()
        process = sides[name].get("process")
        if process is None:
            continue
        if process.poll() is None:
            signal_barrier(sides[name]["barrier"], number, "stop")
            try:
                process.wait(timeout=STOP_GRACE)
            except subprocess.TimeoutExpired:
                pass
        stop(process)
        sides[name]["stream"].close()
        session.report.setdefault("exits", {})[name] = process.returncode
    session.write()
    return session.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub):
        sub.add_argument("--tree", action="append", required=True, help="NAME=DIR; give exactly two")
        sub.add_argument("--work-dir", required=True)
        sub.add_argument("--report", required=True)
        sub.add_argument("--evidence-dir")
        sub.add_argument("--ignore", action="append",
                         help="a path relative to both trees left out of every diff; repeatable")
        sub.add_argument("--summary")
        sub.add_argument("--step-timeout", type=int, default=DEFAULT_STEP_TIMEOUT)

    run = commands.add_parser("run", help="run each step's two commands at once and diff after every step")
    run.add_argument("--steps", required=True)
    common(run)
    pair = commands.add_parser("pair", help="run two installers that pause at every barrier and diff there")
    pair.add_argument("--commands", required=True)
    pair.add_argument("--first-step-timeout", type=int,
                      help="seconds to wait for the first barrier, which also covers each install's own setup "
                           "(default: --step-timeout)")
    common(pair)
    barrier = commands.add_parser("barrier", help="installer side: report one finished mod and wait for go/stop")
    barrier.add_argument("--id", required=True)
    barrier.add_argument("--label")
    barrier.add_argument("--exit", type=int, default=0,
                         help="the mod's own result; not 0 on one side only makes the pair stop")
    barrier.add_argument("--error")
    barrier.add_argument("--barrier-dir", help="default: LOCKSTEP_BARRIER_DIR")
    patch = commands.add_parser("patch", help="run a patcher install, then wait at the barrier with its exit code")
    patch.add_argument("--real", required=True, help="the patcher executable")
    patch.add_argument("--barrier-dir", help="default: LOCKSTEP_BARRIER_DIR")
    patch.add_argument("args", nargs=argparse.REMAINDER, help="-- then the patcher's own arguments")
    wrapper = commands.add_parser("wrapper", help="write an executable that runs `patch` around --real")
    wrapper.add_argument("--real", required=True)
    wrapper.add_argument("--barrier-dir", required=True)
    wrapper.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return {"run": command_run, "pair": command_pair, "barrier": command_barrier,
            "patch": command_patch, "wrapper": command_wrapper}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
