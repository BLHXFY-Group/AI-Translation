import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
TOOL_DIR = APP_DIR.parent
RUNTIME_PLATFORM = "windows" if sys.platform == "win32" else "macos"
VENV_DIR = TOOL_DIR / ".runtime" / RUNTIME_PLATFORM / "venv"
VENV_PYTHON = (
    VENV_DIR / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else VENV_DIR / "bin" / "python"
)
SCRIPT_PATH = APP_DIR / "fetch_gbf_wiki_jp_names.py"
VALENTINE_SCRIPT_PATH = APP_DIR / "fetch_gbf_wiki_valentine_names.py"
REQUIREMENTS_PATH = APP_DIR / "requirements.txt"


def clear_screen() -> None:
    if sys.stdout.isatty():
        os.system("cls" if sys.platform == "win32" else "clear")


def dependency_status(show: bool = True) -> bool:
    if not VENV_PYTHON.exists():
        if show:
            print("依赖状态：未创建虚拟环境")
            print(f"解释器：{VENV_PYTHON}")
        return False

    version = subprocess.run(
        [str(VENV_PYTHON), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    playwright = subprocess.run(
        [str(VENV_PYTHON), "-c", "import playwright"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    ready = version.returncode == 0 and playwright.returncode == 0
    if show:
        print("虚拟环境：已创建")
        print((version.stdout or version.stderr).strip() or "Python：无法读取版本")
        print(f"Playwright：{'已安装' if playwright.returncode == 0 else '未安装'}")
    return ready


def install_dependencies() -> bool:
    clear_screen()
    print("正在安装或修复依赖...")
    print()

    if not VENV_PYTHON.exists():
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)], check=False
        )
        if result.returncode != 0:
            print("创建虚拟环境失败。")
            return False

    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)],
        check=False,
    )
    print()
    if result.returncode == 0:
        print("依赖安装完成。")
        return True
    print("依赖安装失败，请检查网络和 Python 环境。")
    return False


def run_script(script_path: Path, args: list[str], title: str) -> bool:
    if not dependency_status(show=False):
        print()
        print("依赖尚未安装，请先选择 [1] 安装依赖。")
        return False

    clear_screen()
    print(f"正在执行{title}...")
    print(flush=True)
    result = subprocess.run(
        [str(VENV_PYTHON), str(script_path), *args],
        check=False,
    )
    print()
    if result.returncode == 0:
        print("操作完成。")
        return True
    print("操作失败，请查看上方错误信息。")
    return False


def run_update(mode: str, title: str) -> bool:
    return run_script(SCRIPT_PATH, [mode], title)


def pause_and_continue() -> None:
    print()
    try:
        input("按回车键返回菜单...")
    except EOFError:
        pass


def main() -> None:
    while True:
        clear_screen()
        print("========================================")
        print("        GBF Wiki 人物名抓取工具")
        print("========================================")
        print()
        dependency_status()
        print()
        print("[1] 安装或修复 Python 依赖")
        print("[2] 全量更新人物名 CSV")
        print("[3] 增量更新人物名 CSV")
        print("[4] 更新情人节返礼人名 CSV")
        print("[Q] 退出")
        print()
        try:
            choice = input("请选择操作：").strip().lower()
        except EOFError:
            return

        if choice == "1":
            install_dependencies()
            pause_and_continue()
        elif choice == "2":
            run_update("full", "全量更新")
            pause_and_continue()
        elif choice == "3":
            run_update("update", "增量更新")
            pause_and_continue()
        elif choice == "4":
            run_script(VALENTINE_SCRIPT_PATH, [], "情人节返礼人名更新")
            pause_and_continue()
        elif choice == "q":
            return
        else:
            print()
            print("无效选项，请输入 1、2、3、4 或 Q。")
            pause_and_continue()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
