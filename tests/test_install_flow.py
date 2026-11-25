"""Windows first-run setup: empty configurations and repeat safety."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import devctl


@unittest.skipUnless(os.name == "nt", "Windows batch setup requires cmd.exe")
class WindowsInstallFlowTest(unittest.TestCase):
    def test_first_run_and_repeat_preserve_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            shutil.copy2(ROOT / "install.cmd", directory)
            shutil.copy2(ROOT / "setup.cmd", directory)
            for example in ROOT.glob("*.example"):
                shutil.copy2(example, directory)
            env = os.environ.copy()
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

            def run_install():
                return subprocess.run(
                    [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(directory / "setup.cmd")],
                    cwd=directory, env=env, input="\n", capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                )

            result = run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Python", result.stdout)
            for name in ("projects", "deps", "ports", "health", "entries", "legacy-proxy"):
                lines = (directory / (name + ".conf")).read_text(encoding="utf-8").splitlines()
                self.assertFalse([line for line in lines if line.strip() and not line.lstrip().startswith("#")], name)

            project = directory / "projects.conf"
            project.write_bytes(b"user-project|D:/my-project|1\n")
            result = run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(project.read_bytes(), b"user-project|D:/my-project|1\n")


    def test_missing_python_shows_install_guidance(self):
        system32 = Path(os.environ["SystemRoot"]) / "System32"
        if any((system32 / name).exists() for name in ("python.exe", "py.exe")):
            self.skipTest("system Python makes the missing-Python fixture unavailable")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            shutil.copy2(ROOT / "install.cmd", directory)
            for example in ROOT.glob("*.example"):
                shutil.copy2(example, directory)
            env = os.environ.copy()
            env["PATH"] = str(system32)
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(directory / "install.cmd")],
                cwd=directory, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("https://www.python.org/downloads/", result.stdout)


class LocalMavenDiscoveryTest(unittest.TestCase):
    def test_project_maven_is_available_to_child_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            maven_bin = root / "apache-maven-3.9.1" / "bin"
            maven_bin.mkdir(parents=True)
            (maven_bin / "mvn.cmd").write_text("@echo off\n", encoding="utf-8")
            with mock.patch.object(devctl, "TOOLS_DIR", root), \
                 mock.patch.object(devctl.plat, "IS_WINDOWS", True), \
                 mock.patch.object(devctl.shutil, "which", return_value=None):
                paths = devctl.child_env()["PATH"].split(os.pathsep)
            self.assertIn(str(maven_bin), paths)


class ShellInstallFlowTest(unittest.TestCase):
    def test_first_run_is_empty_and_repeat_preserves_config(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            shutil.copy2(ROOT / "install.sh", directory)
            shutil.copy2(ROOT / "setup.command", directory)
            for example in ROOT.glob("*.example"):
                shutil.copy2(example, directory)

            def run_install():
                return subprocess.run(
                    [bash, str(directory / "setup.command")],
                    cwd=directory, input="\n", capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                )

            first = run_install()
            self.assertIn(first.returncode, (0, 1), first.stdout + first.stderr)
            for name in ("projects", "deps", "ports", "health", "entries", "legacy-proxy"):
                lines = (directory / (name + ".conf")).read_text(encoding="utf-8").splitlines()
                self.assertFalse([line for line in lines if line.strip() and not line.lstrip().startswith("#")], name)
            project = directory / "projects.conf"
            project.write_bytes(b"user-project|/example/path|1\n")
            second = run_install()
            self.assertIn(second.returncode, (0, 1), second.stdout + second.stderr)
            self.assertEqual(project.read_bytes(), b"user-project|/example/path|1\n")

    def test_mac_launcher_scripts_have_valid_shell_syntax(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is unavailable")
        for name in ("setup.command", "start.command"):
            result = subprocess.run([bash, "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(os.name == "nt", "Windows batch launcher requires cmd.exe")
class WindowsLauncherTest(unittest.TestCase):
    def test_start_entry_calls_launcher_using_valid_python(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            launcher_dir = directory / "app" / "windows"
            launcher_dir.mkdir(parents=True)
            marker = directory / "launched.txt"
            shutil.copy2(ROOT / "start.cmd", directory)
            shutil.copy2(ROOT / "app" / "windows" / "launch.cmd", launcher_dir)
            (launcher_dir / "launch.py").write_text(
                'import os\nfrom pathlib import Path\nPath(os.environ["TXLC_TEST_LAUNCH_MARKER"]).write_text("launched")\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            env["TXLC_TEST_LAUNCH_MARKER"] = str(marker)
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(directory / "start.cmd")],
                cwd=directory, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker.exists(), result.stdout + result.stderr)
