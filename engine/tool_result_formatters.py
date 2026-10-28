from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

FormattedResult = tuple[str, str] | None


@dataclass(frozen=True)
class ToolResultFormatContext:
    """Context supplied to structure-based tool-result formatters."""

    tool_name: str = ""
    tool_spec: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolResultFormatter:
    """A single-pass formatter, with explicit context-aware registration."""

    id: str
    priority: int
    format_fn: Callable[..., FormattedResult]
    needs_context: bool = False

    def format(self, result: Any, ctx: ToolResultFormatContext) -> FormattedResult:
        if self.needs_context:
            return self.format_fn(result, ctx)
        if not isinstance(result, dict):
            return None
        return self.format_fn(result)


_FORMATTERS: list[ToolResultFormatter] = []


def register_result_formatter(formatter: ToolResultFormatter) -> None:
    """Register or replace an entry, ordered by priority and then id."""
    formatter_id = formatter.id.strip()
    if not formatter_id:
        raise ValueError("formatter id is required")
    existing_index = next((i for i, item in enumerate(_FORMATTERS) if item.id == formatter.id), None)
    if existing_index is None:
        _FORMATTERS.append(formatter)
    else:
        _FORMATTERS[existing_index] = formatter
    _FORMATTERS.sort(key=lambda item: (item.priority, item.id))


def format_tool_result_by_structure(
    result: Any,
    ctx: ToolResultFormatContext | None = None,
) -> FormattedResult:
    """Return the first structural match; formatter defects are not mismatches."""
    safe_ctx = ctx or ToolResultFormatContext()
    for formatter in _FORMATTERS:
        rendered = formatter.format(result, safe_ctx)
        if rendered is not None:
            return rendered
    return None


def json_fallback(result: Any) -> tuple[str, str]:
    """Serialize arbitrary tool payloads, retaining text for non-JSON values."""
    try:
        return "json", json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return "json", str(result)


