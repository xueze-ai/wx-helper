"""更新包选择、版本比较和本地解压；不访问网络。"""
import tempfile
import unittest
import zipfile
from pathlib import Path

from wxbot_framework.update import (choose_asset, is_newer, parse_version, stage,
                                     write_install_script)


class UpdateTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertTrue(is_newer("v1.2.1", "1.2.0"))
        self.assertFalse(is_newer("v1.2.0", "1.2.0"))

    def test_asset_preference(self):
        asset = choose_asset([
            {"name": "source.zip", "url": "https://example/source.zip", "size": 1},
            {"name": "wx-helper-windows.zip", "url": "https://example/win.zip", "size": 2},
            {"name": "wx-helper.exe", "url": "https://example/a.exe", "size": 3},
        ])
        self.assertEqual(asset["name"], "wx-helper-windows.zip")
        self.assertIsNone(choose_asset([
            {"name": "notes.txt", "url": "https://example/notes.txt", "size": 0},
        ]))

    def test_stage_zip_and_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "wx-helper-windows.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("wx-helper/wx-helper.exe", b"fake exe")
                archive.writestr("wx-helper/_internal/data.txt", "ok")
                archive.writestr("wx-helper/bot_config.json", "{}")
            stage_root, exe = stage(package, root / "stage")
            self.assertEqual(exe.name, "wx-helper.exe")
            self.assertTrue((stage_root / "wx-helper.exe").exists())
            script = write_install_script(root / "install.ps1")
            self.assertIn("WaitPid", script.read_text(encoding="utf-8-sig"))
            self.assertIn("bot_config.json", script.read_text(encoding="utf-8-sig"))

    def test_stage_rejects_non_exe_zip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "bad.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("readme.txt", "no exe")
            with self.assertRaises(Exception):
                stage(package, root / "stage")


if __name__ == "__main__":
    unittest.main()
