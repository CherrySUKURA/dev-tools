#!/usr/bin/env python3
# devctl.py（Python 核心）的单元测试与假项目集成测试。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v
#
# 为什么要这些测试：
#   S3 把 1100 行 bash 迁到 Python，最怕“看起来一样、细节变了”。
#   这里只测纯逻辑 + 一个临时假项目，不碰本机真实服务与真实配置，
#   保证换机器 / CI 上也能跑。
#
# 覆盖范围：
#   - projects.conf 解析（13 列、注释、缺列）
#   - 项目名简称解析、all/core 目标展开
#   - 启动 / 停止顺序
#   - ports.conf 两种分隔符（空白 / 竖线）
#   - 端口探测优先级：ports.conf > application.yml > 8080
#   - jar 自动发现与排除 -sources/-javadoc/.original
#   - install 目标推导（配置 / @npm / 自动识别 -api 模块）
#   - json 输出字段与类型（port 必须是数字，running 依赖监听端口集合）
#   - action 标记文件、recent_file、源码变更扫描
#   - 假项目 start/stop 全流程（自定义脚本 + 随机端口）

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import devctl  # noqa: E402


def free_port():
    """占一个端口再释放，拿到当前几乎必然空闲的端口号（避免撞真实服务）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ConfTestBase(unittest.TestCase):
    """把 devctl 的配置路径整体指到临时目录，测试之间互不影响。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects_conf = self.root / "projects.conf"
        self.ports_conf = self.root / "ports.conf"
        self.state = self.root / "state"
        self.state.mkdir()

        self.saved = {
            "PROJECTS_CONF": devctl.PROJECTS_CONF,
            "PORTS_CONF": devctl.PORTS_CONF,
            "STATE": devctl.STATE,
            "BASE_DIR": devctl.BASE_DIR,
        }
        devctl.PROJECTS_CONF = self.projects_conf
        devctl.PORTS_CONF = self.ports_conf
        devctl.STATE = self.state
        devctl.BASE_DIR = self.root

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(devctl, key, value)
        self.tmp.cleanup()

    def write_projects(self, rows):
        """rows 是「列值列表」；统一按 13 列写出，缺的补空。"""
        lines = []
        for row in rows:
            padded = list(row) + [""] * (len(devctl.CONF_FIELDS) - len(row))
            lines.append("|".join(padded))
        self.projects_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def make_project(self, name, columns=None):
        """建一个临时项目目录，返回路径；columns 可覆盖默认配置列。"""
        directory = self.root / name
        directory.mkdir()
        row = [name, str(directory), "0"] + (columns or [])
        self.write_projects([row] + getattr(self, "_extra_rows", []))
        return directory


class ProjectsConfTest(ConfTestBase):
    def test_parses_fields_and_skips_bad_rows(self):
        directory = self.root / "svc-a"
        directory.mkdir()
        self.projects_conf.write_text(
            "# 注释\n"
            "\n"
            f" svc-a | {directory} | 1 | app.jar | start.sh | stop.sh | build.sh | doc.html "
            "| @npm | frontend | grp | svc-b,svc-c | redis\n"
            "只有名字\n"
            f"|{directory}|1\n",
            encoding="utf-8",
        )
        projects = devctl.read_projects()
        self.assertEqual(len(projects), 1)
        project = projects[0]
        self.assertEqual(project["name"], "svc-a")
        self.assertEqual(project["core"], "1")
        self.assertEqual(project["jar"], "app.jar")
        self.assertEqual(project["type"], "frontend")
        self.assertEqual(project["depends"], "svc-b,svc-c")

    def test_missing_conf_uses_directory_scan(self):
        svc = self.root / "svc"
        svc.mkdir()
        (svc / "svc-x").mkdir()
        (svc / "not-a-dir.txt").write_text("x", encoding="utf-8")
        self.assertEqual(devctl.all_names(), ["svc-x"])


