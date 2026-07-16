#!/usr/bin/env python3
"""比较翻译前后的 GBF 剧情 CSV，并验证结构与字段不变量。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


REQUIRED_COLUMNS = ("id", "name", "text", "trans")
TAG_PATTERN = re.compile(r"<[^>]+>")
KANA_PATTERN = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
QUOTE_PATTERN = re.compile(r"[「」『』“”]")
ODD_PUNCTUATION_PATTERN = re.compile(r"，，|。。|、，|，。|。；|；。")
TASK_ID_PATTERN = re.compile(r"\A\d{8}-\d{6}\Z")
LONG_VISIBLE_TEXT = 90
DEFAULT_MAX_DETAILS = 5
SKILL_MARKER = Path(".agents/skills/gbf-story-translation/SKILL.md")
TASKS_ROOT = Path(".codex-tmp/gbf-translation")


def configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class FileFormat:
    encoding: str
    bom: bool
    newline_style: str
    final_newline: bool
    physical_lines: int


@dataclass
class CsvDocument:
    path: Path
    raw: bytes
    text: str
    file_format: FileFormat
    rows: list[list[str]]
    header: list[str]
    indexes: dict[str, int]


@dataclass(frozen=True)
class TaskPaths:
    directory: Path
    updates: Path
    state: Path
    report: Path
    backup: Path
    terms: Path
    backlog: Path


class ValidationReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[tuple[str, str, str | None]] = []
        self.files = 0
        self.records = 0
        self.changed_trans = 0

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, category: str, message: str, record_id: str | None = None) -> None:
        self.warnings.append((category, message, record_id))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 GBF 剧情 CSV 的翻译改动")
    parser.add_argument("--before", type=Path, help="编辑前文件或目录")
    parser.add_argument("--after", type=Path, help="编辑后文件或目录")
    parser.add_argument("--task-id", required=True, help="会话任务 ID，格式为 YYYYMMDD-HHMMSS")
    parser.add_argument("--root", type=Path, help="仓库根目录，默认自动发现")
    parser.add_argument(
        "--max-details",
        type=int,
        default=DEFAULT_MAX_DETAILS,
        help="标准输出中每类问题最多展示的详情数，默认 5",
    )
    args = parser.parse_args()
    if args.max_details < 0:
        parser.error("--max-details 不能小于 0")
    if not TASK_ID_PATTERN.fullmatch(args.task_id):
        parser.error("--task-id 必须使用 YYYYMMDD-HHMMSS 格式")
    if (args.before is None) != (args.after is None):
        parser.error("--before 与 --after 必须同时提供")
    return args


def discover_repository_root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
        if not root.is_dir():
            raise ValueError(f"仓库根目录不存在: {root}")
        return root
    seen: set[str] = set()
    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        base = start if start.is_dir() else start.parent
        for candidate in (base, *base.parents):
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            if (candidate / SKILL_MARKER).is_file():
                return candidate
    raise ValueError("无法自动发现仓库根目录，请显式提供 --root")


def resolve_task_paths(root: Path, task_id: str) -> TaskPaths:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("task ID 必须使用 YYYYMMDD-HHMMSS 格式")
    directory = (root / TASKS_ROOT / task_id).resolve()
    return TaskPaths(
        directory=directory,
        updates=directory / "updates.json",
        state=directory / "state.json",
        report=directory / "validation-report.txt",
        backup=directory / "before",
        terms=directory / "terms.md",
        backlog=directory / "backlog.md",
    )


def collect_state_pairs(
    root: Path,
    state_path: Path,
    expected_task_id: str,
) -> list[tuple[str, Path, Path]]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取成功文件清单: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("state 根节点必须是 version 为 1 的对象")
    if set(state) != {"version", "task_id", "files"}:
        raise ValueError("state 根节点必须只包含 version、task_id 和 files")
    if state.get("task_id") != expected_task_id:
        raise ValueError(
            f"state.task_id 与 --task-id 不一致: "
            f"{state.get('task_id')!r} != {expected_task_id!r}"
        )
    entries = state.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("state.files 必须是非空数组")
    pairs: list[tuple[int, str, Path, Path]] = []
    seen_paths: set[str] = set()
    seen_orders: set[int] = set()
    for index, entry in enumerate(entries, start=1):
        label = f"state.files[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} 必须是对象")
        relative_text = entry.get("path")
        backup_text = entry.get("backup")
        order = entry.get("first_processed_order")
        if not isinstance(relative_text, str) or not relative_text or Path(relative_text).is_absolute():
            raise ValueError(f"{label}.path 必须是非空相对路径")
        if not isinstance(backup_text, str) or not backup_text:
            raise ValueError(f"{label}.backup 必须是非空字符串")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise ValueError(f"{label}.first_processed_order 必须是正整数")
        target = (root / relative_text).resolve()
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"{label}.path 越出仓库根目录") from exc
        if target.suffix.lower() != ".csv":
            raise ValueError(f"{label}.path 不是 CSV")
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise ValueError(f"state.files 中重复路径: {relative}")
        if order in seen_orders:
            raise ValueError(f"state.files 中重复 first_processed_order: {order}")
        seen_paths.add(path_key)
        seen_orders.add(order)
        backup_raw = Path(backup_text)
        backup = backup_raw.resolve() if backup_raw.is_absolute() else (root / backup_raw).resolve()
        pairs.append((order, relative, backup, target))
    return [(relative, backup, target) for order, relative, backup, target in sorted(pairs)]


def decode_utf8(raw: bytes, path: Path) -> tuple[str, str, bool]:
    if raw.startswith(b"\xef\xbb\xbf"):
        payload = raw[3:]
        try:
            return payload.decode("utf-8"), "utf-8", True
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path}: UTF-8 BOM 后的内容不是有效 UTF-8: {exc}") from exc
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError(f"{path}: 暂不支持 UTF-16 CSV")
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: 不是有效 UTF-8，无法可靠验证编码保持: {exc}") from exc


def inspect_newlines(raw: bytes) -> tuple[str, bool, int]:
    crlf = raw.count(b"\r\n")
    without_crlf = raw.replace(b"\r\n", b"")
    lone_lf = without_crlf.count(b"\n")
    lone_cr = without_crlf.count(b"\r")
    styles = sum(count > 0 for count in (crlf, lone_lf, lone_cr))
    if styles == 0:
        style = "none"
    elif styles > 1:
        style = "mixed"
    elif crlf:
        style = "crlf"
    elif lone_lf:
        style = "lf"
    else:
        style = "cr"
    final_newline = raw.endswith((b"\r\n", b"\n", b"\r"))
    physical_lines = len(raw.splitlines())
    return style, final_newline, physical_lines


def read_document(path: Path) -> CsvDocument:
    raw = path.read_bytes()
    text, encoding, bom = decode_utf8(raw, path)
    newline_style, final_newline, physical_lines = inspect_newlines(raw)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError(f"{path}: CSV 无法解析: {exc}") from exc
    if not rows:
        raise ValueError(f"{path}: CSV 为空")
    header = rows[0]
    for column in REQUIRED_COLUMNS:
        if header.count(column) != 1:
            raise ValueError(f"{path}: 表头必须且只能包含一个 {column!r} 列")
    width = len(header)
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            raise ValueError(f"{path}:{number}: 字段数 {len(row)} 与表头字段数 {width} 不一致")
    indexes = {column: header.index(column) for column in REQUIRED_COLUMNS}
    file_format = FileFormat(encoding, bom, newline_style, final_newline, physical_lines)
    return CsvDocument(path, raw, text, file_format, rows, header, indexes)


def collect_pairs(before: Path, after: Path) -> list[tuple[str, Path, Path]]:
    if before.is_file() and after.is_file():
        return [(after.name, before, after)]
    if before.is_dir() and after.is_dir():
        before_files = {path.relative_to(before).as_posix(): path for path in before.rglob("*.csv")}
        after_files = {path.relative_to(after).as_posix(): path for path in after.rglob("*.csv")}
        missing = sorted(before_files.keys() - after_files.keys())
        extra = sorted(after_files.keys() - before_files.keys())
        if missing or extra:
            details = []
            if missing:
                details.append("编辑后缺少: " + ", ".join(missing))
            if extra:
                details.append("编辑后新增: " + ", ".join(extra))
            raise ValueError("目录中的 CSV 集合不一致；" + "；".join(details))
        return [(relative, before_files[relative], after_files[relative]) for relative in sorted(before_files)]
    raise ValueError("--before 与 --after 必须同时为文件或同时为目录")


def record_label(document: CsvDocument, row: list[str], number: int) -> str:
    record_id = row[document.indexes["id"]]
    return record_id if record_id else f"物理记录 {number}"


def tags(value: str) -> list[str]:
    return TAG_PATTERN.findall(value)


def visible_text(value: str) -> str:
    value = TAG_PATTERN.sub("", value)
    value = value.replace("\\n", "")
    return re.sub(r"\s+", "", value)


def record_kind(document: CsvDocument, row: list[str]) -> str:
    record_id = row[document.indexes["id"]].strip()
    source = row[document.indexes["text"]]
    if record_id == "译者":
        return "translator"
    if not visible_text(source):
        return "control"
    if record_id == "0-synopsis":
        return "synopsis"
    if record_id == "0-chapter_name":
        return "chapter_name"
    if record_id.startswith("0-"):
        return "metadata"
    return "body"


def is_translatable_record(document: CsvDocument, row: list[str]) -> bool:
    return record_kind(document, row) not in {"translator", "control"}


def is_ordinary_body(document: CsvDocument, row: list[str]) -> bool:
    return record_kind(document, row) == "body"


def check_document_shape(document: CsvDocument, relative: str, report: ValidationReport) -> None:
    if document.file_format.newline_style == "mixed":
        report.error(f"{relative}: 使用了混合记录分隔符")
    if document.file_format.newline_style == "cr":
        report.error(f"{relative}: 使用了不支持的单独 CR 记录分隔符")
    if document.file_format.physical_lines != len(document.rows):
        report.error(
            f"{relative}: 物理行数 {document.file_format.physical_lines} "
            f"与 CSV 记录数 {len(document.rows)} 不一致"
        )
    for number, row in enumerate(document.rows, start=1):
        for column, value in zip(document.header, row):
            if "\r" in value or "\n" in value:
                report.error(f"{relative}:{number}: 字段 {column!r} 包含真实换行")


def compare_formats(
    before: CsvDocument,
    after: CsvDocument,
    relative: str,
    report: ValidationReport,
) -> None:
    if before.file_format.encoding != after.file_format.encoding:
        report.error(f"{relative}: 编码发生变化")
    if before.file_format.bom != after.file_format.bom:
        report.error(f"{relative}: BOM 状态发生变化")
    if before.file_format.newline_style != after.file_format.newline_style:
        report.error(
            f"{relative}: 记录分隔符从 {before.file_format.newline_style} "
            f"变为 {after.file_format.newline_style}"
        )
    if before.file_format.final_newline != after.file_format.final_newline:
        report.error(f"{relative}: 文件末尾换行状态发生变化")


def compare_rows(
    before: CsvDocument,
    after: CsvDocument,
    relative: str,
    report: ValidationReport,
) -> None:
    if before.header != after.header:
        report.error(f"{relative}: CSV 表头或列顺序发生变化")
        return
    if len(before.rows) != len(after.rows):
        report.error(f"{relative}: CSV 记录数从 {len(before.rows)} 变为 {len(after.rows)}")
        return
    trans_index = before.indexes["trans"]
    for number, (old_row, new_row) in enumerate(zip(before.rows[1:], after.rows[1:]), start=2):
        label = record_label(after, new_row, number)
        for index, (old_value, new_value) in enumerate(zip(old_row, new_row)):
            if index == trans_index:
                if old_value != new_value:
                    report.changed_trans += 1
                continue
            if old_value != new_value:
                report.error(f"{relative}:{label}: 非 trans 字段 {before.header[index]!r} 发生变化")


def check_translation_quality(
    before: CsvDocument,
    after: CsvDocument,
    relative: str,
    report: ValidationReport,
) -> None:
    text_index = after.indexes["text"]
    trans_index = after.indexes["trans"]
    for number, (old_row, new_row) in enumerate(zip(before.rows[1:], after.rows[1:]), start=2):
        kind = record_kind(after, new_row)
        if kind in {"translator", "control"}:
            continue
        label = record_label(after, new_row, number)
        source = new_row[text_index]
        translated = new_row[trans_index]
        old_translated = old_row[trans_index]
        if not visible_text(translated):
            report.warn("empty_trans", f"{relative}:{label}: 有效正文的 trans 为空", label)
        if kind == "body" and translated.count("\\n") + 1 > 3:
            report.error(f"{relative}:{label}: 普通正文超过三条显示行")
        source_quotes = QUOTE_PATTERN.findall(source)
        translated_quotes = QUOTE_PATTERN.findall(translated)
        if visible_text(translated) and translated_quotes != source_quotes:
            message = (
                f"{relative}:{label}: 引号序列与 text 不一致"
                f"（text={''.join(source_quotes) or '无'}，trans={''.join(translated_quotes) or '无'}）"
            )
            if translated != old_translated:
                report.error(message)
            else:
                report.warn("quote_style", message, label)
        source_tags = tags(source)
        translated_tags = tags(translated)
        if translated_tags != source_tags:
            if translated != old_translated:
                report.error(f"{relative}:{label}: 修改后的 trans 标签结构与 text 不一致")
            else:
                report.warn(
                    "tag",
                    f"{relative}:{label}: 既有 trans 标签结构可能与 text 不一致",
                    label,
                )
        if KANA_PATTERN.search(TAG_PATTERN.sub("", translated)):
            report.warn("kana", f"{relative}:{label}: trans 可能残留日文假名", label)
        visible = visible_text(translated)
        if kind == "body" and len(visible) > LONG_VISIBLE_TEXT:
            report.warn(
                "long_text",
                f"{relative}:{label}: 可见文本较长（{len(visible)} 字符）",
                label,
            )
        if ODD_PUNCTUATION_PATTERN.search(translated):
            report.warn("punctuation", f"{relative}:{label}: 标点组合可能异常", label)


def validate_pair(
    relative: str,
    before_path: Path,
    after_path: Path,
    report: ValidationReport,
) -> None:
    try:
        before = read_document(before_path)
        after = read_document(after_path)
    except (OSError, ValueError) as exc:
        report.error(str(exc))
        return
    report.files += 1
    report.records += max(len(after.rows) - 1, 0)
    check_document_shape(after, relative, report)
    compare_formats(before, after, relative, report)
    compare_rows(before, after, relative, report)
    if before.header == after.header and len(before.rows) == len(after.rows):
        check_translation_quality(before, after, relative, report)


def summary_lines(report: ValidationReport) -> list[str]:
    lines = [
        f"FILES: {report.files}",
        f"RECORDS: {report.records}",
        f"CHANGED trans: {report.changed_trans}",
        f"ERRORS: {len(report.errors)}",
        f"WARN candidates: {len(report.warnings)}",
    ]
    if not report.errors:
        lines.extend(
            [
                "PASS id/name/text and other non-trans fields unchanged",
                "PASS only trans fields changed",
                "PASS BOM and record separators preserved",
                "PASS no physical newline inside fields",
            ]
        )
    return lines


def write_full_report(report: ValidationReport, path: Path) -> None:
    lines = summary_lines(report)
    if report.errors:
        lines.append("")
        lines.append("[ERROR]")
        lines.extend(report.errors)
    if report.warnings:
        lines.append("")
        lines.append("[WARN]")
        lines.extend(f"[{category}] {message}" for category, message, _ in report.warnings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def print_compact_report(
    report: ValidationReport,
    max_details: int,
    report_path: Path | None,
) -> None:
    if report.errors:
        print(f"ERROR count: {len(report.errors)}")
        for message in report.errors[:max_details]:
            print(f"ERROR {message}")
        omitted = len(report.errors) - max_details
        if omitted > 0:
            print(f"ERROR omitted: {omitted}")
    else:
        print(f"PASS files: {report.files}")
        print(f"PASS records: {report.records}")
        print("PASS non-trans fields and file format unchanged")
        print(f"CHANGED trans: {report.changed_trans}")
    warning_counts = Counter(category for category, _, _ in report.warnings)
    warning_groups: dict[str, list[str]] = defaultdict(list)
    for category, message, _ in report.warnings:
        warning_groups[category].append(message)
    for category in sorted(warning_counts):
        messages = warning_groups[category]
        print(f"WARN {category}: {len(messages)}")
        for message in messages[:max_details]:
            print(f"WARN[{category}] {message}")
        omitted = len(messages) - max_details
        if omitted > 0:
            print(f"WARN[{category}] omitted: {omitted}")
    if report_path is not None:
        print(f"FULL report: {report_path}")


def main() -> int:
    configure_utf8_output()
    args = parse_args()
    report = ValidationReport()
    report_path: Path | None = None
    try:
        root = discover_repository_root(args.root)
        task_paths = resolve_task_paths(root, args.task_id)
        report_path = task_paths.report
        if args.before is None:
            pairs = collect_state_pairs(root, task_paths.state, args.task_id)
        else:
            pairs = collect_pairs(args.before, args.after)
    except ValueError as exc:
        report.error(str(exc))
        written_report: Path | None = None
        try:
            if report_path is not None:
                write_full_report(report, report_path)
                written_report = report_path
        except OSError as report_exc:
            print(f"WARN report_write: {report_exc}")
        print_compact_report(report, args.max_details, written_report)
        return 1
    if not pairs:
        report.error("没有找到可比较的 CSV 文件")
    for relative, before_path, after_path in pairs:
        validate_pair(relative, before_path, after_path, report)
    written_report = None
    try:
        if report_path is not None:
            write_full_report(report, report_path)
            written_report = report_path
    except OSError as exc:
        print(f"WARN report_write: {exc}")
    print_compact_report(report, args.max_details, written_report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
