#!/usr/bin/env python3
# 外部依赖、分组、依赖顺序与批量目标的单元测试（只用标准库）。
#
# 运行：
#   cd ~/java/dev-tools && python3 -m unittest discover -s tests -v
#
# 覆盖范围（对应 ROADMAP 的 S2）：
#   - deps.conf 解析与容错
#   - TCP / HTTP 探活（关闭端口必须判为不通）
#   - 启动前的依赖拦截（dependency_gate）
#   - 项目分组推导与目标展开（all / core / group:xx / names）
#   - 依赖拓扑排序（链、环、不在本次范围）

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402


def closed_port():
    """占用一个端口再释放，拿到一个当前必然没人监听的端口号。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class DepsConfTest(unittest.TestCase):
    """deps.conf 的解析。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "deps.conf"
        self.old = dashboard.DEPS_CONF
        dashboard.DEPS_CONF = self.path

    def tearDown(self):
        dashboard.DEPS_CONF = self.old
        self.tmp.cleanup()

    def test_missing_file_returns_empty(self):
        # 文件不存在时不应该报错，等同「没有登记依赖」。
        self.assertEqual(dashboard.load_deps_conf(), [])

    def test_parse_skips_comments_and_bad_rows(self):
        self.path.write_text(
            "# 注释\n"
            "\n"
            "mysql-test|tcp|192.0.2.10|3306|测试库\n"
            "redis-test | tcp | 192.0.2.10 | 6379 \n"
            "broken-row|tcp\n"          # 列数不够
            "|tcp|192.0.2.10|1234\n",   # 名称为空
            encoding="utf-8",
        )
        deps = dashboard.load_deps_conf()
        self.assertEqual([d["name"] for d in deps], ["mysql-test", "redis-test"])
        # 字段应去掉首尾空格
        self.assertEqual(deps[1]["host"], "192.0.2.10")
        self.assertEqual(deps[1]["port"], "6379")
        self.assertEqual(deps[1]["desc"], "")
        # 类型缺省按 tcp 处理
        self.assertEqual(deps[0]["kind"], "tcp")

    def test_unknown_kind_falls_back_to_tcp(self):
        self.path.write_text("x|udp|192.0.2.10|9999|d\n", encoding="utf-8")
        self.assertEqual(dashboard.load_deps_conf()[0]["kind"], "tcp")


