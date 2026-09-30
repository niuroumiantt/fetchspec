"""仓库架构页生成器(scripts/reporg.py):每条子命令归到一段、每家厂商都在、目标行按类别计数、--check 能发现过期。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reporg", ROOT / "scripts" / "reporg.py")
reporg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporg)


class ReporgTest(unittest.TestCase):
    def test_every_pipeline_command_belongs_to_a_stage(self):
        commands = reporg.parser_commands(ROOT / "src" / "fetchspec" / "pipeline.py")
        staged = {c for names in reporg.STAGES.values() for c in names}
        self.assertTrue(commands, "no subcommands parsed")
        self.assertEqual(set(commands) - staged, set())

    def test_page_names_every_profile_and_command(self):
        html = reporg.build(ROOT, None)
        for path in (ROOT / "profiles").glob("*.json"):
            self.assertIn(json.loads(path.read_text())["company_en"], html)
        for name in reporg.parser_commands(ROOT / "src" / "fetchspec" / "pipeline.py"):
            self.assertIn(f"<code>{name}</code>", html)
        for title in ("定位", "需求", "通道与来源", "环节", "交付", "实时计数", "还没做完的"):
            self.assertIn(f"<h2>{title}</h2>", html)
        self.assertIn("没有给 <code>--upstream</code>", html)

    def test_targets_are_counted_by_kind_from_the_upstream_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            up = Path(tmp)
            (up / "framework").mkdir()
            rows = [{"id": "P.ups.spec", "team": "fetchspec"}, {"id": "P.ups.operation", "team": "fetchspec"},
                    {"id": "P.pdu.spec", "team": "fetchspec"}, {"id": "F.capital.spec", "team": "fetchspec"},
                    {"id": "P.gpu.news", "team": "inews"}]
            (up / "framework" / "tco_targets.json").write_text(json.dumps({"rows": rows}))
            t = reporg.load_targets(up)
            self.assertEqual(t["rows"], 4)
            self.assertEqual(t["kinds"], {"构成 P.*.spec": 2, "运行 P.*.operation": 1, "因子 F.*": 1})
            self.assertEqual(t["parts"], 2)
            self.assertEqual(t["teams"], {"fetchspec": 4, "inews": 1})
            html = reporg.build(ROOT, up)
            self.assertIn("领 <b>4</b> 行", html)

    def test_check_reports_a_stale_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reporg.html"
            self.assertEqual(reporg.main(["--output", str(out)]), 0)
            self.assertEqual(reporg.main(["--output", str(out), "--check"]), 0)
            out.write_text("stale")
            self.assertEqual(reporg.main(["--output", str(out), "--check"]), 1)


if __name__ == "__main__":
    unittest.main()
