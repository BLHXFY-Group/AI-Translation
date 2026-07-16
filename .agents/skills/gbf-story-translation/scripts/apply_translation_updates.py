#!/usr/bin/env python3
"""根据 JSON 更新 GBF 剧情 CSV 的 trans，并在提交前完成验证。"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 避免导入验证模块时在 Skill 目录生成 __pycache__。
sys.dont_write_bytecode = True

import validate_translation_csv as validator


SKILL_MARKER = Path(".agents/skills/gbf-story-translation/SKILL.md")
STATE_FILE_KEYS = {
    "first_processed_order",
    "path",
    "effective_body_count",
    "changed_trans_count",
    "backup",
    "warnings",
}


class ApplyError(Exception):
    """表示 JSON 或写回前置条件不满足。"""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApplyError(f"JSON 对象包含重复键: {key!r}")
        result[key] = value
    return result


@dataclass
class PlannedFile:
    relative: Path
    target: Path
    backup: Path
    source: validator.CsvDocument
    output: bytes
    requested_updates: int
    effective_body_count: int
    changed_trans_count: int = 0
    warnings: list[tuple[str, str, str | None]] = field(default_factory=list)
    staged: Path | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据 JSON 定点更新 GBF 剧情 CSV")
    parser.add_argument("--root", type=Path, help="仓库根目录，默认自动发现")
    parser.add_argument("--task-id", required=True, help="会话任务 ID，格式为 YYYYMMDD-HHMMSS")
    parser.add_argument(
        "--max-details",
        type=int,
        default=validator.DEFAULT_MAX_DETAILS,
        help="标准输出中每类问题最多展示的详情数，默认 5",
    )
    args = parser.parse_args()
    if args.max_details < 0:
        parser.error("--max-details 不能小于 0")
    if not validator.TASK_ID_PATTERN.fullmatch(args.task_id):
        parser.error("--task-id 必须使用 YYYYMMDD-HHMMSS 格式")
    return args


def discover_repository_root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
        if not root.is_dir():
            raise ApplyError(f"仓库根目录不存在: {root}")
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
    raise ApplyError("无法自动发现仓库根目录，请显式提供 --root")


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApplyError(f"{label} 必须是 JSON 对象")
    return value


def require_exact_keys(value: dict[str, Any], required: set[str], label: str) -> None:
    missing = required - value.keys()
    extra = value.keys() - required
    if missing:
        raise ApplyError(f"{label} 缺少字段: {', '.join(sorted(missing))}")
    if extra:
        raise ApplyError(f"{label} 包含未知字段: {', '.join(sorted(extra))}")


def require_nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApplyError(f"{label} 必须是非负整数")
    return value


def state_path_key(raw_path: str) -> str:
    return raw_path.replace("\\", "/").casefold()


def validate_state_file_entry(value: Any, index: int) -> dict[str, Any]:
    label = f"state.files[{index}]"
    entry = require_object(value, label)
    require_exact_keys(entry, STATE_FILE_KEYS, label)
    order = entry["first_processed_order"]
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise ApplyError(f"{label}.first_processed_order 必须是正整数")
    path = entry["path"]
    if not isinstance(path, str) or not path or Path(path).is_absolute():
        raise ApplyError(f"{label}.path 必须是非空相对路径")
    require_nonnegative_integer(entry["effective_body_count"], f"{label}.effective_body_count")
    require_nonnegative_integer(entry["changed_trans_count"], f"{label}.changed_trans_count")
    backup = entry["backup"]
    if not isinstance(backup, str) or not backup:
        raise ApplyError(f"{label}.backup 必须是非空字符串")
    warnings = entry["warnings"]
    if not isinstance(warnings, list):
        raise ApplyError(f"{label}.warnings 必须是数组")
    for warning_index, warning in enumerate(warnings, start=1):
        warning_label = f"{label}.warnings[{warning_index}]"
        warning = require_object(warning, warning_label)
        require_exact_keys(warning, {"type", "id", "message"}, warning_label)
        if not isinstance(warning["type"], str) or not warning["type"]:
            raise ApplyError(f"{warning_label}.type 必须是非空字符串")
        if warning["id"] is not None and not isinstance(warning["id"], str):
            raise ApplyError(f"{warning_label}.id 必须是字符串或 null")
        if not isinstance(warning["message"], str) or not warning["message"]:
            raise ApplyError(f"{warning_label}.message 必须是非空字符串")
    return entry


def load_state(path: Path, task_id: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "task_id": task_id, "files": []}
    try:
        state = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except OSError as exc:
        raise ApplyError(f"无法读取成功文件清单: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplyError(f"成功文件清单无法解析: {exc}") from exc
    state = require_object(state, "state 根节点")
    require_exact_keys(state, {"version", "task_id", "files"}, "state 根节点")
    if isinstance(state["version"], bool) or state["version"] != 1:
        raise ApplyError("state.version 必须是数字 1")
    if state["task_id"] != task_id:
        raise ApplyError(
            f"state.task_id 与 --task-id 不一致: "
            f"{state['task_id']!r} != {task_id!r}"
        )
    if not isinstance(state["files"], list):
        raise ApplyError("state.files 必须是数组")
    orders: set[int] = set()
    paths: set[str] = set()
    for index, raw_entry in enumerate(state["files"], start=1):
        entry = validate_state_file_entry(raw_entry, index)
        order = entry["first_processed_order"]
        path_key = state_path_key(entry["path"])
        if order in orders:
            raise ApplyError(f"state.files 中重复 first_processed_order: {order}")
        if path_key in paths:
            raise ApplyError(f"state.files 中重复路径: {entry['path']}")
        orders.add(order)
        paths.add(path_key)
    return state


def path_for_state(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def warnings_for_state(plan: PlannedFile) -> list[dict[str, str | None]]:
    warnings: list[dict[str, str | None]] = []
    prefix = f"{plan.relative.as_posix()}:"
    for category, message, record_id in plan.warnings:
        detail = message[len(prefix) :].strip() if message.startswith(prefix) else message
        id_prefix = f"{record_id}:" if record_id else ""
        if id_prefix and detail.startswith(id_prefix):
            detail = detail[len(id_prefix) :].strip()
        warnings.append({"type": category, "id": record_id, "message": detail})
    return sorted(warnings, key=lambda item: (str(item["type"]), str(item["id"]), str(item["message"])))


def build_updated_state(
    root: Path,
    state: dict[str, Any],
    plans: list[PlannedFile],
) -> bytes:
    entries = [dict(entry) for entry in state["files"]]
    indexes = {state_path_key(entry["path"]): index for index, entry in enumerate(entries)}
    next_order = max((entry["first_processed_order"] for entry in entries), default=0) + 1
    for plan in plans:
        relative = plan.relative.as_posix()
        path_key = state_path_key(relative)
        existing_index = indexes.get(path_key)
        if existing_index is None:
            order = next_order
            next_order += 1
        else:
            order = entries[existing_index]["first_processed_order"]
        entry = {
            "first_processed_order": order,
            "path": relative,
            "effective_body_count": plan.effective_body_count,
            "changed_trans_count": plan.changed_trans_count,
            "backup": path_for_state(root, plan.backup),
            "warnings": warnings_for_state(plan),
        }
        if existing_index is None:
            indexes[path_key] = len(entries)
            entries.append(entry)
        else:
            entries[existing_index] = entry
    updated = {"version": 1, "task_id": state["task_id"], "files": entries}
    return (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except OSError as exc:
        raise ApplyError(f"无法读取更新 JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplyError(f"更新 JSON 无法解析: {exc}") from exc
    payload = require_object(payload, "JSON 根节点")
    require_exact_keys(payload, {"version", "files"}, "JSON 根节点")
    version = payload["version"]
    if isinstance(version, bool) or version != 1:
        raise ApplyError("version 必须是数字 1")
    files = payload["files"]
    if not isinstance(files, list) or not files:
        raise ApplyError("files 必须是非空数组")
    return payload


def resolve_target(root: Path, raw_path: str) -> tuple[Path, Path]:
    if not raw_path or Path(raw_path).is_absolute():
        raise ApplyError(f"目标路径必须是非空相对路径: {raw_path!r}")
    target = (root / Path(raw_path)).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ApplyError(f"目标路径越出仓库根目录: {raw_path}") from exc
    if target.suffix.lower() != ".csv":
        raise ApplyError(f"目标文件不是 CSV: {raw_path}")
    if not target.is_file():
        raise ApplyError(f"目标 CSV 不存在: {raw_path}")
    return target, relative


def normalize_trans(value: str) -> str:
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def parse_updates(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ApplyError(f"{label}.updates 必须是对象")
    updates: dict[str, str] = {}
    for record_id, translated in value.items():
        item_label = f"{label}.updates[{record_id!r}]"
        if not isinstance(record_id, str) or not record_id:
            raise ApplyError(f"{label}.updates 的键必须是非空字符串")
        if not isinstance(translated, str):
            raise ApplyError(f"{item_label} 必须是字符串")
        updates[record_id] = normalize_trans(translated)
    return updates


def split_record_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1:]
    return line, ""


def csv_field_spans(line: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    in_quotes = False
    while index < len(line):
        char = line[index]
        if in_quotes:
            if char == '"':
                if index + 1 < len(line) and line[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if char == ",":
            spans.append((start, index))
            start = index + 1
        elif char == '"' and index == start:
            in_quotes = True
        index += 1
    if in_quotes:
        raise ApplyError("CSV 记录包含未闭合引号")
    spans.append((start, len(line)))
    return spans


def encode_csv_field(value: str) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([value])
    return stream.getvalue()


def render_updates(
    document: validator.CsvDocument,
    updates: dict[str, str],
    relative: Path,
) -> bytes:
    baseline = validator.ValidationReport()
    validator.check_document_shape(document, relative.as_posix(), baseline)
    if baseline.errors:
        raise ApplyError("；".join(baseline.errors))
    record_lines = document.text.splitlines(keepends=True)
    if len(record_lines) != len(document.rows):
        raise ApplyError(f"{relative.as_posix()}: 无法建立 CSV 记录与物理行的一一对应")
    id_index = document.indexes["id"]
    trans_index = document.indexes["trans"]
    positions: dict[str, list[int]] = defaultdict(list)
    for row_index, row in enumerate(document.rows[1:], start=1):
        positions[row[id_index]].append(row_index)
    for record_id in updates:
        matches = positions.get(record_id, [])
        if not matches:
            raise ApplyError(f"{relative.as_posix()}: 找不到 ID {record_id!r}")
        if len(matches) != 1:
            raise ApplyError(f"{relative.as_posix()}: ID {record_id!r} 匹配 {len(matches)} 条记录")
    for record_id, translated in updates.items():
        row_index = positions[record_id][0]
        content, ending = split_record_ending(record_lines[row_index])
        spans = csv_field_spans(content)
        if len(spans) != len(document.header):
            raise ApplyError(
                f"{relative.as_posix()}:{record_id}: 原始字段跨度数与 CSV 表头不一致"
            )
        start, end = spans[trans_index]
        record_lines[row_index] = content[:start] + encode_csv_field(translated) + content[end:] + ending
    rendered = "".join(record_lines).encode("utf-8")
    if document.file_format.bom:
        rendered = b"\xef\xbb\xbf" + rendered
    return rendered


def build_plans(root: Path, backup_root: Path, payload: dict[str, Any]) -> list[PlannedFile]:
    plans: list[PlannedFile] = []
    seen_paths: set[str] = set()
    for index, raw_file in enumerate(payload["files"], start=1):
        label = f"files[{index}]"
        file_item = require_object(raw_file, label)
        require_exact_keys(file_item, {"path", "updates"}, label)
        raw_path = file_item["path"]
        if not isinstance(raw_path, str):
            raise ApplyError(f"{label}.path 必须是字符串")
        target, relative = resolve_target(root, raw_path)
        path_key = str(target).casefold()
        if path_key in seen_paths:
            raise ApplyError(f"重复目标文件: {relative.as_posix()}")
        seen_paths.add(path_key)
        updates = parse_updates(file_item["updates"], label)
        try:
            document = validator.read_document(target)
        except (OSError, ValueError) as exc:
            raise ApplyError(str(exc)) from exc
        output = render_updates(document, updates, relative)
        effective_body_count = sum(
            validator.is_translatable_record(document, row) for row in document.rows[1:]
        )
        plans.append(
            PlannedFile(
                relative=relative,
                target=target,
                backup=backup_root / relative,
                source=document,
                output=output,
                requested_updates=len(updates),
                effective_body_count=effective_body_count,
            )
        )
    return plans


def bind_recorded_backups(
    root: Path,
    state: dict[str, Any],
    plans: list[PlannedFile],
) -> None:
    entries = {state_path_key(entry["path"]): entry for entry in state["files"]}
    for plan in plans:
        entry = entries.get(state_path_key(plan.relative.as_posix()))
        if entry is None:
            continue
        recorded = Path(entry["backup"])
        plan.backup = recorded.resolve() if recorded.is_absolute() else (root / recorded).resolve()
        if not plan.backup.is_file():
            raise ApplyError(
                f"{plan.relative.as_posix()}: state 记录的原文件副本不存在: {plan.backup}"
            )


def create_backups(plans: list[PlannedFile]) -> None:
    for plan in plans:
        plan.backup.parent.mkdir(parents=True, exist_ok=True)
        if plan.backup.exists():
            try:
                backup_document = validator.read_document(plan.backup)
            except (OSError, ValueError) as exc:
                raise ApplyError(str(exc)) from exc
            compatibility = validator.ValidationReport()
            relative = plan.relative.as_posix()
            validator.check_document_shape(backup_document, relative, compatibility)
            validator.compare_formats(
                backup_document,
                plan.source,
                relative,
                compatibility,
            )
            validator.compare_rows(
                backup_document,
                plan.source,
                relative,
                compatibility,
            )
            if compatibility.errors:
                raise ApplyError(
                    f"既有原文件副本与当前目标不兼容: {'；'.join(compatibility.errors)}"
                )
            continue
        plan.backup.write_bytes(plan.source.raw)


def stage_outputs(plans: list[PlannedFile]) -> None:
    for plan in plans:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{plan.target.name}.",
            suffix=".codex-tmp",
            dir=plan.target.parent,
            delete=False,
        ) as handle:
            handle.write(plan.output)
            plan.staged = Path(handle.name)


def cleanup_staged(plans: list[PlannedFile]) -> None:
    for plan in plans:
        if plan.staged is not None and plan.staged.exists():
            try:
                plan.staged.unlink()
            except OSError:
                pass


def stage_state(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".codex-tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        return Path(handle.name)


def cleanup_staged_state(path: Path | None) -> None:
    if path is not None and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def validate_staged(plans: list[PlannedFile]) -> validator.ValidationReport:
    report = validator.ValidationReport()
    for plan in plans:
        if plan.staged is None:
            report.error(f"{plan.relative.as_posix()}: 缺少临时输出文件")
            continue
        changed_before = report.changed_trans
        warnings_before = len(report.warnings)
        validator.validate_pair(
            plan.relative.as_posix(),
            plan.backup,
            plan.staged,
            report,
        )
        plan.changed_trans_count = report.changed_trans - changed_before
        plan.warnings = list(report.warnings[warnings_before:])
    return report


def atomic_write_bytes(target: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{target.name}.restore.",
        suffix=".codex-tmp",
        dir=target.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        staged = Path(handle.name)
    try:
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()


def commit_plans(plans: list[PlannedFile], staged_state: Path, state_path: Path) -> None:
    committed: list[PlannedFile] = []
    try:
        for plan in plans:
            if plan.staged is None:
                raise ApplyError(f"{plan.relative.as_posix()}: 缺少待提交文件")
            os.replace(plan.staged, plan.target)
            committed.append(plan)
        os.replace(staged_state, state_path)
    except (OSError, ApplyError) as exc:
        rollback_errors: list[str] = []
        for plan in reversed(committed):
            try:
                atomic_write_bytes(plan.target, plan.source.raw)
            except OSError as rollback_exc:
                rollback_errors.append(f"{plan.relative.as_posix()}: {rollback_exc}")
        detail = f"提交失败并已回滚: {exc}"
        if rollback_errors:
            detail += "；回滚失败: " + "；".join(rollback_errors)
        raise ApplyError(detail) from exc


def write_and_print_report(
    report: validator.ValidationReport,
    report_path: Path,
    max_details: int,
) -> None:
    written_report: Path | None = None
    try:
        validator.write_full_report(report, report_path)
        written_report = report_path
    except OSError as exc:
        print(f"WARN report_write: {exc}")
    validator.print_compact_report(report, max_details, written_report)


def main() -> int:
    validator.configure_utf8_output()
    args = parse_args()
    report = validator.ValidationReport()
    try:
        root = discover_repository_root(args.root)
    except ApplyError as exc:
        report.error(str(exc))
        validator.print_compact_report(report, args.max_details, None)
        return 1
    task_paths = validator.resolve_task_paths(root, args.task_id)
    input_path = task_paths.updates
    backup_root = task_paths.backup
    report_path = task_paths.report
    state_path = task_paths.state
    plans: list[PlannedFile] = []
    staged_state: Path | None = None
    try:
        payload = read_payload(input_path)
        plans = build_plans(root, backup_root, payload)
        if state_path in {input_path, report_path} or any(
            state_path == plan.target for plan in plans
        ):
            raise ApplyError("成功文件清单路径不得与更新 JSON、验证报告或目标 CSV 相同")
        if state_path.exists() and not state_path.is_file():
            raise ApplyError(f"成功文件清单路径不是文件: {state_path}")
        state = load_state(state_path, args.task_id)
        bind_recorded_backups(root, state, plans)
        create_backups(plans)
        stage_outputs(plans)
        report = validate_staged(plans)
        if report.errors:
            cleanup_staged(plans)
            write_and_print_report(report, report_path, args.max_details)
            return 1
        state_content = build_updated_state(root, state, plans)
        staged_state = stage_state(state_path, state_content)
        commit_plans(plans, staged_state, state_path)
    except (ApplyError, OSError) as exc:
        cleanup_staged(plans)
        cleanup_staged_state(staged_state)
        report.error(str(exc))
        write_and_print_report(report, report_path, args.max_details)
        return 1
    cleanup_staged(plans)
    cleanup_staged_state(staged_state)
    write_and_print_report(report, report_path, args.max_details)
    print(f"APPLIED files: {len(plans)}")
    print(f"APPLIED requested updates: {sum(plan.requested_updates for plan in plans)}")
    print(f"STATE updated: {state_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