def _data_dict(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data")
    return data if isinstance(data, dict) else None


def _is_read_file_entry(entry: Any) -> bool:
    # Batch wrappers overlap; only documented file entries qualify here.
    if not isinstance(entry, dict):
        return False
    if entry.get("success") is False:
        return "path" in entry and "error" in entry
    return entry.get("type") in ("text", "multimodal", "binary") and "path" in entry


def _is_search_match_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return isinstance(entry.get("file"), str) and "line" in entry and isinstance(entry.get("match"), str)


def _is_list_dir_section(section: Any) -> bool:
    if not isinstance(section, dict):
        return False
    entries = section.get("entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("name"), str):
            return False
        if entry.get("type") not in ("directory", "file"):
            return False
    return True


def _format_display_directive(result: Any, ctx: ToolResultFormatContext) -> FormattedResult:
    if not isinstance(result, dict):
        return None
    display = result.get("_display")
    if isinstance(display, str):
        return "text", display
    if isinstance(display, dict):
        raw_value = display.get("raw")
        if raw_value is None:
            raw_value = display.get("text")
        if raw_value is None:
            raw_value = display.get("message")
        if isinstance(raw_value, str):
            format_value = display.get("format")
            return format_value if isinstance(format_value, str) and format_value else "text", raw_value

    format_id = result.get("_format")
    if not isinstance(format_id, str) or not format_id.strip():
        return None
    format_id = format_id.strip()
    if format_id == "json":
        return json_fallback(result)
    if format_id == "text":
        return _format_primary_text_result(result)
    return _render_formatter_by_id(format_id, result, ctx)


def _format_spec_result_format(result: Any, ctx: ToolResultFormatContext) -> FormattedResult:
    spec = ctx.tool_spec
    if spec is None:
        return None
    format_id = spec.get("result_format")
    if not isinstance(format_id, str) or not format_id.strip():
        return None
    format_id = format_id.strip()
    if format_id == "json":
        return json_fallback(result)
    if format_id == "text":
        if isinstance(result, dict):
            formatted = _format_primary_text_result(result)
            if formatted is not None:
                return formatted
        return "text", str(result)
    return _render_formatter_by_id(format_id, result, ctx)


def _render_formatter_by_id(
    formatter_id: str,
    result: Any,
    ctx: ToolResultFormatContext,
    *,
    skip_ids: set[str] | None = None,
) -> FormattedResult:
    # Directives route to leaf formatters, never to one another.
    if formatter_id in {"display_directive", "spec_result_format"}:
        return None
    if skip_ids is not None and formatter_id in skip_ids:
        return None
    for formatter in _FORMATTERS:
        if formatter.id == formatter_id:
            return formatter.format(result, ctx)
    return None


def _format_read_file_batch(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    if not all(_is_read_file_entry(entry) for entry in results):
        return None
    parts: list[str] = []
    for entry in results:
        path = str(entry.get("path") or "")
        if entry.get("success") and entry.get("type") == "text":
            content = entry.get("content")
            if isinstance(content, str) and content:
                parts.append(f"── {path} ──\n{content}")
        elif entry.get("success") and entry.get("type") == "multimodal":
            parts.append(f"── {path} ── [image: {entry.get('mimeType', '?')}, {entry.get('size', 0)} bytes]")
        elif entry.get("success") and entry.get("type") == "binary":
            parts.append(f"── {path} ── [binary: {entry.get('size', 0)} bytes]")
        elif entry.get("success") is False:
            parts.append(f"── {path} ── ERROR: {entry.get('error', 'unknown')}")
    if not parts:
        return None
    return "text", "\n".join(parts)


def _format_search_matches(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        return None
    results = data.get("results")
    count = data.get("count")
    truncated = data.get("truncated")
    if not isinstance(results, list) or not isinstance(count, int) or not isinstance(truncated, bool):
        return None
    if not all(_is_search_match_entry(entry) for entry in results):
        return None
    parts = [f"{count} results found{' (truncated)' if truncated else ''}:"]
    for item in results:
        file_path = item["file"] or "?"
        line_no = item["line"]
        match_text = item["match"].replace("\n", "\\n")
        parts.append("")
        parts.append(f"{file_path}:{line_no} | {match_text}")
        context = item.get("context")
        if context is not None:
            for context_line in str(context).splitlines():
                parts.append(f"  {context_line}")
    return "text", "\n".join(parts)


def _format_search_replace(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        return None
    files_modified = data.get("filesModified")
    total_replacements = data.get("totalReplacements")
    if not isinstance(files_modified, int) or not isinstance(total_replacements, int):
        return None
    parts = [f"{files_modified} files modified, {total_replacements} replacements total"]
    results = data.get("results")
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("file")
            replacements = entry.get("replacements")
            if isinstance(file_path, str) and isinstance(replacements, int):
                parts.append(f"{file_path}: {replacements} replacements")
    return "text", "\n".join(parts)


def _format_list_dir_tree(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        return None
    total_files = data.get("totalFiles")
    total_dirs = data.get("totalDirs")
    results = data.get("results")
    if not isinstance(total_files, int) or not isinstance(total_dirs, int) or not isinstance(results, list):
        return None
    if not all(_is_list_dir_section(section) for section in results):
        return None
    parts: list[str] = []
    for section in results:
        path = str(section.get("path") or "?")
        if section.get("success") is False:
            parts.append(f"── {path} ── ERROR: {section.get('error', 'unknown')}")
            parts.append("")
            continue
        file_count = section.get("fileCount", 0)
        dir_count = section.get("dirCount", 0)
        parts.append(f"── {path} ── ({file_count} files, {dir_count} dirs)")
        for entry in section["entries"]:
            name = entry["name"]
            if entry["type"] == "directory":
                if not name.endswith("/"):
                    name += "/"
                parts.append(f"  📁 {name}")
            else:
                parts.append(f"  📄 {name}")
        parts.append("")
    while parts and parts[-1] == "":
        parts.pop()
    if parts:
        parts.append("")
    parts.append(f"Total: {total_files} files, {total_dirs} dirs")
    return "text", "\n".join(parts)


def _format_command_output(result: dict[str, Any]) -> FormattedResult:
    output = result.get("output")
    if "returncode" not in result or not isinstance(output, str):
        return None
    return "text", f"returncode={result['returncode']}\n{output}"


def _format_apply_diff_summary(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        return None
    path = data.get("path") or data.get("file")
    if not isinstance(path, str) or not path:
        return None
    if not any(key in data for key in ("diffCount", "appliedCount", "failedCount", "applied", "rejected", "rejectedCount")):
        return None
    applied = data.get("applied", data.get("appliedCount", "?"))
    rejected = data.get("rejected", data.get("rejectedCount", data.get("failedCount", "?")))
    return "text", f"apply_diff on {path}: {applied} applied, {rejected} rejected"


def _format_write_file_summary(result: dict[str, Any]) -> FormattedResult:
    if result.get("ok") is False or result.get("success") is False:
        return None
    path = result.get("path")
    if not (isinstance(path, str) and isinstance(result.get("bytes"), int)):
        # Preserve the strict legacy wrapper without classifying arbitrary data.path.
        data = _data_dict(result)
        if data is None or set(data) != {"path"}:
            return None
        nested_path = data["path"]
        if not isinstance(nested_path, str):
            return None
        if not isinstance(path, str):
            path = nested_path
    if not path:
        return None
    return "text", f"File written: {path}"


def _mcp_content_parts_from_result(result: dict[str, Any]) -> list[Any] | None:
    content = result.get("content")
    if isinstance(content, list):
        return content
    nested = result.get("result")
    if isinstance(nested, dict):
        content = nested.get("content")
        if isinstance(content, list):
            return content
    return None


def _is_mcp_content_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in ("text", "image", "resource")


def _format_mcp_content_parts(result: dict[str, Any]) -> FormattedResult:
    content = _mcp_content_parts_from_result(result)
    if not content or not all(_is_mcp_content_part(part) for part in content):
        return None
    parts: list[str] = []
    for part in content:
        part_type = part["type"]
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif part_type == "image":
            path = part.get("path")
            mime = part.get("mimeType") or part.get("mime_type") or part.get("mime") or "image"
            if isinstance(path, str) and path:
                parts.append(f"[image: {path}]")
            else:
                data = part.get("data")
                size_text = f", {len(data)} base64 chars" if isinstance(data, str) else ""
                parts.append(f"[image: {mime}{size_text}]")
        elif part_type == "resource":
            resource = part.get("resource")
            uri = resource.get("uri") if isinstance(resource, dict) else part.get("uri")
            if isinstance(uri, str) and uri:
                parts.append(f"[resource: {uri}]")
            else:
                parts.append(json_fallback(part)[1])
    if not parts:
        return None
    return "text", "\n".join(parts)


def _attachment_entries(result: dict[str, Any], data: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for attachments in (data.get("attachments"), result.get("attachments")):
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    path = attachment.get("path")
                    if isinstance(path, str) and path and path not in paths:
                        paths.append(path)
                elif isinstance(attachment, str) and attachment and attachment not in paths:
                    paths.append(attachment)
    for source in (data, result):
        for key in ("path", "image_path", "audio_path", "video_path"):
            path = source.get(key)
            if isinstance(path, str) and path.startswith("data/") and path not in paths:
                paths.append(path)
    return paths


def _format_attachment_result(result: dict[str, Any]) -> FormattedResult:
    data = _data_dict(result)
    if data is None:
        data = {}
    paths = _attachment_entries(result, data)
    if not paths:
        return None
    parts: list[str] = []
    for source in (data, result):
        for key in ("text", "description", "message"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
        if parts:
            break
    parts.append("Attachments:")
    parts.extend(f"- {path}" for path in paths)
    return "text", "\n".join(parts)


def _format_primary_text_result(result: dict[str, Any]) -> FormattedResult:
    for key in ("text", "description", "message", "output", "result"):
        value = result.get(key)
        if isinstance(value, str):
            return "text", value
    return None


_DEFAULT_FORMATTERS = [
    ToolResultFormatter("display_directive", 10, _format_display_directive, needs_context=True),
    ToolResultFormatter("spec_result_format", 20, _format_spec_result_format, needs_context=True),
    ToolResultFormatter("read_file_batch", 100, _format_read_file_batch),
    ToolResultFormatter("search_matches", 110, _format_search_matches),
    ToolResultFormatter("search_replace", 111, _format_search_replace),
    ToolResultFormatter("list_dir_tree", 120, _format_list_dir_tree),
    ToolResultFormatter("command_output", 130, _format_command_output),
    ToolResultFormatter("apply_diff_summary", 140, _format_apply_diff_summary),
    ToolResultFormatter("write_file_summary", 150, _format_write_file_summary),
    ToolResultFormatter("mcp_content_parts", 200, _format_mcp_content_parts),
    ToolResultFormatter("attachment_result", 210, _format_attachment_result),
    ToolResultFormatter("primary_text_result", 300, _format_primary_text_result),
]

for _formatter in _DEFAULT_FORMATTERS:
    register_result_formatter(_formatter)
