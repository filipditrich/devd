"""`devd setup` against a throwaway HOME: wiring, idempotence, migration of an old install, and uninstall."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEVD = REPO / "bin" / "devd"
FOREIGN = "/opt/other-tool/hook.sh"


def run(home: Path, *args: str, actor: str = "user") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CURSOR_AGENT", "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX")}
    env.update(HOME=str(home), DEVD_ACTOR=actor, DEVD_CONFIG=str(home / ".config/devd/config.json"))
    return subprocess.run([str(DEVD), *args], env=env, capture_output=True, text=True, check=False)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class Setup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        h = self.home
        for d in (".cursor", ".claude", ".codex"):
            (h / d).mkdir()
        (h / ".cursor/hooks.json").write_text(json.dumps({"version": 1, "hooks": {
            "preToolUse": [{"command": FOREIGN}, {"command": "/old/.local/share/devd/guard preToolUse", "matcher": "Shell"}],
        }}))
        (h / ".claude/settings.json").write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": FOREIGN}]},
        ]}}))
        (h / ".claude/CLAUDE.md").write_text("# Mine\n\nkeep this\n")
        (h / ".codex/hooks.json").write_text(json.dumps({"hooks": {}}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_install_is_complete_and_idempotent(self) -> None:
        r = run(self.home, "setup")
        self.assertEqual(r.returncode, 0, r.stderr)
        h = self.home
        self.assertEqual((h / ".local/bin/devd").resolve(), DEVD.resolve())
        self.assertEqual((h / ".local/share/devd/guard").resolve(), (REPO / "guard/guard").resolve())
        self.assertTrue((h / ".config/devd/config.json").exists())

        cursor = read_json(h / ".cursor/hooks.json")["hooks"]
        pre = [e["command"] for e in cursor["preToolUse"]]
        self.assertEqual(pre, [f"{h}/.local/share/devd/guard preToolUse", FOREIGN])
        self.assertEqual(cursor["beforeShellExecution"][0]["command"], f"{h}/.local/share/devd/guard beforeShellExecution")
        self.assertTrue((h / ".cursor/rules/devd.mdc").is_symlink())
        self.assertTrue((h / ".cursor/skills/devd/SKILL.md").exists())

        claude = read_json(h / ".claude/settings.json")
        self.assertEqual(claude["model"], "x")
        groups = claude["hooks"]["PreToolUse"]
        self.assertEqual(groups[0]["hooks"][0]["command"], f"{h}/.local/share/devd/guard claude")
        self.assertEqual(groups[1]["hooks"][0]["command"], FOREIGN)
        md = (h / ".claude/CLAUDE.md").read_text()
        self.assertTrue(md.startswith("# Mine\n\nkeep this\n"))
        self.assertIn("devd up", md)
        self.assertTrue((h / ".claude/settings.json.devd-backup").exists())

        codex = read_json(h / ".codex/hooks.json")
        self.assertEqual(codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"], f"{h}/.local/share/devd/guard codex")
        self.assertIn("devd up", (h / ".codex/AGENTS.md").read_text())
        self.assertTrue((h / ".agents/skills/devd/SKILL.md").exists())

        snapshot = {p: p.read_text() for p in h.rglob("*") if p.is_file() and not p.is_symlink()}
        again = run(self.home, "setup")
        self.assertIn("already set up", again.stdout)
        self.assertEqual(snapshot, {p: p.read_text() for p in h.rglob("*") if p.is_file() and not p.is_symlink()})

    def test_uninstall_restores_foreign_config(self) -> None:
        run(self.home, "setup")
        r = run(self.home, "setup", "--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        h = self.home
        self.assertFalse((h / ".local/bin/devd").exists())
        self.assertFalse((h / ".cursor/rules/devd.mdc").exists())
        self.assertNotIn("devd/guard", (h / ".cursor/hooks.json").read_text())
        self.assertIn(FOREIGN, (h / ".cursor/hooks.json").read_text())
        self.assertEqual(read_json(h / ".claude/settings.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"], FOREIGN)
        self.assertEqual((h / ".claude/CLAUDE.md").read_text(), "# Mine\n\nkeep this\n")
        self.assertTrue((h / ".config/devd/config.json").exists())

    def test_dry_run_changes_nothing(self) -> None:
        before = {p: p.read_text() for p in self.home.rglob("*") if p.is_file()}
        r = run(self.home, "setup", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would", r.stdout)
        self.assertEqual(before, {p: p.read_text() for p in self.home.rglob("*") if p.is_file()})

    def test_agents_cannot_run_setup(self) -> None:
        r = run(self.home, "setup", actor="agent")
        self.assertEqual(r.returncode, 5)
        self.assertFalse((self.home / ".local/bin/devd").exists())

    def test_missing_harness_is_skipped(self) -> None:
        (self.home / ".codex/hooks.json").unlink()
        (self.home / ".codex").rmdir()
        r = run(self.home, "setup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not found, skipped", r.stdout)


if __name__ == "__main__":
    unittest.main()
