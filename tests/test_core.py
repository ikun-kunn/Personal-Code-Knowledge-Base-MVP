import tempfile
import unittest
from pathlib import Path

from ckb import core


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / ".tmp"
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
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

    def test_invalid_card_is_rejected_without_losing_existing_index(self):
        core.index(self.root)
        before = core.search(self.root, "service errors")
        (self.root / ".ckb" / "knowledge" / "style.md").write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            core.index(self.root)
        self.assertEqual(before, core.search(self.root, "service errors"))
        self.assertTrue(core.validate(self.root))


if __name__ == "__main__":
    unittest.main()
