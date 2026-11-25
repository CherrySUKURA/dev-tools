#!/usr/bin/env python3
# 扫描 Maven 项目，输出 projects.conf 候选行。
# 只做启发式识别；生成后必须人工确认项目名、路径、核心标记。
#
# 用法：
#   python3 discover.py /path/to/projects
#   python3 discover.py /path/a /path/b --depth 8
#
# 输出格式：项目名|绝对路径|是否核心(1/0)|

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SKIP_DIRS = {
    "target", ".git", ".idea", ".vscode", "node_modules",
    "__pycache__", ".mvn", "logs", "dev-tools",
}


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def direct_text(parent, name):
    """读取直接子元素的文本。"""
    for child in parent:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def parse_pom(path):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None

    artifact = direct_text(root, "artifactId")
    packaging = direct_text(root, "packaging") or "jar"

    parent_artifact = None
    parent_rel = "../pom.xml"
    for child in root:
        if local_name(child.tag) == "parent":
            parent_artifact = direct_text(child, "artifactId")
            rel = direct_text(child, "relativePath")
            if rel is not None:
                parent_rel = rel
            break

    has_boot_plugin = any(
        local_name(el.tag) == "artifactId"
        and (el.text or "").strip() == "spring-boot-maven-plugin"
        for el in root.iter()
    )

    return {
        "path": path,
        "dir": path.parent,
        "artifact": artifact,
        "packaging": packaging,
        "parent_artifact": parent_artifact,
        "parent_rel": parent_rel,
        "has_boot_plugin": has_boot_plugin,
    }


def has_main_class(project_dir):
    """粗略判断是否是 Spring Boot 启动模块。"""
    java_dir = project_dir / "src" / "main" / "java"
    if not java_dir.is_dir():
        return False
    for _ in java_dir.rglob("*Application.java"):
        return True
    return False


def is_server_module(pom):
    artifact = (pom["artifact"] or "").lower()
    dir_name = pom["dir"].name.lower()
    if artifact.endswith("-server") or dir_name.endswith("-server"):
        return True
    if pom["packaging"] == "jar" and pom["has_boot_plugin"]:
        return True
    if pom["packaging"] == "jar" and has_main_class(pom["dir"]):
        return True
    return False


def resolve_project_root(pom, pom_map):
    """通过 parent relativePath 找到项目根；找不到则用模块自身目录。"""
    rel = pom["parent_rel"]
    if rel:
        parent_pom = (pom["dir"] / rel).resolve()
        parent = pom_map.get(parent_pom)
        if parent and parent["dir"] != pom["dir"]:
            return parent

    # 默认 ../pom.xml
    parent_pom = (pom["dir"] / ".." / "pom.xml").resolve()
    parent = pom_map.get(parent_pom)
    if parent and parent["dir"] != pom["dir"]:
        return parent

    return None


def read_port(project_root, server_dir):
    """优先读 local profile，再读主配置；解析 server.port。"""
    candidates = [
        server_dir / "src/main/resources/application-local.yml",
        server_dir / "src/main/resources/application.yml",
        project_root / "src/main/resources/application-local.yml",
        project_root / "src/main/resources/application.yml",
    ]
    # 只在 server: 块里找 port，避免误读 redis/datasource 等其他 port。
    server_block = re.compile(
        r"^server:\s*\n((?:[ \t]+.*(?:\n|$))*)",
        re.MULTILINE,
    )
    port_line = re.compile(
        r"^[ \t]+port:\s*(?:\$\{SERVER_PORT:(\d+)\}|(\d+))",
        re.MULTILINE,
    )
    inline_server = re.compile(
        r"^server:\s*\{[^}]*\bport:\s*(?:\$\{SERVER_PORT:(\d+)\}|(\d+))",
        re.MULTILINE,
    )

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        block = server_block.search(text)
        if block:
            match = port_line.search(block.group(1))
            if match:
                return match.group(1) or match.group(2)

        match = inline_server.search(text)
        if match:
            return match.group(1) or match.group(2)

    return ""


def guess_name(project_pom, server_pom):
    if project_pom and project_pom["artifact"]:
        name = project_pom["artifact"]
    else:
        name = server_pom["artifact"] or server_pom["dir"].name

    # 单模块 Spring Boot 项目：svc-xxx-server -> svc-xxx
    if name.lower().endswith("-server"):
        name = name[: -len("-server")]
    return name


def guess_core(name, path):
    text = f"{name} {path}".lower()
    return "1" if any(key in text for key in ("auth", "baseinfo", "gateway")) else "0"


def iter_poms(root, max_depth):
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            depth = 0
        if depth > max_depth:
            dirnames[:] = []
            continue
        if "pom.xml" in filenames:
            yield current / "pom.xml"


def main():
    parser = argparse.ArgumentParser(description="扫描 Maven 项目并输出 projects.conf 候选")
    parser.add_argument("roots", nargs="+", help="项目根目录或工作区目录")
    parser.add_argument("--depth", type=int, default=6, help="最大扫描深度，默认 6")
    args = parser.parse_args()

    all_poms = []
    for root in map(Path, args.roots):
        if not root.exists():
            print(f"# 跳过不存在的目录：{root}", file=sys.stderr)
            continue
        for pom_path in iter_poms(root, args.depth):
            pom = parse_pom(pom_path)
            if pom:
                all_poms.append(pom)

    pom_map = {pom["path"].resolve(): pom for pom in all_poms}
    projects = {}
    skipped = 0

    for pom in all_poms:
        if not is_server_module(pom):
            skipped += 1
            continue

        project_pom = resolve_project_root(pom, pom_map)
        project_root = project_pom["dir"] if project_pom else pom["dir"]
        server_dir = pom["dir"]

        name = guess_name(project_pom, pom)
        if not name:
            name = project_root.name

        # 同一项目多个 server 模块时只取第一个，后续人工确认。
        key = str(project_root.resolve())
        if key in projects:
            continue

        port = read_port(project_root, server_dir)
        projects[key] = {
            "name": name,
            "dir": key,
            "port": port,
            "core": guess_core(name, key),
        }

    if not projects:
        print("# 未识别到 Spring Boot server 模块。")
        print("# 请确认项目包含 *-server 模块，或手动编写 projects.conf。")
        print(f"# 已扫描 POM 数：{len(all_poms)}，跳过：{skipped}", file=sys.stderr)
        return 0

    print("# 由 discover.py 生成，请确认项目名、路径、核心标记和端口后再使用。")
    print("# 项目名|项目绝对路径|是否核心(1/0)|jar相对路径(可选)")
    for item in sorted(projects.values(), key=lambda x: (-int(x["core"]), x["name"])):
        print(f"{item['name']}|{item['dir']}|{item['core']}|")
        if item["port"]:
            print(f"# 建议端口：{item['name']} {item['port']}", file=sys.stderr)

    print(
        f"# 共识别 {len(projects)} 个项目，扫描 POM {len(all_poms)} 个，跳过 {skipped} 个。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
