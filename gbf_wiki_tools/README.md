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

Windows 和 macOS 分别使用 `.runtime/windows` 和 `.runtime/macos` 保存虚拟环境与浏览器验证状态，互不混用。抓取结果写入工具根目录的 `gbf_wiki_jp_names.csv`。

两个平台启动脚本只负责寻找系统 Python。菜单、依赖管理和更新调度统一由 `app/launcher.py` 处理，抓取源码和依赖清单也位于 `app` 目录。
