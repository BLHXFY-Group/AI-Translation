# GBF Wiki 人物名抓取工具

Windows 可以直接双击 `run_gbf_wiki_tools.bat`，通过菜单安装依赖并执行全量或增量更新。

macOS 可以直接双击 `run_gbf_wiki_tools.command`。如果系统首次阻止运行，可以在终端执行：

```bash
chmod +x run_gbf_wiki_tools.command
./run_gbf_wiki_tools.command
```

首次使用：

```powershell
python -m venv .runtime/windows/venv
.\.runtime\windows\venv\Scripts\python -m pip install -r app\requirements.txt
```

全量更新：

```powershell
.\.runtime\windows\venv\Scripts\python app\fetch_gbf_wiki_jp_names.py full
```

增量更新：

```powershell
.\.runtime\windows\venv\Scripts\python app\fetch_gbf_wiki_jp_names.py update
```

更新 2018 年至当前年份的情人节返礼人名：

```powershell
.\.runtime\windows\venv\Scripts\python app\fetch_gbf_wiki_valentine_names.py
```

Windows 和 macOS 分别使用 `.runtime/windows` 和 `.runtime/macos` 保存虚拟环境与浏览器验证状态，互不混用。抓取结果写入工具根目录的 `gbf_wiki_jp_names.csv`。

CSV 使用 `type,id,japanese_name,chinese_name,ids` 结构。`name` 记录按日文名去重，并使用最大 ID 对应的中文名；`ids` 保留该日文名已抓取的全部 ID。`skip` 记录汇总不含人物数据的占位页面 ID，增量更新会跳过它们，全量更新则会重新检查。

情人节返礼结果写入 `gbf_wiki_jp_names_valentine.csv`，字段为 `japanese_name,chinese_name`。工具每次会抓取 2018 年至当前年份的全部有效分页，自动忽略未创建的红链分页，同一日文名使用最新年份的中文名。页面错误会记录到控制台并跳过，已有 CSV 中本次未抓取到的人名会保留。

两个平台启动脚本只负责寻找系统 Python。菜单、依赖管理和更新调度统一由 `app/launcher.py` 处理，抓取源码和依赖清单也位于 `app` 目录。
