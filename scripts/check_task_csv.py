"""Check the four-column task CSV; content and asset review remain separate."""

import argparse
import csv
import io
import json
import re
from collections import Counter
from pathlib import Path


HEADERS = ["任务", "难度", "是否需要多轮交互", "任务来源"]
LEVELS = {"基础", "进阶", "复杂", "综合挑战"}
SCRIPTED_TURN = re.compile(r"(?:^|\n)\s*(?:首轮|第[一二三四五六七八九十0-9]+轮)\s*(?:[（(][^\n）)]*[）)])?\s*[:：]")
THERMAL = re.compile(r"热分析|热仿真|散热优化|稳态导热|瞬态导热|热传导(?:分析|仿真)|\bthermal\s+(?:analysis|simulation)\b", re.IGNORECASE)


def check(path: Path, expected: int | None, allow_thermal: bool) -> dict:
    """Return format errors and counts without modifying the input file."""
    errors = []
    try:
        raw = path.read_bytes()
        if not raw.startswith(b"\xef\xbb\xbf"):
            errors.append("文件需要 UTF-8 BOM 编码。")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True))
    except (OSError, UnicodeError, csv.Error) as error:
        return {"ok": False, "errors": [f"无法读取 CSV：{error}"]}
    if not rows or rows[0] != HEADERS:
        return {"ok": False, "errors": errors + [f"表头必须依次为：{','.join(HEADERS)}"]}
    records = rows[1:]
    if not records:
        errors.append("CSV 至少需要一条任务。")
    if expected is not None and len(records) != expected:
        errors.append(f"任务数为 {len(records)}，预期 {expected}。")
    seen = {}
    levels = Counter()
    interactions = Counter()
    for number, row in enumerate(records, 1):
        if len(row) != 4:
            errors.append(f"第 {number} 条包含 {len(row)} 列，需要 4 列。")
            continue
        task, level, multi, source = row
        if any(not value.strip() for value in row):
            errors.append(f"第 {number} 条存在空字段。")
        if level not in LEVELS:
            errors.append(f"第 {number} 条难度值无效：{level}")
        if multi not in {"是", "否"}:
            errors.append(f"第 {number} 条多轮值必须是“是”或“否”。")
        if SCRIPTED_TURN.search(task):
            errors.append(f"第 {number} 条含预写对话轮次，请改为用户初次发言。")
        if not allow_thermal and THERMAL.search(task):
            errors.append(f"第 {number} 条包含默认范围之外的热相关任务。")
        normalized = re.sub(r"\s+", " ", task).strip()
        if normalized in seen:
            errors.append(f"第 {number} 条与第 {seen[normalized]} 条题面重复。")
        else:
            seen[normalized] = number
        levels[level] += 1
        interactions[multi] += 1
    return {"ok": not errors, "count": len(records), "difficulty": dict(levels), "interaction": dict(interactions), "errors": errors}


def main() -> int:
    """Print a JSON report and return a nonzero exit status for invalid input."""
    parser = argparse.ArgumentParser(description="检查任务问题生成skill的四列 CSV。")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--allow-thermal", action="store_true")
    args = parser.parse_args()
    if args.expected_count is not None and args.expected_count < 1:
        parser.error("--expected-count 必须为正整数。")
    report = check(args.csv_path, args.expected_count, args.allow_thermal)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
