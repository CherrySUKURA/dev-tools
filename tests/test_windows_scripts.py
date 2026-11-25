#!/usr/bin/env python3
# 前端启动/构建脚本的跨平台生成测试。
#
# 为什么单独测：
#   Windows 分支不能靠 macOS 手工点一遍验证，但生成出来的 .cmd 至少可以在这里
#   静态检查（注释必须是 rem、端口用 %PORT%、路径 cd /d）。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402


class FrontendScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.services = Path(self.tmp.name) / "services"
        self.project = Path(self.tmp.name) / "web-app"
        self.project.mkdir()
        self.saved = dashboard.SERVICES_DIR
        dashboard.SERVICES_DIR = self.services

    def tearDown(self):
        dashboard.SERVICES_DIR = self.saved
        self.tmp.cleanup()

    def test_posix_scripts(self):
        with mock.patch.object(dashboard.plat, "IS_WINDOWS", False):
            start, build = dashboard.write_frontend_scripts(
                "web-app", self.project, "pnpm", "dev", "build", 5173, "vite"
            )
        self.assertEqual(start.name, "web-app-start.sh")
        self.assertEqual(build.name, "web-app-build.sh")
        text = start.read_text(encoding="utf-8")
        self.assertIn("#!/bin/bash", text)
        self.assertIn('export PORT="${PORT:-5173}"', text)
        self.assertIn("pnpm run dev -- --port \"$PORT\"", text)
        self.assertTrue(start.stat().st_mode & 0o111)

    def test_windows_scripts(self):
        with mock.patch.object(dashboard.plat, "IS_WINDOWS", True):
            start, build = dashboard.write_frontend_scripts(
                "web-app", self.project, "pnpm", "dev", "build", 5173, "vite"
            )
        self.assertEqual(start.name, "web-app-start.cmd")
        self.assertEqual(build.name, "web-app-build.cmd")
        text = start.read_text(encoding="utf-8")
        self.assertIn("@echo off", text)
        self.assertIn("setlocal", text)
        self.assertIn('cd /d "', text)
        self.assertIn('if not defined PORT set "PORT=5173"', text)
        self.assertIn("pnpm run dev -- --port %PORT%", text)
        # cmd 不认 # 注释，生成内容里不能出现以 # 开头的行。
        for line in text.splitlines():
            self.assertFalse(line.lstrip().startswith("#"), line)
        self.assertIn("rem 项目：web-app", text)


if __name__ == "__main__":
    unittest.main()