class ResolveTest(ConfTestBase):
    def setUp(self):
        super().setUp()
        self.dir_a = self.make_project("svc-auth")

    def test_full_and_short_name(self):
        self.assertEqual(devctl.resolve_name("svc-auth"), "svc-auth")
        self.assertEqual(devctl.resolve_name("auth"), "svc-auth")
        self.assertIsNone(devctl.resolve_name("missing"))

    def test_gateway_alias(self):
        gateway = self.root / "txlc-gateway"
        gateway.mkdir()
        self.write_projects([["txlc-gateway", str(gateway), "1"]])
        self.assertEqual(devctl.resolve_name("gateway"), "txlc-gateway")

    def test_resolve_targets(self):
        self.write_projects([
            ["svc-auth", str(self.dir_a), "1"],
            ["svc-b", str(self.root / "svc-b"), "0"],
        ])
        self.assertEqual(devctl.resolve_targets("all"), ["svc-auth", "svc-b"])
        self.assertEqual(devctl.resolve_targets("core"), ["svc-auth"])
        self.assertEqual(devctl.resolve_targets("b"), ["svc-b"])
        self.assertIsNone(devctl.resolve_targets("nope"))

    def test_core_falls_back_to_default_when_unflagged(self):
        # 配置存在但没有任何 core=1 时，回退到默认核心项目，避免「启动核心」变成空操作。
        self.write_projects([["svc-auth", str(self.dir_a), "0"]])
        self.assertEqual(devctl.core_names(), list(devctl.DEFAULT_CORE))

    def test_order_for_start(self):
        core_a, core_b, other = "svc-auth", "svc-baseinfo", "svc-order"
        gateway = "txlc-gateway"
        self.write_projects([
            [core_a, str(self.root / core_a), "1"],
            [core_b, str(self.root / core_b), "1"],
            [other, str(self.root / other), "0"],
            [gateway, str(self.root / gateway), "1"],
        ])
        # 核心（gateway 除外）→ 其他 → gateway；不在目标里的不补。
        self.assertEqual(
            devctl.order_for_start([other, gateway, core_b]),
            [core_b, other, gateway],
        )
        self.assertEqual(devctl.order_for_start([other]), [other])


class PortTest(ConfTestBase):
    def test_override_accepts_space_and_pipe(self):
        self.ports_conf.write_text(
            "# 注释\n"
            "svc-a 9001\n"
            "svc-b|9002\n"
            "svc-c\t9003\n",
            encoding="utf-8",
        )
        self.assertEqual(devctl.port_override("svc-a"), "9001")
        self.assertEqual(devctl.port_override("svc-b"), "9002")
        self.assertEqual(devctl.port_override("svc-c"), "9003")
        self.assertIsNone(devctl.port_override("svc-d"))

    def test_override_wins_over_yml(self):
        directory = self.make_project("svc-a")
        resources = directory / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text("server:\n  port: 7001\n", encoding="utf-8")
        self.ports_conf.write_text("svc-a 9100\n", encoding="utf-8")
        self.assertEqual(devctl.port_number("svc-a"), 9100)

    def test_yml_fallback_and_default(self):
        directory = self.make_project("svc-a")
        resources = directory / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text("server:\n  port: 7001\n", encoding="utf-8")
        self.assertEqual(devctl.project_port("svc-a"), "7001")
        # 构建产物里的配置不参与探测。
        (directory / "target").mkdir()
        (directory / "target" / "application.yml").write_text("server:\n  port: 7002\n", encoding="utf-8")
        self.assertEqual(devctl.project_port("svc-a"), "7001")
        empty = self.make_project("svc-b")
        self.assertEqual(devctl.project_port("svc-b"), "8080")
        self.assertEqual(devctl.port_number("svc-b"), 8080)


