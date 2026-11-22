"""Check task metadata and links to rubric details without running acceptance."""

import argparse
import csv
import io
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote


HEADERS = ["任务", "难度", "是否需要多轮交互", "任务来源", "任务处理模态", "上传文件", "rubric", "验收方式", "环境准备"]
LEVELS = {"基础", "进阶", "复杂", "综合挑战"}
MODALITIES = {"自然语言", "图片", "自然语言+图片"}
METHODS = {"程序验收", "额外模型主观评价", "混合验收"}
TASK_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SCRIPTED_TURN = re.compile(r"(?:^|\n)\s*(?:首轮|第[一二三四五六七八九十0-9]+轮)\s*(?:[（(][^\n）)]*[）)])?\s*[:：]")
THERMAL = re.compile(r"热分析|热仿真|散热优化|稳态导热|瞬态导热|热传导(?:分析|仿真)|\bthermal\s+(?:analysis|simulation)\b", re.IGNORECASE)


def rule_sections(text: str, errors: list[str]) -> dict[str, str]:
    """Read task sections and ignore headings inside fenced examples."""
    lines = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            token, suffix = marker.groups()
            if not fence_char:
                fence_char, fence_length = token[0], len(token)
            elif token[0] == fence_char and len(token) >= fence_length and not suffix.strip():
                fence_char, fence_length = "", 0
            lines.append("")
        else:
            lines.append("" if fence_char else line)
    visible = "\n".join(lines)
    headings = list(re.finditer(r"^(#{1,2})[ \t]+([^\n]+?)[ \t]*$", visible, re.MULTILINE))
    sections = {}
    for index, heading in enumerate(headings):
        task_id = heading.group(2)
        if heading.group(1) != "##" or not TASK_ID.fullmatch(task_id):
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(visible)
        if task_id in sections:
            errors.append(f"详细说明中的任务 ID 重复：{task_id}")
        sections[task_id] = visible[heading.end():end]
    return sections


def rubric_reference(summary: str) -> tuple[Path, str] | None:
    """Return the single local Markdown detail link in a rubric summary."""
    links = []
    for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", summary):
        target = target.removeprefix("<").removesuffix(">")
        if "://" in target or "#" not in target:
            continue
        filename, task_id = target.rsplit("#", 1)
        filename, task_id = unquote(filename), unquote(task_id)
        if filename.lower().endswith(".md"):
            links.append((Path(filename), task_id))
    return links[0] if len(links) == 1 else None


def check(path: Path, expected: int | None, allow_thermal: bool, rubric_md: Path | None = None) -> dict:
    """Return CSV and rubric-reference diagnostics without changing files."""
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
    details_path = (rubric_md if rubric_md is not None else path.parent / "rubric与验收说明.md").resolve()
    try:
        sections = rule_sections(details_path.read_text(encoding="utf-8-sig"), errors)
    except (OSError, UnicodeError) as error:
        sections = None
        errors.append(f"无法读取详细验收说明 {details_path}：{error}")
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
        reference = rubric_reference(rubric)
        if reference is None:
            errors.append(f"第 {number} 条 rubric 需要一个本地详细说明 MD 的任务锚点链接。")
        else:
            filename, task_id = reference
            target = (filename if filename.is_absolute() else path.parent / filename).resolve()
            if target != details_path:
                errors.append(f"第 {number} 条 rubric 链接与指定的详细说明文件不一致。")
            if not TASK_ID.fullmatch(task_id):
                errors.append(f"第 {number} 条任务锚点需使用小写字母、数字和连字符。")
            elif task_id in used_ids:
                errors.append(f"第 {number} 条与第 {used_ids[task_id]} 条共用了任务锚点 {task_id}。")
            else:
                used_ids[task_id] = number
            if sections is not None:
                section = sections.get(task_id)
                if section is None:
                    errors.append(f"第 {number} 条的任务锚点在详细说明中不存在：{task_id}")
                else:
                    declarations = re.findall(r"^验收方式[：:][ \t]*([^\n]+)$", section, re.MULTILINE)
                    if len(declarations) != 1 or declarations[0].strip() != method:
                        errors.append(f"第 {number} 条的验收方式与详细说明不一致或缺少唯一声明。")
        levels[level] += 1
        interactions[multi] += 1
        modalities[modality] += 1
        methods[method] += 1
    return {"ok": not errors, "count": len(records), "difficulty": dict(levels), "interaction": dict(interactions), "modalities": dict(modalities), "evaluation_methods": dict(methods), "rubric_md": str(details_path), "errors": errors}


def main() -> int:
    """Print a JSON report and fail when task metadata or links are invalid."""
    parser = argparse.ArgumentParser(description="检查任务问题生成skill的九列 CSV、上传文件路径与独立验收说明。")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--rubric-md", type=Path, help="默认读取 CSV 同目录的 rubric与验收说明.md。")
    parser.add_argument("--allow-thermal", action="store_true")
    args = parser.parse_args()
    if args.expected_count is not None and args.expected_count < 1:
        parser.error("--expected-count 必须为正整数。")
    report = check(args.csv_path, args.expected_count, args.allow_thermal, args.rubric_md)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
