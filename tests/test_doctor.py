#!/usr/bin/env python3
# doctor.py（环境与配置自检）的单元测试。
#
# 为什么这些测试重要：
#   doctor 的全部价值是「在启动之前指出哪里会失败」。如果它自己误报/漏报，
#   用户就会重新回到「启动 -> 等超时 -> 翻日志」的老路。
#   这里用临时目录模拟各种坏配置，不启动真实服务，也不读取本机真实 projects.conf。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v

import contextlib
import io
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import devctl  # noqa: E402
import doctor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class DoctorTestBase(unittest.TestCase):
    """把 devctl 的配置路径整体指到临时目录，并让端口/进程探测保持离线。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects_conf = self.root / "projects.conf"
        self.ports_conf = self.root / "ports.conf"
        self.settings = self.root / "maven-settings.xml"
        self.state = self.root / "state"
        self.state.mkdir()

        self.saved = {
            "PROJECTS_CONF": devctl.PROJECTS_CONF,
            "PORTS_CONF": devctl.PORTS_CONF,
            "MAVEN_SETTINGS": devctl.MAVEN_SETTINGS,
            "BASE_DIR": devctl.BASE_DIR,
            "STATE": devctl.STATE,
            "TOOLS_DIR": devctl.TOOLS_DIR,
        }
        devctl.PROJECTS_CONF = self.projects_conf
        devctl.PORTS_CONF = self.ports_conf
        devctl.MAVEN_SETTINGS = self.settings
        devctl.BASE_DIR = self.root
        devctl.STATE = self.state
        devctl.TOOLS_DIR = self.root

        # 测试不能依赖本机端口占用与 /tmp 里的 pid 文件。
        self.port_patch = mock.patch.object(devctl, "port_listening", return_value=False)
        self.pid_patch = mock.patch.object(devctl, "pid_alive", return_value=False)
        self.port_patch.start()
        self.pid_patch.start()

    def tearDown(self):
        self.pid_patch.stop()
        self.port_patch.stop()
        for key, value in self.saved.items():
            setattr(devctl, key, value)
        self.tmp.cleanup()

    def write_projects(self, rows):
        """按 13 列补空后写入 projects.conf。"""
        lines = []
        for row in rows:
            padded = list(row) + [""] * (len(devctl.CONF_FIELDS) - len(row))
            lines.append("|".join(padded))
        self.projects_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def make_project(self, name, kind="backend", port=None, pom=True):
        """建一个最小项目目录；需要端口时写一份 application.yml。"""
        directory = self.root / name
        directory.mkdir()
        if pom:
            (directory / "pom.xml").write_text("<project/>", encoding="utf-8")
        if port is not None:
            resources = directory / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (resources / "application.yml").write_text(
                f"server:\n  port: {port}\n", encoding="utf-8"
            )
        return directory

    def collect(self, env_only=False):
        """配置类测试统一跳过环境检查，避免受本机 Java/Maven 安装情况影响。"""
        with mock.patch.object(doctor, "check_environment"):
            return doctor.collect(devctl, env_only=env_only)

    def levels(self, report):
        return [item["level"] for item in report.items]

    def texts(self, report):
        return "\n".join(item["text"] for item in report.items)


class ConfigCheckTest(DoctorTestBase):
    def test_missing_conf_fails_with_actionable_hint(self):
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("未找到 projects.conf", self.texts(report))
        hints = "\n".join(item["hint"] for item in report.items)
        self.assertIn("./install.sh", hints)

    def test_empty_conf_warns_instead_of_failing(self):
        self.projects_conf.write_text("# 只有注释\n\n", encoding="utf-8")
        report = self.collect()
        self.assertFalse(report.has_fail())
        self.assertIn("还没有任何项目", self.texts(report))
        self.assertIn("--scan", "\n".join(item["hint"] for item in report.items))

    def test_valid_project_passes(self):
        directory = self.make_project("svc-a", port=19081)
        self.write_projects([["svc-a", str(directory), "1"]])
        report = self.collect()
        self.assertFalse(report.has_fail(), self.texts(report))
        self.assertIn("1 个项目", self.texts(report))

    def test_missing_directory_fails(self):
        self.write_projects([["svc-a", str(self.root / "not-exist"), "0"]])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("目录不存在", self.texts(report))

    def test_relative_directory_warns(self):
        directory = self.make_project("svc-a")
        self.write_projects([["svc-a", directory.name, "0"]])
        report = self.collect()
        self.assertIn("不是绝对路径", self.texts(report))

    def test_duplicate_name_fails(self):
        directory = self.make_project("svc-a")
        self.write_projects([
            ["svc-a", str(directory), "0"],
            ["svc-a", str(directory), "0"],
        ])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("项目名重复", self.texts(report))

    def test_duplicate_ports_fail(self):
        first = self.make_project("svc-a")
        second = self.make_project("svc-b")
        self.write_projects([
            ["svc-a", str(first), "0"],
            ["svc-b", str(second), "0"],
        ])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("端口 8080 被多个项目占用", self.texts(report))

    def test_missing_start_script_fails(self):
        directory = self.make_project("svc-a")
        self.write_projects([["svc-a", str(directory), "0", "", "missing-start.sh"]])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("启动脚本不存在", self.texts(report))

    def test_platform_mismatch_warns(self):
        directory = self.make_project("svc-a")
        # POSIX 上配置 .cmd、Windows 上配置 .sh 都只是警告，不是致命错误。
        script = directory / "start.cmd"
        script.write_text("@echo off\n", encoding="utf-8")
        self.write_projects([["svc-a", str(directory), "0", "", str(script)]])
        report = self.collect()
        if devctl.plat.IS_WINDOWS:
            self.assertNotIn("当前平台使用", self.texts(report))
        else:
            self.assertIn("当前平台使用 .sh", self.texts(report))

    def test_frontend_without_package_json_fails(self):
        directory = self.make_project("web", pom=False)
        self.write_projects([["web", str(directory), "0", "", "", "", "", "", "@npm", "frontend"]])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("没有 package.json", self.texts(report))

    def test_frontend_without_package_manager_warns(self):
        directory = self.make_project("web", pom=False)
        (directory / "package.json").write_text("{}", encoding="utf-8")
        self.write_projects([["web", str(directory), "0", "", "", "", "", "", "", "frontend"]])
        report = self.collect()
        self.assertFalse(report.has_fail(), self.texts(report))
        self.assertIn("未指定前端包管理器", self.texts(report))

    def test_unknown_depends_warns(self):
        directory = self.make_project("svc-a")
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "", "backend", "", "svc-missing"]])
        report = self.collect()
        self.assertFalse(report.has_fail())
        self.assertIn("未登记的项目：svc-missing", self.texts(report))

    def test_missing_install_module_warns(self):
        directory = self.make_project("svc-a")
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "svc-a-module/api"]])
        report = self.collect()
        self.assertIn("install 模块不存在", self.texts(report))


class DepsCheckTest(DoctorTestBase):
    def test_requires_without_deps_conf_fails(self):
        directory = self.make_project("svc-a", port=19082)
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "", "backend", "", "", "mysql-test"]])
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("未找到 deps.conf", self.texts(report))

    def test_requires_name_not_registered_fails(self):
        directory = self.make_project("svc-a", port=19083)
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "", "backend", "", "", "mysql-test"]])
        (self.root / "deps.conf").write_text("redis-test|tcp|192.0.2.10|6379|缓存\n", encoding="utf-8")
        report = self.collect()
        self.assertTrue(report.has_fail())
        self.assertIn("依赖未登记：mysql-test", self.texts(report))

    def test_registered_dependency_passes(self):
        directory = self.make_project("svc-a", port=19084)
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "", "backend", "", "", "mysql-test"]])
        (self.root / "deps.conf").write_text("mysql-test|tcp|192.0.2.10|3306|测试库\n", encoding="utf-8")
        report = self.collect()
        self.assertFalse(report.has_fail(), self.texts(report))

    def test_bad_deps_rows_warn(self):
        (self.root / "deps.conf").write_text(
            "only-name|tcp\n"
            "http-dep|ftp|192.0.2.10|8080|x\n"
            "bad-port|tcp|192.0.2.10|99999|x\n",
            encoding="utf-8",
        )
        directory = self.make_project("svc-a", port=19085)
        self.write_projects([["svc-a", str(directory), "0", "", "", "", "", "", "", "backend", "", "", "only-name"]])
        report = self.collect()
        self.assertTrue(report.has_fail())  # only-name 行格式不全，所以没有登记成功
        text = self.texts(report)
        self.assertIn("格式不完整", text)
        self.assertIn("不认识", text)
        self.assertIn("端口不合法", text)


class EnvironmentCheckTest(DoctorTestBase):
    def test_missing_java_is_fail_when_backend_exists(self):
        directory = self.make_project("svc-a", port=19086)
        self.write_projects([["svc-a", str(directory), "0"]])
        with mock.patch.object(devctl, "find_tool", return_value=None):
            report = doctor.collect(devctl)
        self.assertTrue(report.has_fail())
        self.assertIn("未找到 java", self.texts(report))

    def test_missing_java_is_warn_without_backend(self):
        self.projects_conf.write_text("# 空配置\n", encoding="utf-8")
        with mock.patch.object(devctl, "find_tool", return_value=None):
            report = doctor.collect(devctl)
        self.assertFalse(report.has_fail())
        self.assertIn("未找到 java", self.texts(report))

    def test_java_version_parsing(self):
        self.assertEqual(doctor.java_version('openjdk version "17.0.9" 2023-10-17'), "17.0.9")
        self.assertEqual(doctor.java_version('java version "1.8.0_392"'), "1.8.0_392")
        self.assertEqual(doctor.java_major("1.8.0_392"), 8)
        self.assertEqual(doctor.java_major("17.0.9"), 17)
        self.assertEqual(doctor.java_major(""), 0)

    def test_old_java_warns(self):
        directory = self.make_project("svc-a", port=19087)
        self.write_projects([["svc-a", str(directory), "0"]])
        fake_java = self.root / "java"
        fake_java.write_text("", encoding="utf-8")

        def fake_find_tool(*names):
            return str(fake_java) if names and names[0] == "java" else None

        with mock.patch.object(devctl, "find_tool", side_effect=fake_find_tool), \
                mock.patch.object(doctor, "command_output", return_value='java version "1.8.0_392"'):
            report = doctor.collect(devctl)
        self.assertIn("版本偏低", self.texts(report))

    def test_env_only_skips_config_section(self):
        report = self.collect(env_only=True)
        self.assertFalse(report.has_fail())
        self.assertEqual(report.items, [])


class RunTest(DoctorTestBase):
    def test_run_returns_one_when_conf_missing(self):
        buffer = io.StringIO()
        with mock.patch.object(doctor, "check_environment"), contextlib.redirect_stdout(buffer):
            code = doctor.run()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", buffer.getvalue())

    def test_run_returns_zero_for_healthy_config(self):
        directory = self.make_project("svc-a", port=19088)
        self.write_projects([["svc-a", str(directory), "0"]])
        buffer = io.StringIO()
        with mock.patch.object(doctor, "check_environment"), contextlib.redirect_stdout(buffer):
            code = doctor.run()
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertIn("全部通过", buffer.getvalue())

    def test_unknown_argument_returns_two(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            self.assertEqual(doctor.main(["--wat"]), 2)
        self.assertIn("未知参数", buffer.getvalue())


class DevctlIntegrationTest(DoctorTestBase):
    def test_list_empty_conf_prints_hint(self):
        self.projects_conf.write_text("# 空\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = devctl.do_list()
        self.assertEqual(code, 0)
        self.assertIn("还没有任何项目", buffer.getvalue())

    def test_status_empty_conf_returns_one(self):
        self.projects_conf.write_text("# 空\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = devctl.do_status("all")
        self.assertEqual(code, 1)
        self.assertIn("--scan", buffer.getvalue())

    def test_json_stays_empty_array_without_projects(self):
        self.projects_conf.write_text("# 空\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            devctl.json_status()
        self.assertEqual(buffer.getvalue().strip(), "[]")

    def test_doctor_dispatch_env_flag(self):
        with mock.patch("doctor.run", return_value=3) as run:
            self.assertEqual(devctl.do_doctor("--env"), 3)
        run.assert_called_once_with(env_only=True)

    def test_doctor_rejects_unknown_flag(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(devctl.do_doctor("--bad"), 1)
        self.assertIn("用法", buffer.getvalue())

    def test_install_sh_syntax(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is unavailable")
        subprocess.run([bash, "-n", str(ROOT / "install.sh")], check=True)

    def test_install_cmd_is_crlf_batch(self):
        data = (ROOT / "install.cmd").read_bytes()
        self.assertIn(b"\r\n", data)
        text = data.decode("utf-8")
        self.assertTrue(text.startswith("@echo off\r\n"))
        # cmd 不认 # 注释，避免从 shell 脚本照搬。
        for line in text.splitlines():
            self.assertFalse(line.lstrip().startswith("#"), line)


if __name__ == "__main__":
    unittest.main()
