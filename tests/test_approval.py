"""Regressions: blocked starts must wait for a scoped human decision, never bypass it."""

import argparse
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DEVD_CONFIG", str(Path(__file__).parent / "fixtures" / "config.json"))

from devd import approval, commands, state  # noqa: E402


class ApprovalStarts(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (state, approval):
            self.stack.enter_context(patch.object(module, "DEVD_DIR", self.directory))
        for name, value in (("STATE_FILE", self.directory / "state.json"),
                            ("LOCK_FILE", self.directory / "state.lock"),
                            ("LOG_DIR", self.directory / "logs")):
            self.stack.enter_context(patch.object(state, name, value, create=True))
        self.stack.enter_context(patch.object(commands, "actor", return_value="agent"))
        self.stack.enter_context(patch.object(commands, "resolve_up_target", return_value=("app", "/repo", "/repo", "npm run dev")))
        self.stack.enter_context(patch.object(commands, "snapshot", return_value={}))
        self.stack.enter_context(patch.object(commands, "listeners", return_value={}))
        self.stack.enter_context(patch.object(commands, "refresh"))
        self.stack.enter_context(patch.object(commands, "unmanaged_units", return_value=[]))
        self.spawn = self.stack.enter_context(patch.object(commands, "spawn", side_effect=self.started))
        self.stack.enter_context(patch.object(commands, "wait_ready", side_effect=lambda rid, _: state.load_state()["servers"][rid] | {"_ready": True}))
        self.dialog = self.stack.enter_context(patch.object(approval, "native_dialog", return_value="allowed"))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.args = argparse.Namespace(target=None, name=None, cwd=None, cmd=[], raw=False, force=False, over_budget=False, wait=1)
        with state.locked_state() as data:
            data["servers"]["app@repo"] = {
                "id": "app@repo", "name": "app", "cwd": "/repo", "root": "/repo",
                "cmd": "npm run dev", "status": "stopped", "stopped_by": "user", "stopped_at": 1,
            }
            data["config"]["retain_min"] = 0

    def started(self, rec):
        rec.update(status="running", hostname="app.localhost", port=3000)

    def change(self, **fields):
        with state.locked_state() as data:
            data["servers"]["app@repo"].update(fields)

    def budget_full(self):
        with state.locked_state() as data:
            data["config"]["max_servers"] = 0

    def test_user_stopped_start_requires_allow_and_preserves_limits(self):
        # Bug: returning exit 3 before prompting, or treating approval as a global override.
        before = state.load_state()["config"]
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.dialog.call_count, 1)
        self.assertIn("stopped by user", self.dialog.call_args.args[0])
        self.assertEqual(state.load_state()["config"], before)
        self.assertEqual(state.load_state()["servers"]["app@repo"]["started_by"], "agent")

    def test_reject_keeps_server_down_and_retries_do_not_prompt_again(self):
        # Bug: a rejected start spawns, or retries flood the desktop.
        self.dialog.return_value = "rejected"
        self.assertEqual(commands.cmd_up(self.args), 3)
        self.assertEqual(commands.cmd_up(self.args), 3)
        self.assertEqual(self.dialog.call_count, 1)
        self.spawn.assert_not_called()
        self.assertEqual(state.load_state()["servers"]["app@repo"]["status"], "stopped")
        self.assertIn("rejected recently", self.output.getvalue())

    def test_denial_cooldown_expires_and_allows_a_fresh_decision(self):
        # Bug: a cached rejection permanently prevents the user from reconsidering.
        self.dialog.return_value = "rejected"
        self.assertEqual(commands.cmd_up(self.args), 3)
        self.dialog.return_value = "allowed"
        later = approval.time.time() + 601
        with patch.object(approval.time, "time", return_value=later):
            self.assertEqual(commands.cmd_up(self.args), 0)
        self.assertEqual(self.dialog.call_count, 2)
        self.assertEqual(self.spawn.call_count, 1)

    def test_busy_dialog_queue_times_out_without_spawning_or_prompting(self):
        # Bug: another stuck dialog can make an agent wait forever.
        with approval.approval_lock("dialog") as acquired:
            self.assertTrue(acquired)
            with patch.object(approval, "QUEUE_TIMEOUT", 0.01):
                self.assertEqual(commands.cmd_up(self.args), 3)
        self.dialog.assert_not_called()
        self.spawn.assert_not_called()
        self.assertIn("queue timed out", self.output.getvalue())

    def test_budget_and_killed_server_share_one_explicit_dialog(self):
        # Bug: approving a restart silently bypasses an unmentioned budget restriction.
        self.change(status="killed", ended_at=2)
        self.budget_full()
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.assertEqual(self.dialog.call_count, 1)
        message = self.dialog.call_args.args[0]
        self.assertIn("killed from outside", message)
        self.assertIn("Allow exceeding", message)
        self.assertIn("npm run dev", message)
        self.assertIn("Checkout: /repo", message)
        self.assertEqual(state.load_state()["config"]["max_servers"], 0)

    def test_new_server_over_budget_requests_approval(self):
        # Bug: new targets still return exit 4 without the native approval flow.
        with state.locked_state() as data:
            data["servers"].clear()
        self.budget_full()
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.assertEqual(self.dialog.call_count, 1)
        self.assertEqual(self.spawn.call_count, 1)

    def test_budget_rejection_returns_four_without_spawning(self):
        self.change(stopped_by="agent")
        self.budget_full()
        self.dialog.return_value = "rejected"
        self.assertEqual(commands.cmd_up(self.args), 4)
        self.spawn.assert_not_called()

    def test_timeout_and_unavailable_alert_fail_closed(self):
        # Bug: timeout/script failure gets interpreted as approval.
        for answer in ("expired", "unavailable", "queue timed out"):
            with self.subTest(answer=answer), patch.object(commands, "request_start", return_value=answer):
                self.assertEqual(commands.cmd_up(self.args), 3)
                self.assertIn(answer, self.output.getvalue())
        self.spawn.assert_not_called()

    def test_approval_is_not_reused_after_another_user_stop(self):
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.change(status="stopped", stopped_by="user", stopped_at=10)
        self.dialog.return_value = "rejected"
        self.assertEqual(commands.cmd_up(self.args), 3)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.dialog.call_count, 2)

    def test_state_lock_is_free_during_dialog_and_new_stop_invalidates_grant(self):
        # Bug: holding the state lock freezes the UI; a stale grant overrides a later stop.
        def first_dialog(_):
            self.change(stopped_at=20)  # would deadlock if cmd_up still held the state lock
            return "allowed"
        with patch.object(commands, "request_start", side_effect=lambda req: first_dialog(req) if req.key == first_key else "rejected"):
            first_key = commands._up_attempt(self.args, "agent", None).key
            self.assertEqual(commands.cmd_up(self.args), 3)
        self.spawn.assert_not_called()

    def test_user_stop_during_route_reclaim_cancels_approved_start(self):
        # Bug: reclaim finishes outside the state lock and overwrites a newer user stop.
        self.change(hostname="app.localhost")
        real_begin = commands.begin_replace
        def begin(rec, procs, who):
            real_begin(rec, procs, who)
            return {42}
        with patch.object(commands, "begin_replace", side_effect=begin), patch.object(commands, "kill_pids", side_effect=lambda _: self.change(status="stopped", stopped_by="user", stopped_at=42)):
            self.assertEqual(commands.cmd_up(self.args), 3)
        self.spawn.assert_not_called()
        self.assertEqual(state.load_state()["servers"]["app@repo"]["stopped_by"], "user")

    def test_down_restart_uses_same_approval_and_budget_checks(self):
        self.dialog.return_value = "rejected"
        self.assertEqual(commands.cmd_restart(argparse.Namespace(target="app@repo", wait=1)), 3)
        self.assertEqual(self.dialog.call_count, 1)
        self.spawn.assert_not_called()

    def test_running_server_and_ordinary_agent_stop_need_no_dialog(self):
        self.change(status="running", hostname="app.localhost")
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.spawn.assert_not_called()
        self.change(status="stopped", stopped_by="agent")
        self.assertEqual(commands.cmd_up(self.args), 0)
        self.dialog.assert_not_called()

    def test_approval_does_not_enable_user_only_flags(self):
        for flag in ("force", "over_budget"):
            with self.subTest(flag=flag):
                args = argparse.Namespace(**(vars(self.args) | {flag: True}))
                self.assertEqual(commands.cmd_up(args), 5)
        self.dialog.assert_not_called()
        self.spawn.assert_not_called()

    def test_concurrent_callers_share_one_prompt_and_start(self):
        # Bug: independent agents each show a dialog or spawn a second server.
        from concurrent.futures import ThreadPoolExecutor
        import time
        def slow_dialog(_):
            time.sleep(0.2)
            return "allowed"
        self.dialog.side_effect = slow_dialog
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: commands.cmd_up(self.args), range(2)))
        self.assertEqual(results, [0, 0])
        self.assertEqual(self.dialog.call_count, 1)
        self.assertEqual(self.spawn.call_count, 1)


class NativeDialog(unittest.TestCase):
    def test_command_text_is_an_argument_not_executable_applescript(self):
        # Bug: quotes/newlines in a command inject AppleScript into the alert.
        message = 'npm run dev "\\n"; do shell script "touch /tmp/never"\nŽluťoučký'
        with patch.object(approval.sys, "platform", "darwin"), patch.object(approval.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, 'Allow once\n')) as run:
            self.assertEqual(approval.native_dialog(message), "allowed")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], message)
        self.assertNotIn(message, argv[2])
        self.assertIn('default button "Reject"', argv[2])

    def test_timeout_and_script_error_are_not_approval(self):
        with patch.object(approval.sys, "platform", "darwin"):
            with patch.object(approval.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 125)):
                self.assertEqual(approval.native_dialog("start"), "expired")
            with patch.object(approval.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "Allow once")):
                self.assertEqual(approval.native_dialog("start"), "unavailable")
