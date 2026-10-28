"""Platform-independent reply text splitting and TTS markup parsing."""
from __future__ import annotations

import re


TTS_BACKENDS = {"mimo", "ryza_onnx"}
TTS_PRESETS = {"normal_cn", "normal_ja", "whisper_cn", "whisper_ja"}
_TTS_SEGMENT_RE = re.compile(
    r"<tts\b(?P<attrs>[^>]*)>(?P<html_text>[\s\S]*?)</tts>"
    r"|\[\[tts\]\](?P<legacy_text>[\s\S]*?)\[\[/tts\]\]",
    re.IGNORECASE,
)
_TTS_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_][\w-]*)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"
)


def _parse_tts_attributes(raw: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in _TTS_ATTR_RE.finditer(raw):
        name = match.group("name").lower()
        value = next(
            (item for item in (match.group("double"), match.group("single"), match.group("bare")) if item is not None),
            "",
        ).strip()
        if name in {"backend", "preset", "voice", "instruction"} and value:
            attributes[name] = value
    for name, allowed in (("backend", TTS_BACKENDS), ("preset", TTS_PRESETS)):
        if name in attributes:
            value = attributes[name].lower()
            if value in allowed:
                attributes[name] = value
            else:
                del attributes[name]
    return attributes


def extract_tts_segments(text: str) -> tuple[str, list[dict[str, str]]]:
    """Remove TTS blocks from display text and retain each segment's attributes."""
    segments: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        spoken = match.group("html_text") or match.group("legacy_text") or ""
        spoken = re.sub(r"\[SPLIT\]|\[LEFT\]", " ", spoken).strip()
        if spoken:
            segment = {"text": spoken}
            segment.update(_parse_tts_attributes(match.group("attrs") or ""))
            segments.append(segment)
        return ""

    # Do not normalize whitespace inside the remaining display/code text.
    return _TTS_SEGMENT_RE.sub(replace, text).strip("\r\n"), segments


def split_long_text(text: str, limit: int) -> list[str]:
    """Split at natural boundaries, keeping indentation and balanced code fences."""
    chunks: list[str] = []
    remaining = text
    budget = limit - 4  # Reserve room for a newline and closing fence.
    while len(remaining) > limit:
        window = remaining[:budget]
        cut = window.rfind("\n\n")
        if cut >= budget // 2:
            cut += 2
        else:
            cut = window.rfind("\n")
            if cut >= budget // 2:
                cut += 1
            else:
                for punct in ("。", "！", "？", "；", "…", ".", "!", "?"):
                    pos = window.rfind(punct)
                    if pos > cut:
                        cut = pos + 1
                if cut < budget // 2:
                    cut = window.rfind(" ")
        if cut <= 0:
            cut = budget
        chunk = remaining[:cut]
        remaining = remaining[cut:]
        opening_fence = None
        for line in chunk.splitlines():
            if line.lstrip(" \t").startswith("```"):
                opening_fence = line if opening_fence is None else None
        if opening_fence is not None:
            chunk += "\n```"
            remaining = opening_fence + "\n" + remaining
        chunks.append(chunk)
    if remaining:
        chunks.append(remaining)
    return chunks


def split_message_text(text: str, *, limit: int, split_signal: str) -> list[str]:
    """Honor split/left markers only outside fenced code, then split without tails lost."""
    parts = [""]
    for index, section in enumerate(re.split(r"(```[\s\S]*?(?:```|$))", text)):
        if index % 2:
            parts[-1] += section
        else:
            logical_parts = section.replace("[LEFT]", split_signal).split(split_signal)
            parts[-1] += logical_parts[0]
            parts.extend(logical_parts[1:])
    return [
        chunk
        for part in parts if part.strip()
        for chunk in split_long_text(part, limit=limit)
    ]
