"""Unit tests for the pure parts of devd: parsing, naming, status rules, and the managed Markdown block."""

import os
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# devd reads its config at import time, so point it at the fixture first
os.environ["DEVD_CONFIG"] = str(TESTS / "fixtures" / "config.json")
sys.path.insert(0, str(TESTS.parent / "src"))

from devd.install import MARK_BEGIN, MARK_END, replace_block  # noqa: E402
from devd.model import agent_may_start, classify_exit, down_reason, idle_limit_s  # noqa: E402
from devd.naming import record_id  # noqa: E402
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

    def test_portless_wrapping(self) -> None:
        self.assertEqual(build_argv("shop", "bun run dev")[1:], ["run", "--name", "shop", "--", "bun", "run", "dev"])
        self.assertEqual(build_argv("shop", "bun run dev", raw=True), ["bun", "run", "dev"])
        self.assertEqual(build_argv("shop", "FOO=1 bun dev && x", raw=True), ["/bin/sh", "-c", "FOO=1 bun dev && x"])


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