class JarTest(ConfTestBase):
    def test_configured_jar_wins(self):
        directory = self.make_project("svc-a", ["app/target/app.jar"])
        jar = directory / "app" / "target" / "app.jar"
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"x")
        self.assertEqual(devctl.project_jar("svc-a"), jar)

    def test_server_target_preferred_and_exclusions(self):
        directory = self.make_project("svc-a")
        server = directory / "app-server" / "target"
        server.mkdir(parents=True)
        (server / "app.jar").write_bytes(b"x")
        (server / "app-sources.jar").write_bytes(b"x")
        (server / "app.jar.original").write_bytes(b"x")
        self.assertEqual(devctl.project_jar("svc-a"), server / "app.jar")

    def test_target_fallback(self):
        directory = self.make_project("svc-a")
        target = directory / "target"
        target.mkdir()
        (target / "b.jar").write_bytes(b"x")
        self.assertEqual(devctl.project_jar("svc-a"), target / "b.jar")


class InstallTest(ConfTestBase):
    def test_configured_spec(self):
        self.make_project("svc-a", ["", "", "", "", "", "svc-a-api"])
        self.assertEqual(devctl.install_modules_of("svc-a"), "svc-a-api")

    def test_npm_marker(self):
        self.make_project("web-a", ["", "", "", "", "", "@pnpm"])
        self.assertEqual(devctl.install_modules_of("web-a"), "@pnpm")

    def test_detect_api_modules(self):
        directory = self.make_project("svc-a")
        (directory / "pom.xml").write_text("<project/>", encoding="utf-8")
        module = directory / "svc-a-api"
        module.mkdir()
        (module / "pom.xml").write_text(
            "<project><artifactId>svc-a-api</artifactId></project>", encoding="utf-8"
        )
        core = directory / "svc-a-server"
        core.mkdir()
        (core / "pom.xml").write_text(
            "<project><artifactId>svc-a-server</artifactId></project>", encoding="utf-8"
        )
        self.assertEqual(devctl.detect_api_modules(directory), "svc-a-api")

    def test_non_maven_returns_empty(self):
        self.make_project("web-a")
        self.assertEqual(devctl.install_modules_of("web-a"), "")


class JsonStatusTest(ConfTestBase):
    def test_shape_and_types(self):
        directory = self.make_project(
            "svc-a", ["", "", "", "", "", "", "frontend", "group-a", "svc-b,svc-c", "redis"]
        )
        self.ports_conf.write_text("svc-a 8123\n", encoding="utf-8")

        def fake_ports():
            return [{"port": 8123, "pid": 1, "process": "java", "address": "127.0.0.1"}]

        buffer = io.StringIO()
        with mock.patch.object(devctl.plat, "listening_ports", side_effect=fake_ports), \
             mock.patch.object(devctl, "pid_alive", return_value=False), \
             mock.patch.object(devctl, "watch_pid_value", return_value=None), \
             mock.patch.object(devctl, "action_state", return_value=("", False, "", 0)), \
             contextlib.redirect_stdout(buffer):
            devctl.json_status()

        data = json.loads(buffer.getvalue())
        self.assertEqual(len(data), 1)
        project = data[0]
        # port 必须是数字：旧 bash 版输出数字，前端按数字比较端口。
        self.assertIsInstance(project["port"], int)
        self.assertEqual(project["port"], 8123)
        self.assertTrue(project["running"])
        self.assertEqual(project["depends"], ["svc-b", "svc-c"])
        self.assertEqual(project["requires"], ["redis"])
        self.assertEqual(project["group"], "group-a")
        self.assertEqual(project["doc"], "http://localhost:8123/")
        for key in ("name", "pid", "watch", "core", "log", "dir", "action", "action_running",
                    "action_exit", "action_exit_time", "install", "type", "start", "stop",
                    "build"):
            self.assertIn(key, project)

    def test_running_by_pid_file(self):
        self.make_project("svc-a")
        buffer = io.StringIO()
        with mock.patch.object(devctl, "pid_alive", return_value=True), \
             mock.patch.object(devctl, "read_pid", return_value=4321), \
             mock.patch.object(devctl, "watch_pid_value", return_value=None), \
             mock.patch.object(devctl, "action_state", return_value=("", False, "", 0)), \
             mock.patch.object(devctl.plat, "listening_ports", return_value=[]), \
             contextlib.redirect_stdout(buffer):
            devctl.json_status()
        project = json.loads(buffer.getvalue())[0]
        self.assertTrue(project["running"])
        self.assertEqual(project["pid"], "4321")


