#!/usr/bin/env python3
"""迁移已完成任务：从 Active TASK 删除 → 插入 Recently Completed 行 → 刷新时间戳。

用法：
    python pm_finish_task.py --task P0-T2 --summary "Migration 执行通过"
    python pm_finish_task.py --task P0-T2 --summary "..." --pr 42
    python pm_finish_task.py --task P0-T2 --summary "..." --pr 42 --devlog --slug p0-t2-migration
    python pm_finish_task.py --task P0-T2 --summary "..." --phase --tools 3 --tests 5
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# .opencode/skills/pm-workflow-finish/ → repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TASK_FILE = PROJECT_ROOT / "docs" / "project_tasks.md"
DEFAULT_DEVLOG_FILE = PROJECT_ROOT / "docs" / "development_log.md"


def _find_section_boundaries(lines: list[str], heading: str) -> tuple[int, int]:
    """Return (start_line, end_line) for a ##-level section, 0-indexed. end_line is exclusive."""
    start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == heading:
            start = i
            break
    if start == -1:
        raise ValueError(f"找不到 section: {heading}")

    # Find next ## heading after start
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^##\s", lines[i]):
            end = i
            break
    return start, end


_TASK_ROW_PATTERN = re.compile(r"^\|\s*\**({})\**\s*\|")


def _find_task_row(lines: list[str], task_id: str, section_start: int, section_end: int) -> int | None:
    """Find task row in Active TASK table. Returns line index or None."""
    pattern = re.compile(_TASK_ROW_PATTERN.pattern.format(re.escape(task_id)))
    for i in range(section_start, section_end):
        if pattern.match(lines[i].strip()):
            return i
    return None


