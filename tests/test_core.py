import tempfile
import unittest
from pathlib import Path

from ckb import core


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        core.init(self.root)
        (self.root / ".ckb" / "knowledge" / "style.md").write_text(
            "---\nkind: rule\nstatus: active\ntitle: Error rules\n"
            "summary: Wrap service errors\nscope: src/service/**\n---\n\n"
            "Always wrap database exceptions in a service error.\n", encoding="utf-8"
        )
        (self.root / "src" / "service").mkdir(parents=True)
        (self.root / "src" / "ui").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_search_and_scope(self):
        (self.root / "src" / "service" / "auth.py").write_text(
            "def authenticate():\n    # 处理登录异常\n    raise LoginError()\n", encoding="utf-8"
        )
        core.index(self.root)
        self.assertTrue(any(x.kind == "code" for x in core.search(self.root, "登录异常")))
        self.assertTrue(any(x.kind == "code" for x in core.search(self.root, "登录")))
        self.assertTrue(any(x.kind == "rule" for x in core.search(
            self.root, "service errors", scope="src/service/auth.py")))
        self.assertFalse(any(x.kind == "rule" for x in core.search(
            self.root, "service errors", scope="src/ui/home.py")))

    def test_incremental_update_and_deletion(self):
        file = self.root / "src" / "service" / "auth.py"
        file.write_text("unique_old_token\n", encoding="utf-8")
        self.assertEqual(core.index(self.root)["changed"], 2)
        self.assertEqual(core.index(self.root)["changed"], 0)
        file.write_text("unique_new_token\n", encoding="utf-8")
        core.index(self.root)
        self.assertFalse(core.search(self.root, "unique_old_token"))
        self.assertTrue(core.search(self.root, "unique_new_token"))
        file.unlink()
        self.assertEqual(core.index(self.root)["removed"], 1)
        self.assertFalse(core.search(self.root, "unique_new_token"))

    def test_context_budget_and_rule_priority(self):
        (self.root / "src" / "service" / "auth.py").write_text(
            "def authenticate():\n    # database exception\n" * 20, encoding="utf-8"
        )
        core.index(self.root)
        pack = core.context(self.root, "database exception", scope="src/service/auth.py", budget=500)
        self.assertLessEqual(len(pack), 500)
        self.assertIn("Error rules", pack)
        self.assertIn(".ckb/knowledge/style.md:", pack)

    def test_context_without_target_uses_only_global_rules(self):
        core.add_card(self.root, "rule", "Global rule", "Global guidance",
                      "Use explicit names.", status="active")
        core.index(self.root)
        pack = core.context(self.root, "service errors")
        self.assertIn("Global rule", pack)
        self.assertNotIn("Error rules", pack)
        self.assertIn("provide a target path", pack)
        service_pack = core.context(self.root, "service errors", scope="src/service/auth.py")
        self.assertIn("Error rules", service_pack)
        ui_pack = core.context(self.root, "service errors", scope="src/ui/home.py")
        self.assertNotIn("Error rules", ui_pack)

    def test_context_preserves_late_code_hit_and_cites_displayed_lines(self):
        lines = [f"# filler {i} " + "x" * 70 for i in range(60)]
        lines.extend(["def retry_login():", "    return True", "# end"])
        (self.root / "auth.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        core.index(self.root)
        pack = core.context(self.root, "retry_login", scope="auth.py", budget=500)
        self.assertLessEqual(len(pack), 500)
        self.assertIn("def retry_login():", pack)
        _, section = pack.split("Source: auth.py:", 1)
        line_range, section = section.split("\n", 1)
        start, end = map(int, line_range.split("-"))
        body = section.split("\n\n", 1)[1].removesuffix("\n[truncated]\n")
        self.assertEqual(body.splitlines(), lines[start - 1:end])
        self.assertGreater(start, 1)

    def test_many_rules_do_not_hide_matching_code(self):
        for n in range(5):
            (self.root / ".ckb" / "knowledge" / f"rule-{n}.md").write_text(
                f"---\nkind: rule\nstatus: active\ntitle: Rule {n}\n"
                "summary: General guidance\nscope: **\n---\n\nGeneral guidance.\n", encoding="utf-8"
            )
        (self.root / "src" / "service" / "auth.py").write_text(
            "def authenticate():\n    return unique_auth_result\n", encoding="utf-8"
        )
        core.index(self.root)
        pack = core.context(self.root, "unique_auth_result", scope="src/service/auth.py", budget=3000)
        self.assertIn("src/service/auth.py:", pack)
        small_pack = core.context(self.root, "unique_auth_result", scope="src/service/auth.py", budget=700)
        self.assertIn("src/service/auth.py:", small_pack)

    def test_invalid_card_is_removed_from_index_without_blocking_refresh(self):
        core.index(self.root)
        self.assertTrue(core.search(self.root, "service errors", kind="rule"))
        card = self.root / ".ckb" / "knowledge" / "style.md"
        original = card.read_text(encoding="utf-8")
        card.write_text("broken", encoding="utf-8")
        (self.root / "src" / "service" / "auth.py").write_text("def retry_login(): pass\n", encoding="utf-8")
        expected = [".ckb/knowledge/style.md: missing front matter"]
        self.assertEqual(core.index(self.root)["warnings"], expected)
        self.assertEqual(core.validate(self.root), expected)
        self.assertFalse(core.search(self.root, "service errors", kind="rule"))
        self.assertTrue(core.search(self.root, "retry_login", kind="code"))
        card.write_text(original, encoding="utf-8")
        self.assertEqual(core.index(self.root)["warnings"], [])
        self.assertTrue(core.search(self.root, "service errors", kind="rule"))


if __name__ == "__main__":
    unittest.main()
