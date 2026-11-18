"""Tests for bin/cellar, against a fake restic, fake RCON commands and a fake systemctl on PATH.

Run with: python3 -m unittest discover -s tests
Nothing here needs root or touches /etc/cellar, /var/lib/cellar or a real repository.
"""
import argparse
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

# restic stand-in: logs its arguments (and which repository and environment it got) and answers from
# plan.json, keyed by subcommand; "local:<cmd>" overrides it for the rest: repository (the local copy).
FAKE_RESTIC = r'''#!/usr/bin/env python3
import json, os, shutil, sys
d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
repo = os.environ.get("RESTIC_REPOSITORY", "")
with open(os.path.join(d, "restic.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
with open(os.path.join(d, "env.jsonl"), "a") as f:
    f.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith(("RESTIC_", "AWS_"))}) + "\n")
rest = list(args)
while rest and rest[0].startswith("--"):
    if rest.pop(0) == "--retry-lock":
        rest.pop(0)
cmd = rest[0] if rest else ""
with open(os.path.join(d, "plan.json")) as f:
    plan = json.load(f)
step = plan.get(cmd, {})
if repo.startswith("rest:"):
    step = plan.get("local:" + cmd, step)
if cmd == "restore" and "src" in step:
    shutil.copytree(step["src"], rest[rest.index("--target") + 1], dirs_exist_ok=True)
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("code", 0))
'''

# RCON stand-in: logs "name arg" and fails when rcon.json says so. "term" also sends SIGTERM to cellar.
FAKE_RCON = r'''#!/usr/bin/env python3
import json, os, signal, sys
d = os.environ["FAKE_DIR"]
name = os.path.basename(sys.argv[0])
call = f"{name} {' '.join(sys.argv[1:])}"
with open(os.path.join(d, "rcon.log"), "a") as f:
    f.write(call + "\n")
with open(os.path.join(d, "rcon.json")) as f:
    action = json.load(f).get(call)
if action == "term":
    os.kill(os.getppid(), signal.SIGTERM)
if action == "fail":
    sys.exit(f"{call}: connection refused")
print(f"{call}: done")
'''

SUMMARY = json.dumps({"message_type": "summary", "snapshot_id": "abc123", "data_added": 10,
                      "data_added_packed": 5, "total_bytes_processed": 100, "files_new": 1,
                      "files_changed": 2, "total_files_processed": 3})


