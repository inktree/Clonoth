"""Behavior contracts for single-pass tool-result formatting and routing."""
from __future__ import annotations

import json

import pytest

from engine import tool_result_formatters as formatters
from engine.tool_result_formatters import (
    ToolResultFormatContext,
    ToolResultFormatter,
    format_tool_result_by_structure,
    register_result_formatter,
)
from engine.tool_step import result_to_raw


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(formatters, "_FORMATTERS", list(formatters._DEFAULT_FORMATTERS))


@pytest.mark.parametrize("result,spec,expected", [
    pytest.param(
        {"_display": {"format": "markdown", "raw": "**ready**"}}, None,
        ("markdown", "**ready**"), id="display_directive",
    ),
    pytest.param(
        {"answer": 42}, {"result_format": "text"},
        ("text", "{'answer': 42}"), id="spec_result_format",
    ),
    pytest.param(
        {"data": {"results": [
            {"success": True, "type": "text", "path": "a.py", "content": "print(1)"},
            {"success": True, "type": "multimodal", "path": "b.png", "mimeType": "image/png", "size": 12},
            {"success": True, "type": "binary", "path": "c.bin", "size": 3},
            {"success": False, "path": "missing", "error": "not found"},
        ]}}, None,
        ("text", "── a.py ──\nprint(1)\n── b.png ── [image: image/png, 12 bytes]\n"
         "── c.bin ── [binary: 3 bytes]\n── missing ── ERROR: not found"),
        id="read_file_batch",
    ),
    pytest.param(
        {"data": {"results": [{"file": "a.py", "line": 7, "match": "one\ntwo", "context": "before\nafter"}],
                  "count": 1, "truncated": True}}, None,
        ("text", "1 results found (truncated):\n\na.py:7 | one\\ntwo\n  before\n  after"),
        id="search_matches",
    ),
    pytest.param(
        {"data": {"filesModified": 1, "totalReplacements": 2,
                  "results": [{"file": "a.py", "replacements": 2}]}}, None,
        ("text", "1 files modified, 2 replacements total\na.py: 2 replacements"),
        id="search_replace",
    ),
    pytest.param(
        {"data": {"totalFiles": 1, "totalDirs": 1, "results": [
            {"path": ".", "success": True, "fileCount": 1, "dirCount": 1,
             "entries": [{"name": "src", "type": "directory"}, {"name": "a.py", "type": "file"}]},
            {"path": "private", "success": False, "error": "denied", "entries": []},
        ]}}, None,
        ("text", "── . ── (1 files, 1 dirs)\n  📁 src/\n  📄 a.py\n\n"
         "── private ── ERROR: denied\n\nTotal: 1 files, 1 dirs"),
        id="list_dir_tree",
    ),
    pytest.param(
        {"returncode": 0, "output": "done"}, None,
        ("text", "returncode=0\ndone"), id="command_output",
    ),
    pytest.param(
        {"data": {"file": "a.py", "diffCount": 1, "appliedCount": 0, "failedCount": 1}}, None,
        ("text", "apply_diff on a.py: 0 applied, 1 rejected"), id="apply_diff_summary",
    ),
    pytest.param(
        {"ok": True, "path": "a.py", "bytes": 0}, None,
        ("text", "File written: a.py"), id="write_file_summary",
    ),
    pytest.param(
        {"result": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "path": "data/pic.png"},
            {"type": "resource", "resource": {"uri": "file:///a.py"}},
        ]}}, None,
        ("text", "hello\n[image: data/pic.png]\n[resource: file:///a.py]"), id="mcp_content_parts",
    ),
    pytest.param(
        {"data": {"attachments": [{"path": "data/pic.png"}], "description": "  produced  "},
         "attachments": ["data/pic.png", {"path": "data/sound.wav"}], "message": "secondary"}, None,
        ("text", "produced\nAttachments:\n- data/pic.png\n- data/sound.wav"), id="attachment_result",
    ),
    pytest.param(
        {"text": "first", "description": "second"}, None,
        ("text", "first"), id="primary_text_result",
    ),
])
def test_default_formatter_outputs(result, spec, expected):
    ctx = ToolResultFormatContext(tool_name="external_tool", tool_spec=spec)
    assert format_tool_result_by_structure(result, ctx) == expected
    assert result_to_raw("external_tool", result, tool_spec=spec) == expected