def _update_timestamp(lines: list[str]) -> None:
    """Update the 'Last updated:' line at the top of the file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for i, line in enumerate(lines):
        if line.startswith("Last updated:"):
            lines[i] = f"Last updated: {now}\n"
            return


def _insert_recently_completed(lines: list[str], task_id: str, summary: str, pr: str | None) -> None:
    """Insert a new row into the Recently Completed table."""
    now = datetime.now().strftime("%Y-%m-%d")
    commit = f"PR #{pr}" if pr else "待提交"
    # Escape pipe characters in summary to avoid breaking markdown table
    safe_summary = summary.replace("|", "\\|")
    new_row = f"| {now} | {task_id} | {commit} | {safe_summary} |\n"

    # Find the Recently Completed table header separator line
    for i, line in enumerate(lines):
        if "---" in line.strip() and line.strip().startswith("|"):
            # Check that preceding line is the header
            if i > 0 and "Date" in lines[i - 1] and "Task" in lines[i - 1]:
                # Insert after separator
                lines.insert(i + 1, new_row)
                return

    raise ValueError("找不到 Recently Completed 表头")


def _remove_task_row(lines: list[str], row_idx: int) -> None:
    """Remove a single table row from Active TASK."""
    del lines[row_idx]


def _trim_recently_completed(lines: list[str], max_rows: int = 6) -> int:
    """Keep at most max_rows data rows in Recently Completed table. Returns rows trimmed."""
    rc_start, rc_end = _find_section_boundaries(lines, "## Recently Completed")
    # Find separator line (e.g. "| --- | --- | --- | --- |")
    sep_idx = None
    for i in range(rc_start, rc_end):
        stripped = lines[i].strip()
        if stripped.startswith("|") and "---" in stripped:
            sep_idx = i
            break
    if sep_idx is None:
        return 0
    # Count data rows from sep_idx+1 until non-table line or section end
    data_start = sep_idx + 1
    data_end = data_start
    while data_end < rc_end and lines[data_end].strip().startswith("|"):
        data_end += 1
    data_rows = data_end - data_start
    if data_rows > max_rows:
        del lines[data_start + max_rows : data_end]
        return data_rows - max_rows
    return 0


def _slug_from_task(task_id: str) -> str:
    """Derive devlog slug from task ID: BL-PHASE2-SWEEP → bl-phase2-sweep."""
    return task_id.lower().replace(" ", "-").replace("_", "-")


def _write_devlog(devlog_path: Path, task_id: str, slug: str | None, summary: str, pr: str | None) -> bool:
    """Insert a new row at the top of development_log.md table.

    Returns True on success, False on failure (file missing, header not found).
    Auto-inserts a missing |---|---| separator to keep the table valid.
    Uses atomic write (tmp + os.replace) to avoid partial-file corruption.
    """
    if not devlog_path.exists():
        print(f"   错误：devlog 文件不存在: {devlog_path}", file=sys.stderr)
        return False

    with open(devlog_path, encoding="utf-8") as f:
        lines = f.readlines()

    date = datetime.now().strftime("%Y-%m-%d")
    final_slug = slug or _slug_from_task(task_id)
    safe_summary = summary.replace("|", "\\|")
    commit_text = f"[#{pr}](https://github.com/aykgb/xidi-minimal/pull/{pr})" if pr else "待提交"
    new_row = f"| {date} | {final_slug} | {safe_summary} | {commit_text} |\n"

    # Find the header line (contains both "Date" and "Slug")
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and "Date" in stripped and "Slug" in stripped:
            header_idx = i
            break
    if header_idx is None:
        print("   错误：devlog 找不到表头行（| Date | Slug | ... |）", file=sys.stderr)
        return False

    # Ensure separator line exists immediately after the header; auto-insert if missing
    if header_idx + 1 >= len(lines) or "---" not in lines[header_idx + 1]:
        header_line = lines[header_idx].rstrip("\n")
        n_cols = max(header_line.count("|") - 1, 1)
        sep_line = "| " + " | ".join(["---"] * n_cols) + " |\n"
        lines.insert(header_idx + 1, sep_line)
        print(f"   ⚠ devlog 缺 separator，已自动补充（{n_cols} 列）", file=sys.stderr)

    sep_idx = header_idx + 1
    lines.insert(sep_idx + 1, new_row)

    # Atomic write: write to .tmp first, then os.replace
    tmp_path = devlog_path.with_name(devlog_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp_path, devlog_path)

    print(f"   devlog 已更新: {date} | {final_slug}")
    return True


def _update_phase_status(lines: list[str], tools: int | None, tests: int | None) -> None:
    """Update Tools/Tests counts in Current Phase section."""
    for i, line in enumerate(lines):
        if line.startswith("- **Tools**:"):
            if tools is not None:
                lines[i] = re.sub(r"Tools\*\*: \d+", f"Tools**: {tools}", line)
        elif line.startswith("- **Tests**:"):
            if tests is not None:
                lines[i] = re.sub(r"Tests\*\*: \d+", f"Tests**: {tests}", line)


def _update_status_summary(lines: list[str], text: str) -> None:
    """Replace the '> 当前仓库状态：...' blockquote line."""
    for i, line in enumerate(lines):
        if line.startswith("> 当前仓库状态："):
            lines[i] = f"> 当前仓库状态：{text}\n"
            return


def _update_phase_status_text(lines: list[str], text: str) -> None:
    """Replace the '- **Status**: ...' line in Current Phase section."""
    for i, line in enumerate(lines):
        if line.startswith("- **Status**:"):
            lines[i] = f"- **Status**: {text}\n"
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移已完成任务到 Recently Completed")
    parser.add_argument("--task", required=True, help="任务 ID，如 P0-T2")
    parser.add_argument("--summary", required=True, help="完成摘要")
    parser.add_argument("--pr", default=None, help="PR 编号")
    parser.add_argument("--devlog", action="store_true", help="同步更新 docs/development_log.md")
    parser.add_argument("--slug", type=str, default=None, help="devlog slug（默认从 task ID 推导）")
    parser.add_argument("--tools", type=int, default=None, help="更新工具数量")
    parser.add_argument("--tests", type=int, default=None, help="更新测试数量")
    parser.add_argument("--status-summary", type=str, default=None, help="更新仓库状态摘要（> 当前仓库状态：...）")
    parser.add_argument("--phase-status", type=str, default=None, help="更新 Phase Status 行（- **Status**: ...）")
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_TASK_FILE,
        help="TASK 文件路径（默认 docs/project_tasks.md）",
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"错误：找不到 {args.file}", file=sys.stderr)
        sys.exit(1)

    with open(args.file, encoding="utf-8") as f:
        lines = f.readlines()

    # 1. Locate Active TASK section
    active_start, active_end = _find_section_boundaries(lines, "## Active TASK")

    # 2. Find the task row in the table
    row_idx = _find_task_row(lines, args.task, active_start, active_end)
    if row_idx is None:
        print(f"错误：在 Active TASK 中找不到 {args.task}", file=sys.stderr)
        sys.exit(1)

    # 3. Remove task row from Active TASK table
    _remove_task_row(lines, row_idx)

    # 4. Insert into Recently Completed
    _insert_recently_completed(lines, args.task, args.summary, args.pr)

    # 5. Trim Recently Completed to max 6 rows
    trimmed = _trim_recently_completed(lines)
    if trimmed:
        print(f"   Recently Completed 裁剪 {trimmed} 行（保持 ≤6）")

    # 6. Update timestamp
    _update_timestamp(lines)

    # 7. Update phase counts if needed
    _update_phase_status(lines, args.tools, args.tests)

    # 8. Update status text if provided
    if args.status_summary:
        _update_status_summary(lines, args.status_summary)
    if args.phase_status:
        _update_phase_status_text(lines, args.phase_status)

    # 9. Sync devlog if requested
    if args.devlog:
        if not _write_devlog(DEFAULT_DEVLOG_FILE, args.task, args.slug, args.summary, args.pr):
            sys.exit(1)

    with open(args.file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"✅ {args.task} 已迁移：Active TASK → Recently Completed")
    if args.tools is not None or args.tests is not None:
        print(f"   Tools={args.tools}, Tests={args.tests} 已更新")


if __name__ == "__main__":
    main()