class ProbeDepTest(unittest.TestCase):
    """依赖探活：只判断「端口上有没有服务」，不做协议交互。"""

    def test_closed_tcp_port_is_down(self):
        port = closed_port()
        ok, latency, error = dashboard.probe_dep(
            {"name": "x", "kind": "tcp", "host": "127.0.0.1", "port": str(port)}
        )
        self.assertFalse(ok)
        self.assertGreaterEqual(latency, 0)
        self.assertIn(str(port), error)

    def test_open_tcp_port_is_up(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            ok, _, _ = dashboard.probe_dep(
                {"name": "x", "kind": "tcp", "host": "127.0.0.1", "port": str(port)}
            )
            self.assertTrue(ok)
        finally:
            server.close()

    def test_invalid_port_is_down(self):
        ok, _, error = dashboard.probe_dep(
            {"name": "x", "kind": "tcp", "host": "127.0.0.1", "port": ""}
        )
        self.assertFalse(ok)
        self.assertIn("端口", error)


class DependencyGateTest(unittest.TestCase):
    """启动前的依赖拦截规则。"""

    def test_no_requires_never_blocks(self):
        gate = dashboard.dependency_gate({"name": "a", "requires": []})
        self.assertFalse(gate["blocked"])

    def test_missing_registration_blocks(self):
        # 项目声明了依赖，但 deps.conf 里没有 → 视为不通，避免「以为检查过了」。
        with mock.patch.object(dashboard, "deps_snapshot", return_value=[]):
            gate = dashboard.dependency_gate({"name": "a", "requires": ["mysql"]})
        self.assertTrue(gate["blocked"])
        self.assertIn("没有登记", gate["down"][0]["error"])

    def test_down_dependency_blocks_with_reason(self):
        snapshot = [{"name": "mysql", "ok": False, "error": "连接失败"}]
        with mock.patch.object(dashboard, "deps_snapshot", return_value=snapshot):
            gate = dashboard.dependency_gate({"name": "a", "requires": ["mysql"]})
        self.assertTrue(gate["blocked"])
        self.assertIn("mysql", gate["message"])

    def test_all_up_allows_start(self):
        snapshot = [{"name": "mysql", "ok": True, "error": ""}]
        with mock.patch.object(dashboard, "deps_snapshot", return_value=snapshot):
            gate = dashboard.dependency_gate({"name": "a", "requires": ["mysql"]})
        self.assertFalse(gate["blocked"])


PROJECTS = [
    {"name": "svc-auth", "core": True, "type": "backend", "group": "core", "depends": []},
    {"name": "svc-baseinfo", "core": True, "type": "backend", "group": "core", "depends": []},
    {"name": "txlc-gateway", "core": True, "type": "backend", "group": "core",
     "depends": ["svc-auth", "svc-baseinfo"]},
    {"name": "svc-order", "core": False, "type": "backend", "group": "business", "depends": []},
    {"name": "txlccwweb", "core": False, "type": "frontend", "group": "frontend", "depends": []},
]


class GroupAndTargetTest(unittest.TestCase):
    """分组推导与动作目标展开。"""

    def test_project_group_prefers_config(self):
        self.assertEqual(dashboard.project_group({"group": "portal"}), "portal")

    def test_project_group_falls_back_by_type_and_core(self):
        self.assertEqual(dashboard.project_group({"type": "frontend"}), "frontend")
        self.assertEqual(dashboard.project_group({"core": True}), "core")
        self.assertEqual(dashboard.project_group({}), "business")

    def test_expand_target_all_and_core(self):
        with mock.patch.object(dashboard, "get_projects", return_value=PROJECTS):
            self.assertEqual(len(dashboard.expand_target("all")), 5)
            self.assertEqual(
                sorted(dashboard.expand_target("core")),
                ["svc-auth", "svc-baseinfo", "txlc-gateway"],
            )

    def test_expand_target_group(self):
        with mock.patch.object(dashboard, "get_projects", return_value=PROJECTS):
            self.assertEqual(dashboard.expand_target("group:frontend"), ["txlccwweb"])

    def test_expand_target_names_keeps_registry_order(self):
        # names 来自前端勾选，顺序不应影响执行顺序（由拓扑排序决定）。
        with mock.patch.object(dashboard, "get_projects", return_value=PROJECTS):
            got = dashboard.expand_target("", ["txlccwweb", "svc-auth", "不存在"])
        self.assertEqual(got, ["svc-auth", "txlccwweb"])

    def test_expand_target_unknown_returns_empty(self):
        with mock.patch.object(dashboard, "get_projects", return_value=PROJECTS):
            self.assertEqual(dashboard.expand_target("svc-unknown"), [])


class TopoOrderTest(unittest.TestCase):
    """依赖拓扑排序：被依赖的排前面，依赖链正确，环不死循环。"""

    def test_dependency_comes_first(self):
        order = dashboard.topo_order(["txlc-gateway", "svc-order", "svc-auth"], PROJECTS)
        self.assertLess(order.index("svc-auth"), order.index("txlc-gateway"))
        self.assertEqual(len(order), 3)

    def test_dependency_outside_selection_ignored(self):
        # 只启动网关时，不应把它依赖的 auth 也带进来。
        order = dashboard.topo_order(["txlc-gateway"], PROJECTS)
        self.assertEqual(order, ["txlc-gateway"])

    def test_cycle_does_not_hang(self):
        cyclic = [
            {"name": "a", "depends": ["b"]},
            {"name": "b", "depends": ["a"]},
        ]
        order = dashboard.topo_order(["a", "b"], cyclic)
        self.assertEqual(sorted(order), ["a", "b"])

    def test_duplicate_names_deduplicated(self):
        self.assertEqual(dashboard.topo_order(["svc-order", "svc-order"], PROJECTS), ["svc-order"])


if __name__ == "__main__":
    unittest.main()