@pytest.mark.parametrize("result,expected", [
    ({"text": "", "message": "not selected"}, ("text", "")),
    ({"_display": "", "text": "not selected"}, ("text", "")),
    ({"data": {"results": [], "count": 0, "truncated": False}}, ("text", "0 results found:")),
    ({"data": {"results": [], "totalFiles": 0, "totalDirs": 0}}, ("text", "Total: 0 files, 0 dirs")),
    ({"returncode": 0, "output": ""}, ("text", "returncode=0\n")),
    ({"data": {"filesModified": 0, "totalReplacements": 0}},
     ("text", "0 files modified, 0 replacements total")),
    ({"content": [{"type": "text", "text": ""}]}, ("text", "")),
])
def test_empty_values_are_not_mismatches(result, expected):
    assert format_tool_result_by_structure(result) == expected


@pytest.mark.parametrize("result", [
    {"data": {"results": []}},
    {"content": []},
    {"data": {"path": "a.py", "unrelated": True}},
    {"ok": False, "path": "a.py", "bytes": 1},
    {"data": {"results": [{"file": "a.py", "line": 1}], "count": 1, "truncated": False}},
    {"data": {"results": [{"entries": [{"name": "x", "type": "unknown"}]}],
              "totalFiles": 0, "totalDirs": 0}},
    {"content": [{"type": "unrecognized", "text": "not a supported MCP part"}]},
    {"data": {"results": [{"success": True, "path": "a.py", "type": []}]}},
    {"content": [{"type": []}]},
    {"data": {"results": [{"entries": [{"name": "x", "type": []}]}],
              "totalFiles": 0, "totalDirs": 0}},
])
def test_unrecognized_structures_fall_back_to_json(result):
    assert format_tool_result_by_structure(result) is None
    assert result_to_raw("external_tool", result) == (
        "json", json.dumps(result, ensure_ascii=False, indent=2),
    )


@pytest.mark.parametrize("result,expected", [
    ({"_format": "command_output", "returncode": 2, "output": "bad"}, ("text", "returncode=2\nbad")),
    ({"_format": "text", "message": "hello"}, ("text", "hello")),
    ({"_display": {"text": "hello", "format": "html"}}, ("html", "hello")),
    ({"_display": {"raw": "", "text": "not selected"}}, ("text", "")),
    ({"_format": "missing", "text": "fallback"}, ("text", "fallback")),
])
def test_display_directives_and_named_routing(result, expected):
    assert format_tool_result_by_structure(result) == expected


@pytest.mark.parametrize("spec", [None, {"result_format": "text"}])
def test_display_json_overrides_primary_text(spec):
    result = {"_format": "json", "text": "do not use plain text"}
    ctx = ToolResultFormatContext(tool_spec=spec)
    assert format_tool_result_by_structure(result, ctx) == (
        "json", json.dumps(result, ensure_ascii=False, indent=2),
    )


@pytest.mark.parametrize("result,format_id,expected", [
    (None, "text", ("text", "None")),
    ([1, 2], "text", ("text", "[1, 2]")),
    ([1, 2], "json", ("json", "[\n  1,\n  2\n]")),
    ({"returncode": 0, "output": "done"}, "command_output", ("text", "returncode=0\ndone")),
    ({"text": "fallback"}, "missing", ("text", "fallback")),
])
def test_spec_directives_preserve_scalar_and_named_formats(result, format_id, expected):
    ctx = ToolResultFormatContext(tool_spec={"result_format": format_id})
    assert format_tool_result_by_structure(result, ctx) == expected


@pytest.mark.parametrize("named", [False, True])
def test_registered_formatter_called_once(named):
    calls = []

    def format_once(result):
        calls.append(result)
        return "text", ""

    register_result_formatter(ToolResultFormatter("once", 30, format_once))
    result = {"text": "must not replace an empty successful rendering"}
    if named:
        result["_format"] = "once"
    assert format_tool_result_by_structure(result) == ("text", "")
    assert calls == [result]


def test_mismatches_continue_without_repeating_callbacks():
    calls = []

    def decline(result):
        calls.append("decline")
        return None

    def accept(result):
        calls.append("accept")
        return "text", "accepted"

    register_result_formatter(ToolResultFormatter("decline", 30, decline))
    register_result_formatter(ToolResultFormatter("accept", 31, accept))
    assert format_tool_result_by_structure({}) == ("text", "accepted")
    assert calls == ["decline", "accept"]


