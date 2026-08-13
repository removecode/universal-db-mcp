"""按 Linux/CPython3.12 环境模拟离线安装，验证 wheels 目录自给自足。

pip 的 --platform 不会改变环境标记的求值（仍按当前解释器算），在 Windows 上
没法直接 dry-run 出 Linux 的安装结果。这里直接读每个 wheel 的 METADATA，
用目标环境求值 marker，逐层展开依赖，检查有没有缺包。
"""

from __future__ import annotations

import glob
import re
import sys
import zipfile
from email.parser import Parser

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

TARGET_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "linux",
}

wheels_dir = sys.argv[1]
paths = glob.glob(f"{wheels_dir}/*.whl")
if not paths:
    sys.exit(f"没有找到 wheel: {wheels_dir}")

available: dict[str, tuple[str, str]] = {}  # name -> (version, path)
requires: dict[str, list[str]] = {}

for path in paths:
    with zipfile.ZipFile(path) as z:
        meta_name = next(n for n in z.namelist() if re.match(r"[^/]+\.dist-info/METADATA$", n))
        meta = Parser().parsestr(z.read(meta_name).decode("utf-8", "replace"))
    name = canonicalize_name(meta["Name"])
    available[name] = (meta["Version"], path.replace("\\", "/").split("/")[-1])
    requires[name] = meta.get_all("Requires-Dist") or []

print(f"wheels 目录共 {len(available)} 个包\n")

seen: set[tuple[str, frozenset]] = set()
missing: list[str] = []
unsatisfied: list[str] = []
stack = [("starrocks-mcp", frozenset())]

while stack:
    name, extras = stack.pop()
    key = (name, extras)
    if key in seen:
        continue
    seen.add(key)

    if name not in available:
        missing.append(name)
        continue

    for raw in requires[name]:
        req = Requirement(raw)
        contexts = [{**TARGET_ENV, "extra": e} for e in extras] or [{**TARGET_ENV, "extra": ""}]
        if req.marker is not None and not any(req.marker.evaluate(c) for c in contexts):
            continue
        dep = canonicalize_name(req.name)
        if dep not in available:
            missing.append(f"{dep}  (被 {name} 依赖: {raw})")
            continue
        version = available[dep][0]
        if req.specifier and not req.specifier.contains(version, prereleases=True):
            unsatisfied.append(f"{dep}=={version} 不满足 {name} 的约束 {req.specifier}")
        stack.append((dep, frozenset(req.extras)))

reachable = sorted({n for n, _ in seen if n in available})
print(f"从 starrocks-mcp 出发可达 {len(reachable)} 个包")

unused = sorted(set(available) - set(reachable))
if unused:
    print(f"未被依赖到（多余但无害）: {', '.join(unused)}")

if missing:
    print("\n缺失的包:")
    for m in sorted(set(missing)):
        print("  -", m)
if unsatisfied:
    print("\n版本不满足约束:")
    for u in sorted(set(unsatisfied)):
        print("  -", u)

if missing or unsatisfied:
    sys.exit(1)

print("\n依赖闭包完整，Linux/py312 上可以纯离线安装")
print(f"关键版本: " + ", ".join(
    f"{n}=={available[n][0]}" for n in ("mcp", "starlette", "uvicorn", "pymysql", "dbutils", "pydantic")
    if n in available
))
