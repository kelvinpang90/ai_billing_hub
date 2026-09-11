#!/usr/bin/env python3
"""Verify the INSTALLED distribution, not the source tree.

`pytest` 跑的是源码树（`pyproject.toml` 里 `pythonpath = ["."]`），结构上发现
不了打包缺陷——PR #22 上真发生过：`packages = ["app"]` 只把根包打进 wheel，
`app.api` / `app.core` 全部丢失，测试全绿而装完起不来。

**这个脚本必须在一个装了本项目的干净环境里跑，且不能让源码树进 sys.path。**
它放在 `scripts/` 下就是为了这个：以脚本方式运行时 `sys.path[0]` 是脚本自己
的目录（`scripts/`），那里没有 `app`，所以 `import app` 只能解析到已安装的
那一份。断言里再确认一次路径，防止有人把它挪到仓库根目录。

用法见 docs/WORKFLOW.md §7。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    import app
    from app.main import create_app

    locations = [Path(p).resolve() for p in app.__path__]
    if not any("site-packages" in p.as_posix() for p in locations):
        print(
            "Refusing to pass: `app` resolved to the source tree, not an installed "
            f"distribution ({locations}). Run this from a clean environment.",
            file=sys.stderr,
        )
        return 1

    # 子包必须真的在 wheel 里。只 import 根包发现不了缺子包。
    import app.api.health  # noqa: F401
    import app.core.config  # noqa: F401

    paths = create_app().openapi()["paths"]
    if "/healthz" not in paths:
        print(f"Installed app does not expose /healthz (paths: {sorted(paths)})", file=sys.stderr)
        return 1

    print(f"Packaging smoke passed: {locations[0]} serves /healthz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