def load_cellar():
    loader = importlib.machinery.SourceFileLoader("cellar", str(ROOT / "bin" / "cellar"))
    spec = importlib.util.spec_from_loader("cellar", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write_exe(path, text):
    path.write_text(text)
    path.chmod(0o755)


def make_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table t (x)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()


class CellarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cellar-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        c = self.c = load_cellar()
        etc, state = self.tmp / "etc", self.tmp / "state"
        etc.mkdir()
        state.mkdir()
        c.ETC, c.STATE = str(etc), str(state)
        c.STATUS, c.STATUS_LOCK, c.LOCK = f"{state}/status.json", f"{state}/status.lock", f"{state}/lock"
        c.STAGING, c.WORK, c.TMP, c.CACHE = f"{state}/staging", f"{state}/work", f"{state}/tmp", str(self.tmp / "cache")
        c.CONF = {"HC_URL": "https://hc.invalid/nightly"}
        (etc / "b2.env").write_text("RESTIC_REPOSITORY=s3:https://s3.example.invalid/bucket\n")
        (etc / "restic.pass").write_text("test\n")
        (etc / "sources.txt").write_text(f"{self.tmp}\n")

        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        write_exe(bin_dir / "restic", FAKE_RESTIC)
        write_exe(bin_dir / "systemctl", "#!/bin/sh\nexit 0\n")
        for name in ("mcA", "mcB"):
            write_exe(bin_dir / name, FAKE_RCON)
        env = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DIR": str(self.fake)})
        env.start()
        self.addCleanup(env.stop)
        self.plan({})
        self.rcon_plan({})

        self.pings = []
        urlopen = mock.patch.object(c.urllib.request, "urlopen",
                                    lambda req, timeout: self.pings.append(req.full_url))
        urlopen.start()
        self.addCleanup(urlopen.stop)
        self.logs = []
        c.log = self.logs.append
        sleep = mock.patch.object(c.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def plan(self, steps):
        base = {"backup": {"stdout": SUMMARY + "\n"}, "snapshots": {"stdout": '[{"short_id": "abc12345"}]'},
                "stats": {"stdout": '{"total_size": 42}'},
                "ls": {"stdout": json.dumps({"type": "file", "path": f"{self.tmp}/x", "size": 7}) + "\n"}}
        base.update(steps)
        (self.fake / "plan.json").write_text(json.dumps(base))

    def rcon_plan(self, actions):
        (self.fake / "rcon.json").write_text(json.dumps(actions))

    def restic_calls(self):
        path = self.fake / "restic.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def calls_for(self, cmd, repo=None):
        """Calls containing cmd; repo="b2" or "local" keeps only those made against that repository."""
        calls = zip(self.restic_calls(), self.restic_envs())
        return [a for a, e in calls if cmd in a and
                (repo is None or e.get("RESTIC_REPOSITORY", "").startswith("rest:") == (repo == "local"))]

    def restic_envs(self):
        path = self.fake / "env.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def rcon_calls(self):
        path = self.fake / "rcon.log"
        return path.read_text().splitlines() if path.exists() else []

    def status(self):
        return json.loads(Path(self.c.STATUS).read_text())

    def backup(self, tag):
        self.c.cmd_backup(argparse.Namespace(tag=tag))


class Backup(CellarTest):
    def test_nightly_groups_by_host_and_tag_and_bounds_prune(self):
        self.backup("nightly")
        backup = self.calls_for("backup")[0]
        self.assertIn("--group-by", backup)
        self.assertEqual(backup[backup.index("--group-by") + 1], "host")
        frequent_forget, nightly_forget = self.calls_for("forget")
        self.assertEqual(frequent_forget[frequent_forget.index("--group-by") + 1], "host,tags")
        self.assertIn("--keep-within", frequent_forget)
        self.assertEqual(nightly_forget[nightly_forget.index("--group-by") + 1], "host,tags")
        self.assertIn("--prune", nightly_forget)
        self.assertEqual(nightly_forget[nightly_forget.index("--max-repack-size") + 1], "300M")

        s = self.status()
        self.assertEqual(s["backup"]["result"], "ok")
        self.assertEqual(s["backup"]["snapshot"], "abc123")
        self.assertTrue(s["last_success"])
        self.assertEqual(s["repo"]["size_bytes"], 42)
        self.assertEqual(s["sources"], [{"path": str(self.tmp), "bytes": 7}])
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/start", "https://hc.invalid/nightly"])

    def test_prune_limit_can_be_turned_off(self):
        self.c.CONF["PRUNE_MAX_REPACK"] = ""
        self.backup("nightly")
        self.assertNotIn("--max-repack-size", self.calls_for("forget")[1])

    def test_frequent_has_its_own_section_and_skips_prune_and_stats(self):
        self.c.status_merge(backup={"result": "failed", "error": "nightly broke"}, last_success="then")
        self.backup("frequent")
        s = self.status()
        self.assertEqual(s["backup"], {"result": "failed", "error": "nightly broke"})
        self.assertEqual(s["last_success"], "then")
        self.assertEqual(s["frequent"]["result"], "ok")
        self.assertTrue(s["frequent"]["last_success"])
        self.assertEqual(len(self.calls_for("forget")), 1)
        self.assertEqual(self.calls_for("stats"), [])
        self.assertEqual(self.pings, [], "a frequent success must not ping the nightly check")

    def test_frequent_failure_reaches_hc_url_as_fail(self):
        self.plan({"backup": {"code": 1, "stderr": "Fatal: unable to open repository"}})
        with self.assertRaises(self.c.Failed):
            self.backup("frequent")
        self.assertEqual(self.status()["frequent"]["result"], "failed")
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/fail"])

    def test_frequent_uses_its_own_url_when_set(self):
        self.c.CONF["HC_URL_FREQUENT"] = "https://hc.invalid/frequent"
        self.backup("frequent")
        self.assertEqual(self.pings, ["https://hc.invalid/frequent/start", "https://hc.invalid/frequent"])

    def test_frequent_failure_keeps_previous_last_success(self):
        self.backup("frequent")
        first = self.status()["frequent"]["last_success"]
        self.plan({"backup": {"code": 1, "stderr": "boom"}})
        with self.assertRaises(self.c.Failed):
            self.backup("frequent")
        self.assertEqual(self.status()["frequent"]["last_success"], first)

    def test_frequent_skips_when_another_job_runs(self):
        with self.c.locked():
            self.backup("frequent")
        self.assertEqual(self.restic_calls(), [])
        self.assertFalse(Path(self.c.STATUS).exists())

    def test_manual_runs_dont_ping(self):
        self.backup("pre-change")
        self.assertEqual(self.status()["backup"]["tag"], "pre-change")
        self.assertEqual(self.pings, [])

    def test_failed_forget_keeps_the_snapshot(self):
        self.plan({"forget": {"code": 1, "stderr": "Fatal: storage cap exceeded"}})
        self.backup("nightly")
        s = self.status()
        self.assertEqual(s["backup"]["result"], "partial")
        self.assertEqual(s["backup"]["snapshot"], "abc123")
        self.assertIn("retention or stats failed", s["backup"]["error"])
        self.assertTrue(s["last_success"])
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly/fail")

    def test_exit_code_3_is_partial_with_a_success_ping(self):
        self.plan({"backup": {"code": 3, "stdout": SUMMARY + "\n"}})
        self.backup("nightly")
        s = self.status()["backup"]
        self.assertEqual(s["result"], "partial")
        self.assertIn("could not be read", s["error"])
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly")

    def test_bad_json_lines_are_skipped(self):
        self.plan({"backup": {"stdout": "{not json\n" + SUMMARY + "\n"}})
        self.backup("nightly")
        self.assertEqual(self.status()["backup"]["snapshot"], "abc123")

    def test_restic_temp_files_stay_out_of_tmp(self):
        self.assertEqual(self.c.restic_env()["TMPDIR"], self.c.TMP)
        self.assertTrue(os.path.isdir(self.c.TMP))


class Minecraft(CellarTest):
    def setUp(self):
        super().setUp()
        self.c.CONF["MINECRAFT"] = "a:mcA b:mcB"

    def test_pauses_and_resumes_every_server(self):
        self.backup("nightly")
        self.assertEqual(self.rcon_calls(), ["mcA save-off", "mcA save-all flush", "mcB save-off",
                                             "mcB save-all flush", "mcA save-on", "mcB save-on"])
        self.assertEqual(self.status()["backup"]["result"], "ok")

    def test_server_that_cant_pause_is_backed_up_anyway(self):
        self.rcon_plan({"mcA save-off": "fail"})
        self.backup("nightly")
        s = self.status()["backup"]
        self.assertEqual(s["result"], "partial")
        self.assertIn("a: couldn't pause auto-save", s["error"])
        self.assertEqual(len(self.calls_for("backup")), 1)
        self.assertNotIn("mcA save-on", self.rcon_calls())
        self.assertIn("mcB save-on", self.rcon_calls())
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly")

    def test_failed_save_on_is_retried_then_alerts(self):
        self.rcon_plan({"mcA save-on": "fail"})
        self.backup("nightly")
        self.assertEqual(self.rcon_calls().count("mcA save-on"), 2)
        s = self.status()["backup"]
        self.assertEqual(s["result"], "partial")
        self.assertIn("auto-save is still OFF", s["error"])
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly/fail")

    def test_sigterm_during_save_on_still_resumes_the_rest(self):
        old = signal.signal(signal.SIGTERM, self.c.on_sigterm)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        self.rcon_plan({"mcA save-on": "term"})
        with self.assertRaises(self.c.Stopped):
            self.backup("nightly")
        self.assertIn("mcB save-on", self.rcon_calls())
        s = self.status()["backup"]
        self.assertEqual(s["result"], "partial")
        self.assertIn("SIGTERM", s["error"])

    def test_malformed_entry_fails_clearly(self):
        self.c.CONF["MINECRAFT"] = "justaunit"
        with self.assertRaisesRegex(self.c.Failed, "isn't unit:command"):
            self.backup("nightly")


class PreBackup(CellarTest):
    def test_runs_before_restic_reads_the_sources(self):
        marker = self.tmp / "pulled"
        self.c.CONF["PRE_BACKUP"] = f"sh -c 'echo pulled; touch {marker}'"
        self.backup("nightly")
        self.assertTrue(marker.exists())
        self.assertIn("PRE_BACKUP: pulled", self.logs)
        self.assertEqual(self.status()["backup"]["result"], "ok")
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly")

    def test_failure_alerts_but_the_backup_still_runs(self):
        self.c.CONF["PRE_BACKUP"] = "sh -c 'echo h5 unreachable >&2; exit 2'"
        self.backup("frequent")
        self.assertEqual(len(self.calls_for("backup")), 1)
        s = self.status()["frequent"]
        self.assertEqual(s["result"], "partial")
        self.assertIn("PRE_BACKUP failed", s["error"])
        self.assertIn("h5 unreachable", s["error"])
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly/fail")

    def test_missing_command_alerts(self):
        self.c.CONF["PRE_BACKUP"] = str(self.tmp / "no-such-script")
        self.backup("nightly")
        self.assertIn("PRE_BACKUP failed", self.status()["backup"]["error"])
        self.assertEqual(len(self.calls_for("backup")), 1)


class Status(CellarTest):
    def test_concurrent_merges_lose_nothing(self):
        ctx = multiprocessing.get_context("fork")

        def writer(i):
            for n in range(60):
                self.c.status_merge(**{f"job{i}": n})

        procs = [ctx.Process(target=writer, args=(i,)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        self.assertEqual([p.exitcode for p in procs], [0, 0, 0, 0])
        s = self.status()
        self.assertEqual([s.get(f"job{i}") for i in range(4)], [59, 59, 59, 59])
        self.assertEqual([f for f in os.listdir(self.c.STATE) if f.startswith(".status-")], [])

    def test_unreadable_file_is_kept_aside(self):
        Path(self.c.STATUS).write_text("{")
        self.c.status_merge(check={"result": "ok"})
        self.assertEqual(Path(self.c.STATUS + ".bad").read_text(), "{")
        self.assertEqual(self.status()["check"], {"result": "ok"})

    def test_status_command_reports_corrupt_file(self):
        Path(self.c.STATUS).write_text("{")
        with self.assertRaises(SystemExit) as e:
            self.c.cmd_status(argparse.Namespace(json=False))
        self.assertIn("can't read", str(e.exception.code))

    def test_running_without_the_lock_shows_interrupted(self):
        self.c.status_merge(check={"result": "running", "started": "2026-11-08T12:35:21+00:00"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.c.cmd_status(argparse.Namespace(json=False))
        self.assertRegex(out.getvalue(), r"check\s+interrupted")
        out = io.StringIO()
        with self.c.locked(), contextlib.redirect_stdout(out):
            self.c.cmd_status(argparse.Namespace(json=False))
        self.assertRegex(out.getvalue(), r"check\s+running")


class StoppedCommand(CellarTest):
    def stopped(self, job, result):
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": result, "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"}):
            self.c.cmd_stopped(argparse.Namespace(job=job))

    def test_killed_run_is_marked_failed(self):
        self.c.status_merge(check={"result": "running", "error": None})
        Path(self.c.WORK).mkdir()
        self.stopped("check", "oom-kill")
        s = self.status()["check"]
        self.assertEqual(s["result"], "failed")
        self.assertIn("oom-kill", s["error"])
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/fail"])
        self.assertFalse(Path(self.c.WORK).exists())

    def test_finished_run_is_left_alone(self):
        self.c.status_merge(backup={"result": "ok"})
        self.stopped("nightly", "exit-code")
        self.assertEqual(self.status()["backup"], {"result": "ok"})
        self.stopped("nightly", "success")
        self.assertEqual(self.pings, [])

    def test_sigterm_message_is_readable(self):
        with self.assertRaisesRegex(self.c.Stopped, "SIGTERM"):
            self.c.on_sigterm(signal.SIGTERM, None)


class CheckAndDrill(CellarTest):
    def test_check_reads_this_weeks_slice(self):
        self.c.cmd_check(argparse.Namespace(subset=None))
        check = self.calls_for("check")[0]
        self.assertIn(f"--read-data-subset={self.c.weekly_subset()}", check)
        s = self.status()["check"]
        self.assertEqual((s["result"], s["subset"]), ("ok", self.c.weekly_subset()))

    def test_check_failure_pings(self):
        self.plan({"check": {"code": 1, "stderr": "Fatal: repository contains errors"}})
        with self.assertRaises(self.c.Failed):
            self.c.cmd_check(argparse.Namespace(subset="1/52"))
        self.assertEqual(self.status()["check"]["result"], "failed")
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/fail"])

    def test_weekly_subset_covers_every_week(self):
        self.assertEqual(self.c.weekly_subset(datetime.date(2026, 1, 1)), "1/52")
        self.assertEqual(self.c.weekly_subset(datetime.date(2026, 12, 24)), "52/52")
        self.assertEqual(self.c.weekly_subset(datetime.date(2026, 12, 31)), "1/52")  # ISO week 53

    def test_drill_matches_hidden_directories(self):
        src = self.tmp / "restore-src"
        make_db(src / "home/u/.app/data.db")
        self.plan({"restore": {"src": str(src)}})
        Path(self.c.ETC, "drill.txt").write_text("/home/u/*/data.db\n")
        self.c.cmd_drill(argparse.Namespace())
        s = self.status()["drill"]
        self.assertEqual(s["result"], "ok")
        self.assertEqual([(f["path"], f["integrity"]) for f in s["files"]], [("/home/u/.app/data.db", "ok")])

    def test_drill_with_no_snapshots_fails_clearly(self):
        self.plan({"snapshots": {"stdout": "[]"}})
        Path(self.c.ETC, "drill.txt").write_text("/x\n")
        with self.assertRaisesRegex(self.c.Failed, "no snapshots"):
            self.c.cmd_drill(argparse.Namespace())

    def test_drill_removes_leftovers_of_a_killed_drill(self):
        old = Path(self.c.STATE, "drill-old")
        old.mkdir()
        self.plan({"snapshots": {"stdout": "[]"}})
        Path(self.c.ETC, "drill.txt").write_text("/x\n")
        with self.assertRaises(self.c.Failed):
            self.c.cmd_drill(argparse.Namespace())
        self.assertFalse(old.exists())


class Config(CellarTest):
    def test_read_env(self):
        path = self.tmp / "x.env"
        path.write_text('MINECRAFT=minecraft:mc   # main server\n'
                        'HC_URL="https://hc.invalid/x"  # ping\n'
                        "export B=1\n"
                        "C='a\"b'\n"
                        "D=https://x.invalid/#frag\n"
                        "# comment\n"
                        "E=\n")
        self.assertEqual(self.c.read_env(str(path)), {
            "MINECRAFT": "minecraft:mc", "HC_URL": "https://hc.invalid/x", "B": "1", "C": 'a"b',
            "D": "https://x.invalid/#frag", "E": ""})


class Staging(CellarTest):
    def setUp(self):
        super().setUp()
        # runuser needs root; run the copy as ourselves instead.
        real_run = self.c.subprocess.run

        def run(cmd, *a, **kw):
            if cmd[:1] == ["runuser"]:
                cmd = cmd[cmd.index("--") + 1:]
            return real_run(cmd, *a, **kw)

        patcher = mock.patch.object(self.c.subprocess, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.busy, self.small = self.tmp / "live/busy.db", self.tmp / "live/small.db"
        make_db(self.busy)
        make_db(self.small)
        Path(self.c.ETC, "sqlite.txt").write_text(f"nightly {self.busy}\n{self.small}\n")

    def staged(self, path):
        return Path(self.c.STAGING + str(path))

    def test_frequent_keeps_the_nightly_copy(self):
        self.c.stage()
        before = self.staged(self.busy).stat().st_ino
        sqlite3.connect(self.busy).execute("insert into t values (2)").connection.commit()
        self.c.stage(frequent=True)
        self.assertEqual(self.staged(self.busy).stat().st_ino, before)
        self.c.stage()
        rows = sqlite3.connect(self.staged(self.busy)).execute("select count(*) from t").fetchone()[0]
        self.assertEqual(rows, 2)

    def test_frequent_makes_a_first_copy_and_always_copies_unmarked_databases(self):
        self.c.stage(frequent=True)
        self.assertTrue(self.staged(self.busy).is_file())
        sqlite3.connect(self.small).execute("insert into t values (2)").connection.commit()
        self.c.stage(frequent=True)
        rows = sqlite3.connect(self.staged(self.small)).execute("select count(*) from t").fetchone()[0]
        self.assertEqual(rows, 2)
        self.assertEqual(self.staged(self.small).stat().st_mode & 0o777, 0o600)


class SqliteCopy(CellarTest):
    def copy(self, src, dst):
        return subprocess.run([sys.executable, "-c", self.c.SQLITE_COPY, str(src), str(dst)],
                              text=True, capture_output=True)

    def test_path_with_hash_and_question_mark(self):
        src = self.tmp / "odd#name?.db"
        make_db(src)
        p = self.copy(src, self.tmp / "copy.db")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sqlite3.connect(self.tmp / "copy.db").execute("select x from t").fetchall(), [(1,)])
        self.assertFalse((self.tmp / "odd").exists(), "must not open a truncated path")

    def test_empty_database_is_rejected(self):
        src = self.tmp / "empty.db"
        sqlite3.connect(src).close()
        p = self.copy(src, self.tmp / "copy.db")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no tables", p.stderr)

    def test_staged_copy_never_follows_a_symlink(self):
        target = self.tmp / "victim"
        target.write_text("keep")
        link = self.tmp / "copy.db"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            self.c.copy_regular_file(str(link), str(self.tmp / "dst.db"))
        self.assertEqual(target.read_text(), "keep")


class LocalCopy(CellarTest):
    """The optional second repository: an append-only rest-server on another machine."""

    def setUp(self):
        super().setUp()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.addCleanup(self.server.close)
        self.use_port(self.server.getsockname()[1])
        Path(self.c.ETC, "local.pass").write_text("local\n")

    def use_port(self, port):
        Path(self.c.ETC, "local.env").write_text(f"RESTIC_REPOSITORY=rest:http://127.0.0.1:{port}/box/\n"
                                                 "RESTIC_REST_USERNAME=box\nRESTIC_REST_PASSWORD=secret\n")

    def server_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.use_port(port)

    def test_nightly_backs_up_both_and_never_prunes_the_local_copy(self):
        self.backup("nightly")
        self.assertEqual(len(self.calls_for("backup", "b2")), 1)
        local = self.calls_for("backup", "local")
        self.assertEqual(len(local), 1)
        self.assertEqual(self.calls_for("forget", "local"), [])
        self.assertEqual(len(self.calls_for("forget", "b2")), 2)
        self.assertEqual(self.calls_for("ls", "local"), [])
        envs = [e for e in self.restic_envs() if e["RESTIC_REPOSITORY"].startswith("rest:")]
        self.assertTrue(all(e["RESTIC_PASSWORD_FILE"].endswith("/local.pass") for e in envs))
        self.assertTrue(all("AWS_ACCESS_KEY_ID" not in e for e in envs))

        s = self.status()
        self.assertEqual(s["backup"]["result"], "ok")
        self.assertEqual(s["local"]["backup"]["result"], "ok")
        self.assertEqual(s["local"]["backup"]["snapshot"], "abc123")
        self.assertTrue(s["local"]["last_success"])
        self.assertEqual(s["local"]["repo"]["size_bytes"], 42)
        self.assertNotIn("sources", s["local"])
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/start", "https://hc.invalid/nightly"])

    def test_minecraft_is_paused_once_for_both(self):
        self.c.CONF["MINECRAFT"] = "a:mcA"
        self.backup("nightly")
        self.assertEqual(self.rcon_calls(), ["mcA save-off", "mcA save-all flush", "mcA save-on"])
        self.assertEqual(len(self.calls_for("backup")), 2)

    def test_unreachable_local_copy_leaves_the_off_site_backup_ok(self):
        self.server_down()
        with self.assertRaisesRegex(self.c.Failed, "local copy: .*isn't reachable"):
            self.backup("nightly")
        self.assertEqual(self.calls_for("backup", "local"), [])
        s = self.status()
        self.assertEqual(s["backup"]["result"], "ok")
        self.assertTrue(s["last_success"])
        self.assertEqual(s["local"]["backup"]["result"], "failed")
        self.assertNotIn("last_success", s["local"])
        # The nightly's own success ping, then the local copy's failure (no HC_URL_LOCAL set).
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/start", "https://hc.invalid/nightly",
                                      "https://hc.invalid/nightly/fail"])

    def test_local_copy_failure_has_its_own_check_when_set(self):
        self.c.CONF["HC_URL_LOCAL"] = "https://hc.invalid/local"
        self.plan({"local:backup": {"code": 1, "stderr": "Fatal: unable to open config file"}})
        with self.assertRaises(self.c.Failed):
            self.backup("nightly")
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/start", "https://hc.invalid/local/start",
                                      "https://hc.invalid/nightly", "https://hc.invalid/local/fail"])

    def test_local_nightly_success_pings_its_own_check(self):
        self.c.CONF["HC_URL_LOCAL"] = "https://hc.invalid/local"
        self.backup("nightly")
        self.assertIn("https://hc.invalid/local", self.pings)
        self.backup("frequent")
        self.assertEqual(self.pings.count("https://hc.invalid/local"), 1, "frequent successes don't ping")

    def test_frequent_local_failure_without_its_own_check_stays_quiet(self):
        self.server_down()
        with self.assertRaises(self.c.Failed):
            self.backup("frequent")
        s = self.status()
        self.assertEqual(s["frequent"]["result"], "ok")
        self.assertEqual(s["local"]["frequent"]["result"], "failed")
        self.assertEqual(self.pings, [])

    def test_off_site_failure_still_makes_a_local_snapshot(self):
        self.plan({"backup": {"code": 1, "stderr": "Fatal: storage cap exceeded"},
                   "local:backup": {"stdout": SUMMARY + "\n"}})
        with self.assertRaisesRegex(self.c.Failed, "off-site: restic --json: Fatal: storage cap exceeded"):
            self.backup("nightly")
        s = self.status()
        self.assertEqual(s["backup"]["result"], "failed")
        self.assertNotIn("last_success", s)
        self.assertEqual(s["local"]["backup"]["result"], "ok")
        self.assertTrue(s["local"]["last_success"])
        self.assertEqual(self.pings, ["https://hc.invalid/nightly/start", "https://hc.invalid/nightly/fail"])

    def test_overdue_prune_on_the_backup_host_is_flagged(self):
        snaps = [{"short_id": "a", "time": "2026-11-10T04:30:00Z", "tags": ["frequent"]},
                 {"short_id": "b", "time": "2026-11-18T04:00:00Z", "tags": ["frequent"]},
                 {"short_id": "c", "time": "2026-11-01T04:30:00Z", "tags": ["nightly"]}]
        self.plan({"local:snapshots": {"stdout": json.dumps(snaps)}})
        self.backup("nightly")
        s = self.status()
        self.assertEqual(s["backup"]["result"], "ok")
        self.assertEqual(s["local"]["backup"]["result"], "partial")
        self.assertIn("hasn't pruned", s["local"]["backup"]["error"])
        self.assertEqual(s["local"]["repo"]["snapshots"], 3)
        self.assertEqual(self.pings[-1], "https://hc.invalid/nightly/fail")

    def test_existing_status_keys_keep_their_shape(self):
        self.backup("nightly")
        s = self.status()
        self.assertEqual(set(s) - {"local"}, {"schema", "updated", "last_success", "backup", "repo", "sources"})
        self.assertEqual(s["schema"], 1)

    def test_only_flag_picks_one_repository(self):
        self.c.cmd_backup(argparse.Namespace(tag="manual", only="local"))
        self.assertEqual(len(self.calls_for("backup", "local")), 1)
        self.assertEqual(self.calls_for("backup", "b2"), [])
        self.assertNotIn("backup", self.status())

    def test_local_flag_without_a_local_copy_fails_clearly(self):
        Path(self.c.ETC, "local.env").unlink()
        with self.assertRaisesRegex(self.c.Failed, "there's no local copy"):
            self.c.cmd_snapshots(argparse.Namespace(only="local"))
        self.backup("nightly")
        self.assertNotIn("local", self.status())

    def test_check_reads_all_of_the_local_copy_and_reports_each(self):
        self.plan({"check": {"code": 1, "stderr": "Fatal: repository contains errors"},
                   "local:check": {"code": 0}})
        with self.assertRaisesRegex(self.c.Failed, "off-site"):
            self.c.cmd_check(argparse.Namespace(subset=None))
        self.assertIn("--read-data", self.calls_for("check", "local")[0])
        self.assertIn(f"--read-data-subset={self.c.weekly_subset()}", self.calls_for("check", "b2")[0])
        s = self.status()
        self.assertEqual(s["check"]["result"], "failed")
        self.assertEqual((s["local"]["check"]["result"], s["local"]["check"]["subset"]), ("ok", "all"))

    def test_local_drill_restores_everything_and_checks_staged_databases(self):
        src = self.tmp / "restore-src"
        make_db(src / "home/u/app.db")
        make_db(Path(str(src) + self.c.STAGING) / "home/u/other.sqlite")
        (src / "etc").mkdir()
        (src / "etc/hosts").write_text("127.0.0.1 localhost\n")
        files = [p for p in src.rglob("*") if p.is_file()]
        summary = {"total_files_processed": len(files), "total_bytes_processed": sum(p.stat().st_size for p in files)}
        self.plan({"restore": {"src": str(src)},
                   "local:snapshots": {"stdout": json.dumps([{"short_id": "l1", "summary": summary}])}})
        Path(self.c.ETC, "drill.txt").write_text("/home/u/app.db\n")
        self.c.cmd_drill(argparse.Namespace())
        self.assertIn("--include", self.calls_for("restore", "b2")[0])
        self.assertNotIn("--include", self.calls_for("restore", "local")[0])
        s = self.status()
        self.assertEqual(s["drill"]["result"], "ok")
        self.assertFalse(s["drill"]["full"])
        d = s["local"]["drill"]
        self.assertEqual((d["result"], d["snapshot"], d["full"]), ("ok", "l1", True))
        self.assertEqual(d["restored"], {"files": summary["total_files_processed"],
                                         "bytes": summary["total_bytes_processed"]})
        self.assertEqual([f["path"] for f in d["files"]],
                         ["/home/u/app.db", self.c.STAGING + "/home/u/other.sqlite"])

    def test_local_drill_fails_when_files_are_missing(self):
        src = self.tmp / "restore-src"
        make_db(src / "home/u/app.db")
        summary = {"total_files_processed": 5, "total_bytes_processed": 100}
        self.plan({"restore": {"src": str(src)},
                   "local:snapshots": {"stdout": json.dumps([{"short_id": "l1", "summary": summary}])}})
        Path(self.c.ETC, "drill.txt").write_text("/home/u/app.db\n")
        with self.assertRaisesRegex(self.c.Failed, "local copy: restored 1 files"):
            self.c.cmd_drill(argparse.Namespace())
        self.assertEqual(self.status()["drill"]["result"], "ok")

    def test_killed_run_marks_the_local_section_failed_too(self):
        self.c.status_merge(backup={"result": "running"})
        self.c.status_merge(under="local", backup={"result": "running"})
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "timeout", "EXIT_CODE": "killed", "EXIT_STATUS": "TERM"}):
            self.c.cmd_stopped(argparse.Namespace(job="nightly"))
        s = self.status()
        self.assertEqual((s["backup"]["result"], s["local"]["backup"]["result"]), ("failed", "failed"))

    def test_init_copies_the_off_site_chunker_params(self):
        self.plan({"local:cat": {"code": 10, "stderr": "Fatal: repository does not exist"}})
        Path(self.c.ETC, "b2.env").write_text("RESTIC_REPOSITORY=s3:https://s3.example.invalid/bucket\n"
                                              "AWS_ACCESS_KEY_ID=key\n")
        self.c.cmd_init(argparse.Namespace(only="local"))
        init = self.calls_for("init")[0]
        self.assertIn("--copy-chunker-params", init)
        env = self.restic_envs()[-1]
        self.assertTrue(env["RESTIC_REPOSITORY"].startswith("rest:"))
        self.assertEqual(env["RESTIC_FROM_REPOSITORY"], "s3:https://s3.example.invalid/bucket")
        self.assertTrue(env["RESTIC_FROM_PASSWORD_FILE"].endswith("/restic.pass"))
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "key")

    def test_status_command_shows_the_local_copy(self):
        self.backup("nightly")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.c.cmd_status(argparse.Namespace(json=False))
        self.assertRegex(out.getvalue(), r"local copy:\n  backup\s+ok")


if __name__ == "__main__":
    unittest.main()
