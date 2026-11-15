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

### NotoSansSC-Regular.ttf

月度对账单 PDF 里客户名、项目名等中文字符用的字体（Phase 7；Kelvin 2026-11-14 定：嵌入完整
字体，不预先子集化）。fpdf2 渲染时只把用到的字形子集嵌进 PDF，所以 PDF 本身很小。

- 来源：Google Fonts 的 Noto Sans SC 静态 Regular（`static/NotoSansSC-Regular.ttf`，
  字体版本 2.004），TrueType（`glyf`）轮廓，不是可变字体；原样提交，未改名、未改字形
- 许可证：SIL Open Font License 1.1，全文随附于 `NotoSansSC-OFL.txt`（OFL 要求再分发时
  随附；`*.txt` 与 `*.ttf` 都在 package-data 里，wheel 与 Docker 镜像都会带上）
- SHA-256：`d45f67f0a7c0ca3f256950777ce6a61cc7ce5f9696d02900cbbaac25f8aa7d16`
- 大小：10,559,284 字节

⚠️ **换这个文件会改变历史对账单 PDF 重新渲染出的字形**（PDF 由快照即时生成，不存文件），
所以换字体要过设计闸门，并同步对账单 PDF 代码里钉住的哈希。
"""