def test_context_is_only_required_by_contextual_callbacks():
    ctx = ToolResultFormatContext(tool_name="external_tool")
    calls = []

    def contextual(result, supplied_ctx):
        calls.append((result, supplied_ctx))
        return "text", supplied_ctx.tool_name

    register_result_formatter(ToolResultFormatter("contextual", 30, contextual, needs_context=True))
    assert format_tool_result_by_structure("scalar", ctx) == ("text", "external_tool")
    assert calls == [("scalar", ctx)]


@pytest.mark.parametrize("display_id,spec_id", [
    ("spec_result_format", "display_directive"),
    ("display_directive", "spec_result_format"),
])
def test_directive_dispatchers_cannot_reenter_each_other(display_id, spec_id):
    reads = []

    class CountedSpec(dict):
        def get(self, key, default=None):
            if key == "result_format":
                reads.append(key)
            return super().get(key, default)

    result = {"_format": display_id, "text": "fallback"}
    ctx = ToolResultFormatContext(tool_spec=CountedSpec(result_format=spec_id))
    assert format_tool_result_by_structure(result, ctx) == ("text", "fallback")
    # Each dispatcher gets one automatic attempt, never recursively re-enters.
    assert reads == ["result_format"]


@pytest.mark.parametrize("named", [False, True])
def test_formatter_defects_are_not_silently_treated_as_mismatches(named):
    def broken(result):
        raise RuntimeError("formatter bug")

    register_result_formatter(ToolResultFormatter("broken", 30, broken))
    result = {"text": "must not hide the bug"}
    if named:
        result["_format"] = "broken"
    with pytest.raises(RuntimeError, match="formatter bug"):
        format_tool_result_by_structure(result)


def test_registration_priority_tie_break_and_replacement():
    register_result_formatter(ToolResultFormatter("z", 1, lambda result: ("text", "z")))
    register_result_formatter(ToolResultFormatter("a", 1, lambda result: ("text", "old")))
    assert format_tool_result_by_structure({}) == ("text", "old")
    register_result_formatter(ToolResultFormatter("a", 1, lambda result: ("text", "new")))
    assert format_tool_result_by_structure({}) == ("text", "new")
    register_result_formatter(ToolResultFormatter("a", 2, lambda result: ("text", "later")))
    assert format_tool_result_by_structure({}) == ("text", "z")


def test_registration_rejects_blank_id():
    with pytest.raises(ValueError, match="formatter id is required"):
        register_result_formatter(ToolResultFormatter("  ", 1, lambda result: None))


@pytest.mark.parametrize("canonical", ["canonical result", ""])
def test_canonical_data_result_has_highest_priority(canonical):
    result = {"data": {"result": canonical}, "_display": "other text", "_format": "json"}
    assert result_to_raw("external_tool", result, tool_spec={"result_format": "json"}) == ("text", canonical)


@pytest.mark.parametrize("result,expected", [
    ({"data": {"path": "old.txt"}}, ("text", "File written: old.txt")),
    ({"data": {"path": "old.txt", "applied": 2, "rejected": 0}},
     ("text", "apply_diff on old.txt: 2 applied, 0 rejected")),
    ({"content": [{"type": "image", "mime_type": "image/png", "data": "abcd"}]},
     ("text", "[image: image/png, 4 base64 chars]")),
])
def test_legacy_shapes_and_inline_mcp_images_remain_supported(result, expected):
    assert format_tool_result_by_structure(result) == expected


def test_mcp_resource_without_uri_reuses_json_fallback():
    resource = {"type": "resource", "resource": {"text": "hello"}}
    assert format_tool_result_by_structure({"content": [resource]}) == (
        "text", json.dumps(resource, ensure_ascii=False, indent=2),
    )
    non_json_resource = {"type": "resource", "extra": {1}}
    assert format_tool_result_by_structure({"content": [non_json_resource]}) == (
        "text", str(non_json_resource),
    )


def test_non_dictionary_without_spec_keeps_json_fallback():
    assert format_tool_result_by_structure("plain") is None
    assert result_to_raw("external_tool", "plain") == ("json", '"plain"')
