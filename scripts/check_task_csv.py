"""Check task metadata and paired preparation/evaluation files without executing them."""

import argparse
import csv
import io
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath


HEADERS = ["任务", "难度", "是否需要多轮交互", "任务来源", "任务处理模态", "上传文件", "rubric", "验收方式", "环境准备"]
LEVELS = {"基础", "进阶", "复杂", "综合挑战"}
MODALITIES = {"自然语言", "图片", "自然语言+图片"}
METHODS = {"程序验收", "额外模型主观评价", "混合验收"}
TASK_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SCRIPTED_TURN = re.compile(r"(?:^|\n)\s*(?:首轮|第[一二三四五六七八九十0-9]+轮)\s*(?:[（(][^\n）)]*[）)])?\s*[:：]")
THERMAL = re.compile(r"热分析|热仿真|散热优化|稳态导热|瞬态导热|热传导(?:分析|仿真)|\bthermal\s+(?:analysis|simulation)\b", re.IGNORECASE)


def task_asset_id(directory: Path, rubric: str, method: str, preparation: str, number: int, errors: list[str]) -> str | None:
    """Check declared preparation/evaluation files and return their common task ID without executing them."""
    required = {
        "程序验收": {"程序入口"},
        "额外模型主观评价": {"模型提示词"},
        "混合验收": {"程序入口", "模型提示词"},
    }[method] | {"环境脚本"}
    task_ids = set()
    for label, folder, filename, text, field in [
        ("环境脚本", "environments", "prepare.py", preparation, "环境准备"),
        ("程序入口", "evaluators", "check.py", rubric, "rubric"),
        ("模型提示词", "evaluators", "judge_prompt.txt", rubric, "rubric"),
    ]:
        entries = re.findall(rf"^[ \t]*{label}[：:][ \t]*([^\n]+)$", text, re.MULTILINE)
        if not entries:
            if label in required:
                errors.append(f"第 {number} 条需要在{field}中注明{label}。")
            continue
        if len(entries) != 1:
            errors.append(f"第 {number} 条{label}只能声明一次。")
            continue
        relative = entries[0].strip()
        match = re.fullmatch(rf"{folder}/({TASK_ID.pattern})/{re.escape(filename)}", relative)
        if match is None:
            errors.append(f"第 {number} 条{label}需为 {folder}/<task-id>/{filename}，任务 ID 使用小写字母、数字和连字符。")
            continue
        task_ids.add(match.group(1))
        target = directory / relative
        try:
            if not target.resolve().is_relative_to(directory.resolve()):
                errors.append(f"第 {number} 条{label}不能指向 CSV 所在目录之外：{relative}")
                continue
            content = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            errors.append(f"第 {number} 条无法读取{label} {relative}：{error}")
            continue
        if not content.strip():
            errors.append(f"第 {number} 条{label}文件为空：{relative}")
        elif filename.endswith(".py"):
            try:
                compile(content, str(target), "exec")
            except (SyntaxError, ValueError) as error:
                errors.append(f"第 {number} 条{label}语法无效 {relative}：{error}")
    if len(task_ids) > 1:
        errors.append(f"第 {number} 条配套文件的任务 ID 不一致。")
    return next(iter(task_ids)) if len(task_ids) == 1 else None


def check(path: Path, expected: int | None, allow_thermal: bool) -> dict:
    """Return CSV and paired-file diagnostics without running preparation, rollouts or model calls."""
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
    used_ids = {}
    levels, interactions, modalities, methods = Counter(), Counter(), Counter(), Counter()
    for number, row in enumerate(records, 1):
        if len(row) != len(HEADERS):
            errors.append(f"第 {number} 条包含 {len(row)} 列，需要 {len(HEADERS)} 列。")
            continue
        task, level, multi, source, modality, uploads, rubric, method, preparation = row
        if any(not value.strip() for value in row):
            errors.append(f"第 {number} 条存在空字段。")
        if level not in LEVELS:
            errors.append(f"第 {number} 条难度值无效：{level}")
        if multi not in {"是", "否"}:
            errors.append(f"第 {number} 条多轮值必须是“是”或“否”。")
        if modality not in MODALITIES:
            errors.append(f"第 {number} 条任务处理模态无效：{modality}")
        if method not in METHODS:
            errors.append(f"第 {number} 条验收方式无效：{method}")
        try:
            files = json.loads(uploads)
        except json.JSONDecodeError:
            errors.append(f"第 {number} 条上传文件必须是 JSON 路径数组，无附件填 []。")
        else:
            if not isinstance(files, list) or any(not isinstance(file, str) or not file.strip() for file in files):
                errors.append(f"第 {number} 条上传文件必须是非空路径字符串组成的数组，无附件填 []。")
            else:
                for file in files:
                    relative = PurePosixPath(file)
                    if "\\" in file or relative.is_absolute() or PureWindowsPath(file).drive or ".." in relative.parts or not relative.name:
                        errors.append(f"第 {number} 条上传文件需使用多模态数据目录内的相对文件路径和 / 分隔符：{file}")
                if len(set(files)) != len(files):
                    errors.append(f"第 {number} 条上传文件包含重复路径。")
        if SCRIPTED_TURN.search(task):
            errors.append(f"第 {number} 条含预写对话轮次，请改为用户初次发言。")
        if not allow_thermal and THERMAL.search(task):
            errors.append(f"第 {number} 条包含默认范围之外的热相关任务。")
        normalized = re.sub(r"\s+", " ", task).strip()
        if normalized in seen:
            errors.append(f"第 {number} 条与第 {seen[normalized]} 条题面重复。")
        else:
            seen[normalized] = number
        if method in METHODS:
            task_id = task_asset_id(path.parent, rubric, method, preparation, number, errors)
            if task_id is not None:
                if task_id in used_ids:
                    errors.append(f"第 {number} 条与第 {used_ids[task_id]} 条共用了任务 ID {task_id}。")
                else:
                    used_ids[task_id] = number
        levels[level] += 1
        interactions[multi] += 1
        modalities[modality] += 1
        methods[method] += 1
    return {"ok": not errors, "count": len(records), "difficulty": dict(levels), "interaction": dict(interactions), "modalities": dict(modalities), "evaluation_methods": dict(methods), "errors": errors}


def main() -> int:
    """Print a JSON report and fail when task metadata or paired preparation/evaluation files are invalid."""
    parser = argparse.ArgumentParser(description="检查九列 CSV、上传文件路径及配套环境脚本、验收程序和评审提示词。")
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
