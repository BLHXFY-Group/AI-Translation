#!/usr/bin/env python3
"""紧凑读取 GBF 剧情 CSV 的格式摘要、有效正文和译者记录。"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# 避免导入验证模块时在 Skill 目录生成 __pycache__。
sys.dont_write_bytecode = True

import validate_translation_csv as validator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读取 GBF 剧情 CSV 的有效正文和译者记录")
    parser.add_argument("file", type=Path, help="需要读取的 CSV 文件")
    return parser.parse_args()


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def main() -> int:
    validator.configure_utf8_output()
    args = parse_args()
    try:
        document = validator.read_document(args.file.resolve())
    except (OSError, ValueError) as exc:
        print(f"ERROR {exc}")
        return 1

    effective_rows = [
        row for row in document.rows[1:] if validator.is_translatable_record(document, row)
    ]
    translator_rows = [
        row for row in document.rows[1:] if validator.record_kind(document, row) == "translator"
    ]
    output_rows = [
        row
        for row in document.rows[1:]
        if validator.is_translatable_record(document, row)
        or validator.record_kind(document, row) == "translator"
    ]
    trans_index = document.indexes["trans"]
    blank_trans = sum(not validator.visible_text(row[trans_index]) for row in effective_rows)
    file_format = document.file_format
    print(f"FILE {document.path}")
    print(
        "FORMAT "
        f"encoding={file_format.encoding} "
        f"bom={yes_no(file_format.bom)} "
        f"newline={file_format.newline_style} "
        f"final_newline={yes_no(file_format.final_newline)}"
    )
    print(
        "RECORDS "
        f"total={len(document.rows) - 1} "
        f"effective={len(effective_rows)} "
        f"translator={len(translator_rows)} "
        f"blank_trans={blank_trans} "
        f"existing_trans={len(effective_rows) - blank_trans}"
    )
    print(f"BODY total={len(output_rows)}")
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(validator.REQUIRED_COLUMNS)
    indexes = [document.indexes[column] for column in validator.REQUIRED_COLUMNS]
    for row in output_rows:
        writer.writerow([row[index] for index in indexes])
    return 0


if __name__ == "__main__":
    sys.exit(main())
