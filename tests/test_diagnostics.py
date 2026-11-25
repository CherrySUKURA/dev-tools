#!/usr/bin/env python3
# 诊断日志功能的单元测试（只用标准库）。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v
#
# 覆盖范围：
#   - 日志清单只登记真实存在的文件，控制台日志含轮转文件
#   - 诊断 id 是路径哈希，且只认识本次清单里出现过的 id（不能读任意文件）
#   - 尾随读取最多读 512KB，不会把几十 MB 的日志整份读进内存
#   - 诊断包包含 meta.txt 与各日志末尾内容，且绝不包含 *.conf

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402


class DiagnosticsTest(unittest.TestCase):
    """诊断清单、id 白名单、尾随读取与打包。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.job_dir = self.root / "jobs"
        self.job_dir.mkdir()

        # 控制台主日志 + 一个轮转文件，验证两者都会出现在清单里。
        (self.log_dir / "console.log").write_text("main log line\n" * 50, encoding="utf-8")
        (self.log_dir / "console.log.1").write_text("old log line\n" * 20, encoding="utf-8")

        # 任务日志：写入元数据，确认标签能被补全成「动作 · 目标」。
        (self.job_dir / "abc123.log").write_text("job output\n", encoding="utf-8")
        (self.job_dir / "abc123.json").write_text(
            json.dumps({"action": "update", "target": "svc-demo"}), encoding="utf-8"
        )

        # 项目相关日志：服务输出、启停操作、健康巡检、Spring 应用日志。
        self.project_log = self.root / "svc-demo.log"
        self.project_log.write_text("project output\n", encoding="utf-8")
        self.app_log = self.root / "app.log"
        self.app_log.write_text("app log\n", encoding="utf-8")
        self.health_log = self.root / "svc-demo.health.log"
        self.health_log.write_text("health log\n", encoding="utf-8")

        # 一份配置：绝不能进诊断包（可能含内网地址与本机路径）。
        self.conf = self.root / "projects.conf"
        self.conf.write_text("svc-demo|/secret/path|core\n", encoding="utf-8")

        self.patches = [
            mock.patch.object(dashboard, "LOG_DIR", self.log_dir),
            mock.patch.object(dashboard, "JOB_DIR", self.job_dir),
            mock.patch.object(dashboard, "health_log_path",
                              lambda name, kind: self.health_log),
            mock.patch.object(dashboard, "get_projects", lambda: [{
                "name": "svc-demo",
                "log": str(self.project_log),
                "log_sources": {"app": [{"label": "应用日志", "path": str(self.app_log)}]},
            }]),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_entries_only_existing_files(self):
        """清单只包含真实存在的文件，缺失的启停日志不会占位。"""
        entries = dashboard.diagnostics_entries()
        names = {entry["name"] for entry in entries}
        self.assertIn("console.log", names)
        self.assertIn("console.log.1", names)  # 轮转文件
        self.assertIn("abc123.log", names)
        self.assertIn("svc-demo.log", names)
        self.assertIn("app.log", names)
        self.assertIn("svc-demo.health.log", names)
        # 不存在的 <项目>.action.log 不应出现。
        self.assertNotIn("svc-demo.action.log", names)

    def test_entries_grouped_and_labeled(self):
        """分组覆盖控制台/任务/项目/健康/应用，任务标签带动作与目标。"""
        entries = dashboard.diagnostics_entries()
        groups = {entry["group"] for entry in entries}
        self.assertEqual(groups, {"console", "job", "project", "health", "app"})
        job_labels = [e["label"] for e in entries if e["group"] == "job"]
        self.assertIn("update · svc-demo", job_labels)

    def test_startup_entry_maps_to_project_log_source(self):
        """启动日志条目带 source/kind，前端据此跳到项目日志弹窗的同一来源。

        两处查看器（诊断面板 / 项目日志）必须能互相定位到同一个文件，
        否则同一项目会出现「两个地方看到的日志不一样」。
        """
        entries = dashboard.diagnostics_entries()
        startup = next(e for e in entries if e["label"] == "启动日志")
        self.assertEqual(startup["project"], "svc-demo")
        self.assertEqual(startup["source"], "startup")
        self.assertEqual(startup["kind"], "service")
        self.assertEqual(startup["path"], str(self.project_log))

        app = next(e for e in entries if e["label"] == "应用日志")
        self.assertEqual(app["source"], "app")
        self.assertEqual(app["kind"], "service")

    def test_id_is_path_hash_and_whitelisted(self):
        """id 是路径哈希；只有本次清单登记过的 id 才能还原成路径。"""
        entries = dashboard.diagnostics_entries()
        console = next(e for e in entries if e["label"] == "控制台主日志")
        self.assertEqual(console["id"], dashboard.diag_id(self.log_dir / "console.log"))
        self.assertEqual(
            dashboard.resolve_diag_entry(console["id"]), self.log_dir / "console.log"
        )
        # 未登记的 id 必须拒绝，避免接口被用来读任意文件。
        self.assertIsNone(dashboard.resolve_diag_entry("deadbeef"))
        self.assertIsNone(dashboard.resolve_diag_entry(str(self.conf)))

    def test_entries_hides_conf_files(self):
        """配置文件即使放在日志目录里也不会被登记。"""
        (self.log_dir / "projects.conf").write_text("secret|/path\n", encoding="utf-8")
        entries = dashboard.diagnostics_entries()
        self.assertNotIn("projects.conf", {entry["name"] for entry in entries})

    def test_read_tail_bytes_caps_large_file(self):
        """大日志只取末尾 LOG_TAIL_BYTES，避免整份读入内存。"""
        big = self.root / "big.log"
        big.write_bytes(b"x" * (dashboard.LOG_TAIL_BYTES + 4096))
        data = dashboard.read_tail_bytes(big)
        self.assertEqual(len(data), dashboard.LOG_TAIL_BYTES)

    def test_read_tail_bytes_keeps_small_file(self):
        """小文件原样返回，不做截断。"""
        self.assertEqual(
            dashboard.read_tail_bytes(self.app_log), b"app log\n"
        )

    def test_build_zip_contains_logs_and_meta_without_conf(self):
        """诊断包含 meta.txt 与各日志末尾内容，且不含任何 *.conf。"""
        entries = dashboard.diagnostics_entries()
        payload = dashboard.build_diagnostics_zip(entries)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("meta.txt", names)
            meta = archive.read("meta.txt").decode("utf-8")
            self.assertIn("TXLC 服务控制台诊断包", meta)
            self.assertIn("不包含 projects.conf", meta)
            # 每个日志一条：console 2 + job 1 + project 1 + health 1 + app 1
            self.assertEqual(len([n for n in names if n != "meta.txt"]), 6)
            self.assertIn("console/console/console.log", names)
            self.assertIn("project/svc-demo/svc-demo.log", names)
            self.assertTrue(all(not n.endswith(".conf") for n in names))
            self.assertEqual(
                archive.read("project/svc-demo/svc-demo.log"), b"project output\n"
            )

    def test_build_zip_caps_each_file(self):
        """每个日志在包里最多 512KB，避免诊断包被大日志撑爆。"""
        big = self.root / "big-app.log"
        big.write_bytes(b"y" * (dashboard.LOG_TAIL_BYTES * 2))
        entries = dashboard.diagnostics_entries()
        # 手动补一条大文件，模拟线上几十 MB 的日志。
        entries.append({
            "id": "bigapp01",
            "group": "app",
            "label": "大日志",
            "project": "svc-demo",
            "name": "big-app.log",
            "size": big.stat().st_size,
            "mtime": int(big.stat().st_mtime),
        })
        dashboard.DIAG_INDEX["bigapp01"] = big
        payload = dashboard.build_diagnostics_zip(entries)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(
                len(archive.read("app/svc-demo/big-app.log")), dashboard.LOG_TAIL_BYTES
            )


if __name__ == "__main__":
    unittest.main()
