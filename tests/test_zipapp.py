"""Verify the deliverable works with only the archive in an unrelated directory."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parent.parent


class ZipappTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="hyprland-migrator-portable-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.archive = cls.root / "hyprland-migrator.pyz"
        built = subprocess.run(
            [sys.executable, str(ROOT / "scripts/build-zipapp.py"), "-o", str(cls.archive)],
            capture_output=True, text=True, cwd=cls.root,
        )
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)

    def run_archive(self, *args):
        # -I excludes project paths, user packages, and PYTHONPATH.
        return subprocess.run(
            [sys.executable, "-I", str(self.archive), *map(str, args)],
            capture_output=True, text=True, cwd=self.root,
        )

    def test_archive_contents(self):
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {
                "__main__.py", "hyprland_migrator/", "hyprland_migrator/__init__.py",
                "hyprland_migrator/__main__.py", "hyprland_migrator/cli.py",
                "hyprland_migrator/converter.py", "hyprland_migrator/options.json",
            })
            self.assertIn("general.border_size", json.loads(archive.read("hyprland_migrator/options.json")))
        self.assertTrue(os.access(self.archive, os.X_OK))

    def test_help_and_version(self):
        for flag in ("--help", "--version"):
            with self.subTest(flag=flag):
                result = self.run_archive(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout)

    def test_single_file_conversion_and_source_resolution(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            work = Path(directory)
            (work / "parts").mkdir()
            (work / "parts/input.conf").write_text("input {\n    kb_layout = us\n}\n")
            source = work / "hyprland.conf"
            source.write_text("$mod = SUPER\nsource = parts/*.conf\ngeneral:border_size = 3\nbind = $mod,Q,killactive\nenvd = BACKENDS,wayland,x11\ngesture = 3, horizontal, workspace\n")
            output = work / "hyprland.lua"
            report = work / "report.json"
            result = self.run_archive(source, "-o", output, "--report", report)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('hl.bind("SUPER + Q", hl.dsp.window.close())', output.read_text())
            self.assertIn('["input.kb_layout"] = "us"', output.read_text())
            self.assertIn('hl.env("BACKENDS", "wayland,x11", true)', output.read_text())
            self.assertIn('hl.gesture(', output.read_text())
            self.assertIn('["action"] = "workspace"', output.read_text())
            self.assertEqual(json.loads(report.read_text())["errors"], 0)
            if shutil.which("luac"):
                checked = subprocess.run(["luac", "-p", str(output)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_conversion_failure_keeps_exit_status(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            source = Path(directory) / "bad.conf"
            source.write_text("unknown_option = 1\n")
            result = self.run_archive(source)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("unknown option", result.stderr)
            result = self.run_archive(Path(directory) / "missing.conf")
            self.assertEqual(result.returncode, 2, result.stderr)

    @unittest.skipUnless(os.name == "posix", "direct shebang execution needs POSIX")
    def test_direct_execution_after_renaming(self):
        standalone = self.root / "hyprland-migrator"
        shutil.copy2(self.archive, standalone)
        result = subprocess.run([str(standalone), "--version"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout)


if __name__ == "__main__":
    unittest.main()