class MarkerAndScanTest(ConfTestBase):
    def test_recent_file(self):
        fresh = self.root / "fresh"
        fresh.write_text("x", encoding="utf-8")
        old = self.root / "old"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        self.assertTrue(devctl.recent_file(fresh, 600))
        self.assertFalse(devctl.recent_file(old, 600))
        self.assertFalse(devctl.recent_file(self.root / "missing", 600))

    def test_action_state_flags(self):
        self.make_project("svc-a")
        devctl.action_running_file("svc-a").write_text("start", encoding="utf-8")
        devctl.action_exit_file("svc-a").write_text("0", encoding="utf-8")
        action, running, exit_code, exit_time = devctl.action_state("svc-a")
        self.assertEqual(action, "start")
        self.assertTrue(running)
        self.assertEqual(exit_code, "0")
        self.assertGreater(exit_time, 0)

    def test_run_action_marker_cleans_up(self):
        self.make_project("svc-a")
        with mock.patch.object(devctl, "do_start", return_value=0):
            self.assertEqual(devctl.run_action_marker("svc-a", "start"), 0)
        self.assertFalse(devctl.action_running_file("svc-a").exists())
        self.assertEqual(
            devctl.action_exit_file("svc-a").read_text(encoding="utf-8"), "0"
        )

    def test_scan_changes_skips_build_dirs(self):
        project = self.root / "svc-a"
        src = project / "src"
        src.mkdir(parents=True)
        (project / "target").mkdir()
        (project / "node_modules").mkdir()
        fresh = src / "A.java"
        fresh.write_text("class A {}", encoding="utf-8")
        ignored = project / "target" / "B.java"
        ignored.write_text("class B {}", encoding="utf-8")
        stamp = time.time() - 60
        changed = devctl.scan_changes([project], stamp)
        self.assertEqual(changed, [fresh])
        self.assertEqual(devctl.scan_changes([project], time.time() + 60), [])


class FakeProjectIntegrationTest(ConfTestBase):
    """用临时目录起一个真实进程，验证 start/stop 全流程；不碰本机任何服务。"""

    def test_start_and_stop_with_custom_scripts(self):
        port = free_port()
        directory = self.root / "fake-svc"
        directory.mkdir()
        # 启动脚本以前台方式跑 Python HTTP server；devctl 会把它 detach 到后台。
        start = directory / "start.sh"
        start.write_text(
            "#!/bin/bash\n"
            "exec \"$PYTHON_BIN\" -m http.server \"$PORT\" --bind 127.0.0.1\n",
            encoding="utf-8",
        )
        stop = directory / "stop.sh"
        stop.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        start.chmod(0o755)
        stop.chmod(0o755)

        self.write_projects([[
            "fake-svc", str(directory), "0", "", "start.sh", "stop.sh",
        ]])
        self.ports_conf.write_text(f"fake-svc {port}\n", encoding="utf-8")

        # 子进程用当前解释器的绝对路径，避免测试环境 PATH 不完整。
        old_env = os.environ.get("PYTHON_BIN")
        os.environ["PYTHON_BIN"] = sys.executable

        self.addCleanup(lambda: os.environ.pop("PYTHON_BIN", None) if old_env is None
                        else os.environ.__setitem__("PYTHON_BIN", old_env))
        self.addCleanup(lambda: devctl.stop_one("fake-svc"))

        self.assertEqual(devctl.do_start("fake-svc"), 0)
        self.assertTrue(devctl.port_listening("fake-svc"))
        self.assertTrue(devctl.pid_file("fake-svc").is_file())

        self.assertEqual(devctl.do_stop("fake-svc"), 0)
        self.assertFalse(devctl.port_listening("fake-svc"))
        self.assertFalse(devctl.pid_file("fake-svc").exists())


if __name__ == "__main__":
    unittest.main()
