"""Unit tests for the pure parts of devd: parsing, naming, status rules, and the managed Markdown block."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# devd reads its config at import time, so point it at the fixture first
os.environ["DEVD_CONFIG"] = str(TESTS / "fixtures" / "config.json")
sys.path.insert(0, str(TESTS.parent / "src"))

from devd.install import MARK_BEGIN, MARK_END, replace_block  # noqa: E402
from devd.model import agent_may_start, classify_exit, down_reason, idle_limit_s, note_presence  # noqa: E402
from devd.naming import agent_name_error, fallback_name, package_label, record_id, rename_record  # noqa: E402
from devd.procs import Proc, route_pids  # noqa: E402
from devd.runner import build_argv  # noqa: E402
from devd.util import fmt_minutes, now, parse_minutes, parse_size_mb  # noqa: E402


class Parsing(unittest.TestCase):
    def test_minutes(self) -> None:
        self.assertEqual(parse_minutes("15"), 15)
        self.assertEqual(parse_minutes("15m"), 15)
        self.assertEqual(parse_minutes("1h"), 60)
        self.assertEqual(parse_minutes("30s"), 0.5)
        self.assertEqual(parse_minutes("off"), 0)
        with self.assertRaises(ValueError):
            parse_minutes("soon")

    def test_minutes_round_trip(self) -> None:
        for text in ("15m", "1h", "30s", "off", "90m"):
            self.assertEqual(fmt_minutes(parse_minutes(text)), text)

    def test_sizes(self) -> None:
        self.assertEqual(parse_size_mb("8G"), 8192)
        self.assertEqual(parse_size_mb("512M"), 512)
        self.assertEqual(parse_size_mb("1.5gb"), 1536)
        with self.assertRaises(ValueError):
            parse_size_mb("lots")


class Naming(unittest.TestCase):
    def test_plain_checkout_uses_repo_folder(self) -> None:
        self.assertEqual(record_id("shop", "/code/acme-shop"), "shop@acme-shop")

    def test_worktree_uses_effort_folder(self) -> None:
        self.assertEqual(record_id("shop", "/code/.worktrees/fix-cart/acme-shop"), "shop@fix-cart")

    def test_worktrees_folder_itself_is_not_an_effort(self) -> None:
        self.assertEqual(record_id("shop", "/code/.worktrees/acme-shop"), "shop@acme-shop")

    def test_unsafe_characters_are_replaced(self) -> None:
        self.assertEqual(record_id("my app", "/code/a b"), "my-app@a-b")

    def test_package_label_keeps_the_scope(self) -> None:
        self.assertEqual(package_label("@nfctron/api"), "nfctron-api")
        self.assertEqual(package_label("@nfctron/nfctron-hub"), "nfctron-hub")
        self.assertEqual(package_label("@nfctron/api-pass"), "nfctron-api-pass")
        self.assertEqual(package_label("@nfctron/webpay-api"), "nfctron-webpay-api")
        self.assertEqual(package_label("@sonde/web"), "sonde-web")
        self.assertEqual(package_label("nfctron-tickets"), "nfctron-tickets")
        self.assertEqual(package_label("ditrich.me"), "ditrich-me")

    def test_fallback_reads_the_scoped_package_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "package.json").write_text('{"name":"@nfctron/api"}')
            self.assertEqual(fallback_name(directory), "nfctron-api")

    def test_fallback_uses_the_repo_folder_when_the_package_is_unnamed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "nfctron-api")
            root.mkdir()
            (root / "package.json").write_text("{}")
            subprocess.run(["git", "init", "-q", root], check=True)
            self.assertEqual(fallback_name(str(root)), "nfctron-api")

    def test_agents_cannot_rename_a_checkout(self) -> None:
        self.assertIsNone(agent_name_error(None, "nfctron-api"))
        self.assertIsNone(agent_name_error("nfctron-api", "nfctron-api"))
        self.assertIn("nfctron-api", agent_name_error("packages-api", "nfctron-api"))

    def test_renaming_a_stopped_record_keeps_the_branch_and_frees_the_old_id(self) -> None:
        state = {"servers": {}}
        rec = {
            "id": "api@timed-entry-cart",
            "name": "api",
            "root": "/code/.worktrees/timed-entry-cart/nfctron-api",
            "status": "stopped",
            "hostname": "timed-entry-cart.api.localhost",
            "log": "/tmp/does-not-exist-devd-api.log",
        }
        state["servers"][rec["id"]] = rec
        renamed = rename_record(state, rec, "nfctron-api")
        self.assertEqual(renamed["id"], "nfctron-api@timed-entry-cart")
        self.assertEqual(renamed["hostname"], "timed-entry-cart.nfctron-api.localhost")
        self.assertNotIn("api@timed-entry-cart", state["servers"])
        self.assertIs(state["servers"]["nfctron-api@timed-entry-cart"], renamed)

    def test_a_running_record_is_not_renamed(self) -> None:
        state = {"servers": {}}
        rec = {"id": "hub@donation-settlement", "name": "hub", "root": "/r", "status": "running"}
        state["servers"][rec["id"]] = rec
        self.assertIs(rename_record(state, rec, "nfctron-hub"), rec)
        self.assertEqual(rec["id"], "hub@donation-settlement")

    def test_portless_wrapping(self) -> None:
        self.assertEqual(build_argv("shop", "bun run dev")[1:], ["run", "--name", "shop", "--", "bun", "run", "dev"])
        self.assertEqual(
            build_argv("shop", "bun run dev", force=True)[1:],
            ["run", "--name", "shop", "--force", "--", "bun", "run", "dev"],
        )
        self.assertEqual(build_argv("shop", "bun run dev", raw=True, force=True), ["bun", "run", "dev"])
        self.assertEqual(build_argv("shop", "FOO=1 bun dev && x", raw=True), ["/bin/sh", "-c", "FOO=1 bun dev && x"])

    def test_route_holder_is_part_of_the_tree_to_stop(self) -> None:
        owner = Proc(5, 1, 10, "t", "portless run --name hub")
        child = Proc(6, 5, 10, "t", "bun run dev")
        other = Proc(7, 1, 10, "t", "other")
        procs = {5: owner, 6: child, 7: other}
        routes = [
            {"hostname": "feat.hub.localhost", "pid": 5, "port": 1},
            {"hostname": "other.hub.localhost", "pid": 7, "port": 2},
            {"hostname": "feat.hub.localhost", "pid": 99, "port": 3},
        ]
        self.assertEqual(route_pids({"hostname": "feat.hub.localhost"}, procs, routes), {5, 6})
        self.assertEqual(route_pids({}, procs, routes), set())


class Lifecycle(unittest.TestCase):
    def test_agents_restart_only_what_they_may(self) -> None:
        self.assertTrue(agent_may_start({"status": "failed"}))
        self.assertTrue(agent_may_start({"status": "stopped", "stopped_by": "agent"}))
        self.assertTrue(agent_may_start({"status": "stopped", "stopped_by": "idle"}))
        self.assertFalse(agent_may_start({"status": "stopped", "stopped_by": "user"}))
        self.assertFalse(agent_may_start({"status": "killed"}))
        self.assertFalse(agent_may_start({"status": "exited"}))

    def test_exit_classification(self) -> None:
        early = {"started_at": now()}
        classify_exit(early, 1)
        self.assertEqual(early["status"], "failed")
        late = {"started_at": now() - 3600}
        classify_exit(late, 1)
        self.assertEqual(late["status"], "exited")
        signalled = {"started_at": now() - 3600}
        classify_exit(signalled, -9)
        self.assertEqual(signalled["status"], "killed")

    def test_idle_limit(self) -> None:
        cfg = {"agent_idle_min": 15, "user_idle_min": 0}
        self.assertEqual(idle_limit_s({"started_by": "agent"}, cfg), 900)
        self.assertEqual(idle_limit_s({"started_by": "user"}, cfg), 0)
        self.assertEqual(idle_limit_s({"started_by": "agent", "keep": True}, cfg), 0)

    def test_idle_reason(self) -> None:
        rec = {"status": "stopped", "stopped_by": "idle", "idle_limit_min": 15, "stopped_at": now()}
        self.assertTrue(down_reason(rec).startswith("stopped after 15m idle"))

    def test_a_live_process_is_not_left_looking_failed(self) -> None:
        rec = {"status": "failed", "port": 4610, "ended_at": 1, "exit_code": 1}
        note_presence(rec, alive=True, has_route=False)
        self.assertEqual(rec["status"], "running")
        self.assertNotIn("exit_code", rec)
        stopped = {"status": "stopped", "stopped_by": "user"}
        note_presence(stopped, alive=True, has_route=True)
        self.assertEqual(stopped["status"], "stopped")
        gone = {"status": "running", "started_at": now()}
        note_presence(gone, alive=False, has_route=False)
        self.assertEqual(gone["status"], "killed")


class ManagedBlock(unittest.TestCase):
    def test_append_replace_remove(self) -> None:
        original = "# Mine\n\nkeep this\n"
        added = replace_block(original, "rules v1")
        self.assertIn("keep this", added)
        self.assertEqual(added.count(MARK_BEGIN), 1)
        replaced = replace_block(added, "rules v2")
        self.assertNotIn("rules v1", replaced)
        self.assertEqual(replaced.count(MARK_END), 1)
        self.assertEqual(replace_block(replaced, "rules v2"), replaced)
        self.assertEqual(replace_block(replaced, None), original)

    def test_block_in_the_middle_keeps_both_sides(self) -> None:
        text = f"top\n\n{MARK_BEGIN}\nold\n{MARK_END}\n\nbottom\n"
        self.assertEqual(replace_block(text, None), "top\n\nbottom\n")


if __name__ == "__main__":
    unittest.main()
