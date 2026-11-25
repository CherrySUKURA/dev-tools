#!/usr/bin/env python3
# 控制台核心逻辑的单元测试（只用标准库 unittest，不引入额外依赖）。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v
#
# 覆盖范围（对应 ROADMAP 的 S1）：
#   - 健康检查配置的默认值与 health.conf 覆盖
#   - TCP 探活、HTTP 探活
#   - 端口释放的安全约束（不存在的端口、未知项目）
#   - health.conf 写入与非法输入拒绝

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402


class HealthSettingsTest(unittest.TestCase):
    """health.conf 的解析与默认值合并。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf_path = Path(self.tmp.name) / "health.conf"
        self.old_conf = dashboard.HEALTH_CONF
        dashboard.HEALTH_CONF = self.conf_path
        # 清掉模块级缓存，避免上一个用例的解析结果串进来。
        dashboard.health_conf_cache["mtime"] = None
        dashboard.health_conf_cache["data"] = {}

    def tearDown(self):
        dashboard.HEALTH_CONF = self.old_conf
        dashboard.health_conf_cache["mtime"] = None
        dashboard.health_conf_cache["data"] = {}
        self.tmp.cleanup()

    def test_defaults_use_project_port(self):
        """没有 health.conf 时，默认按项目端口做 TCP 探活并开启自动重启。"""
        settings = dashboard.health_settings({"name": "svc-a", "port": 18089})
        self.assertEqual(settings["mode"], "tcp")
        self.assertEqual(settings["target"], "18089")
        self.assertTrue(settings["restart"])
        self.assertEqual(settings["max_restarts"], 5)
        self.assertEqual(settings["failures"], 3)

    def test_conf_overrides(self):
        """health.conf 里的配置优先于默认值。"""
        self.conf_path.write_text(
            "svc-a|http|http://localhost:18089/actuator/health|20|5|6|0|2\n",
            encoding="utf-8",
        )
        settings = dashboard.health_settings({"name": "svc-a", "port": 18089})
        self.assertEqual(settings["mode"], "http")
        self.assertEqual(settings["target"], "http://localhost:18089/actuator/health")
        self.assertEqual(settings["interval"], 20)
        self.assertEqual(settings["timeout"], 5)
        self.assertEqual(settings["failures"], 6)
        self.assertFalse(settings["restart"])
        self.assertEqual(settings["max_restarts"], 2)

    def test_bad_numbers_fall_back(self):
        """配置里写错数字时回退默认值，写 0 时按最小值兜底，不能抛异常把巡检线程带崩。"""
        self.conf_path.write_text("svc-a|tcp|18089|abc|xyz|0|1|zz\n", encoding="utf-8")
        settings = dashboard.health_settings({"name": "svc-a", "port": 18089})
        self.assertEqual(settings["interval"], 10)   # abc -> 默认 10 秒
        self.assertEqual(settings["failures"], 1)    # 0 -> 下限 1
        self.assertEqual(settings["max_restarts"], 5)  # zz -> 默认 5 次

    def test_off_mode(self):
        """mode=off 时探活直接返回正常，不产生误判。"""
        self.conf_path.write_text("svc-a|off||10|2|3|1|5\n", encoding="utf-8")
        settings = dashboard.health_settings({"name": "svc-a", "port": 18089})
        self.assertEqual(settings["mode"], "off")
        self.assertEqual(dashboard.health_probe(settings), (True, ""))


class ProbeTest(unittest.TestCase):
    """真实起一个临时端口来验证探活，不依赖外部服务。"""

    def test_port_open_true_for_listening_socket(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            settings = {"mode": "tcp", "target": str(port), "timeout": 1}
            ok, error = dashboard.health_probe(settings)
            self.assertTrue(ok, error)
        finally:
            server.close()

    def test_port_open_false_after_close(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        server.close()
        settings = {"mode": "tcp", "target": str(port), "timeout": 1}
        ok, error = dashboard.health_probe(settings)
        self.assertFalse(ok)
        self.assertIn(str(port), error)


class ReleasePortTest(unittest.TestCase):
    """端口释放的安全约束：前端不能凭任意端口/PID 杀进程。"""

    def test_unknown_port_rejected(self):
        result = dashboard.release_port(59997)
        self.assertFalse(result["ok"])
        self.assertIn("没有监听", result["error"])


class SaveHealthSettingsTest(unittest.TestCase):
    """health.conf 的写入、校验与格式。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf_path = Path(self.tmp.name) / "health.conf"
        self.old_conf = dashboard.HEALTH_CONF
        self.old_projects = dashboard.project_names
        dashboard.HEALTH_CONF = self.conf_path
        dashboard.project_names = lambda: {"svc-a"}
        dashboard.health_state.clear()

    def tearDown(self):
        dashboard.HEALTH_CONF = self.old_conf
        dashboard.project_names = self.old_projects
        dashboard.health_state.clear()
        self.tmp.cleanup()

    def test_save_writes_expected_columns(self):
        result = dashboard.save_health_settings({
            "project": "svc-a", "mode": "http",
            "target": "http://localhost:18089/actuator/health",
            "interval": 30, "timeout": 3, "failures": 4,
            "auto_restart": True, "max_restarts": 2,
        })
        self.assertTrue(result["ok"], result)
        line = [l for l in self.conf_path.read_text(encoding="utf-8").splitlines() if l.startswith("svc-a")][0]
        self.assertEqual(
            line,
            "svc-a|http|http://localhost:18089/actuator/health|30|3|4|1|2",
        )

    def test_save_is_idempotent_per_project(self):
        for _ in range(3):
            dashboard.save_health_settings({"project": "svc-a", "mode": "tcp", "target": "18089"})
        lines = [l for l in self.conf_path.read_text(encoding="utf-8").splitlines() if l.startswith("svc-a")]
        self.assertEqual(len(lines), 1, "同一项目重复保存应覆盖而不是追加")

    def test_unknown_project_rejected(self):
        result = dashboard.save_health_settings({"project": "nope"})
        self.assertFalse(result["ok"])

    def test_invalid_mode_rejected(self):
        result = dashboard.save_health_settings({"project": "svc-a", "mode": "ping"})
        self.assertFalse(result["ok"])

    def test_pipe_in_target_rejected(self):
        """目标里带竖线会把配置文件写坏，必须拒绝。"""
        result = dashboard.save_health_settings({"project": "svc-a", "target": "18089|x"})
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
