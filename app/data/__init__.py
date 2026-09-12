"""Data files that ship inside the wheel.

⚠️ **数据文件默认不会被打进 wheel。**`[tool.setuptools.packages.find]` 只收 Python
包，`.txt` 要靠 `pyproject.toml` 的 `[tool.setuptools.package-data]` 显式带上，
而且这个目录必须是个**包**（有 `__init__.py`）才能被 `importlib.resources` 定位。

这是 `alembic/` 那个坑的同一类：漏了不会有任何报错，直到装好的那一份在运行时
找不到文件。`scripts/packaging_smoke.py` 因此会验这里的文件真的在已安装的
发行版里。

### common_passwords.txt

来源：[SecLists](https://github.com/danielmiessler/SecLists) 的
`Passwords/Common-Credentials/xato-net-10-million-passwords-10000.txt`（MIT）。
取回后做了归一化：去空行、转小写、去重，9916 条。

⚠️ **实测数据**：这一万条里只有 **24 条长度 ≥ 12**。也就是说 12 位的长度下限
已经挡掉了其中 99.8%。所以真正在挡弱口令的是长度下限，这份表是补上那 24 条
「够长但仍然烂」的口令 —— 它是补充，不是主控。
"""
