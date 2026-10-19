"""DeepSeek DSML (DeepSeek Markup Language) tool-call parsing.

Why: DeepSeek V4/V4.1 emit native tool calls as DSML text blocks when the
server-side parser does not convert them into structured ``tool_calls``. In that
case the raw markup leaks into user-visible content and the tool is never run.

How: parse the DSML block into structured tool calls, strip the markup from the
visible text, and provide a streaming filter that withholds partial markers so
half-written tags never reach the UI.

Purpose: make DSML output a first-class tool-call path instead of leaked text.

Format reference (DeepSeek-V4 encoding README):

    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="tool">
    <｜DSML｜parameter name="p" string="true">value</｜DSML｜parameter>
    </｜DSML｜invoke>
    </｜DSML｜tool_calls>

V4.1 adds a leading space before the block/invoke/parameter names, so every
pattern below tolerates optional whitespace and both full-width and ASCII pipes.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

_DSML = r"[｜|]\s*DSML\s*[｜|]"
_BLOCK_NAMES = r"(?:tool_calls|calls|toolcalls|tool)"

_OPEN_RE = re.compile(
    rf"<\s*{_DSML}\s*({_BLOCK_NAMES})\s*>",
    re.DOTALL,
)
_BLOCK_RE = re.compile(
    rf"<\s*{_DSML}\s*{_BLOCK_NAMES}\s*>(.*?)</\s*{_DSML}\s*{_BLOCK_NAMES}\s*>",
    re.DOTALL,
)
_UNCLOSED_BLOCK_RE = re.compile(
    rf"<\s*{_DSML}\s*{_BLOCK_NAMES}\s*>(.*)\Z",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    rf'<\s*{_DSML}\s*invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)</\s*{_DSML}\s*invoke\s*>',
    re.DOTALL,
)
_INVOKE_OPEN_RE = re.compile(
    rf'<\s*{_DSML}\s*invoke\s+name\s*=\s*"([^"]+)"\s*>(.*)\Z',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    rf'<\s*{_DSML}\s*parameter\s+name\s*=\s*"([^"]+)"'
    rf'(?:\s+string\s*=\s*"([^"]*)")?\s*>(.*?)</\s*{_DSML}\s*parameter\s*>',
    re.DOTALL,
)

# Any fragment that starts a DSML construct. Used to trim stray leftovers.
_ANY_TAG_RE = re.compile(rf"</?\s*{_DSML}[^>]*>")


def _coerce_param(raw: str, is_string: str | None) -> Any:
    """Convert one DSML parameter value to its Python value."""
    value = raw.strip()
    if str(is_string or "").strip().lower() == "true":
        return value
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split DSML tool calls out of *text*.

    Returns ``(clean_text, calls)`` where ``calls`` is a list of
    ``{"id", "name", "arguments"}`` dicts. Text outside the DSML blocks is kept;
    a trailing unclosed block is consumed rather than shown to the user.
    """
    if not text or "DSML" not in text:
        return text, []

    calls: list[dict[str, Any]] = []

    def _collect(block_body: str) -> None:
        before = len(calls)
        for match in _INVOKE_RE.finditer(block_body):
            name = match.group(1).strip()
            if not name:
                continue
            arguments: dict[str, Any] = {}
            for pm in _PARAM_RE.finditer(match.group(2)):
                arguments[pm.group(1).strip()] = _coerce_param(pm.group(3), pm.group(2))
            calls.append({
                "id": f"dsml_{uuid.uuid4().hex[:8]}",
                "name": name,
                "arguments": arguments,
            })
        if len(calls) > before:
            return
        # Lenient pass: a truncated stream may cut the closing invoke tag.
        for match in _INVOKE_OPEN_RE.finditer(block_body):
            name = match.group(1).strip()
            if not name:
                continue
            arguments: dict[str, Any] = {}
            for pm in _PARAM_RE.finditer(match.group(2)):
                arguments[pm.group(1).strip()] = _coerce_param(pm.group(3), pm.group(2))
            calls.append({
                "id": f"dsml_{uuid.uuid4().hex[:8]}",
                "name": name,
                "arguments": arguments,
            })

    for block in _BLOCK_RE.finditer(text):
        _collect(block.group(1))

    cleaned = _BLOCK_RE.sub("", text)

    # A block that never closed (truncated stream / dropped token) still carries
    # real calls; consume it so the markup cannot surface as prose.
    if "DSML" in cleaned:
        tail = _UNCLOSED_BLOCK_RE.search(cleaned)
        if tail:
            _collect(tail.group(1))
            cleaned = cleaned[: tail.start()]

    # Stray invoke/parameter tags without a wrapping block.
    if "DSML" in cleaned and "invoke" in cleaned:
        before = len(calls)
        _collect(cleaned)
        if len(calls) > before:
            cleaned = _INVOKE_RE.sub("", cleaned)

    if "DSML" in cleaned:
        cleaned = _ANY_TAG_RE.sub("", cleaned)

    return cleaned.strip(), calls


def strip_blocks(text: str) -> str:
    """Remove all DSML markup from *text*, ignoring any embedded calls."""
    if not text or "DSML" not in text:
        return text
    cleaned = _BLOCK_RE.sub("", text)
    cleaned = _UNCLOSED_BLOCK_RE.sub("", cleaned)
    cleaned = _ANY_TAG_RE.sub("", cleaned)
    return cleaned.strip()


class StreamFilter:
    """Incremental filter that hides DSML blocks from a live text stream.

    The filter holds back text that might still grow into a DSML marker and
    swallows everything inside a tool-call block. Call :meth:`finish` when the
    stream ends to flush any surviving plain text.
    """

    # How long a trailing ``<`` fragment may stay buffered before it is treated
    # as ordinary text. A real DSML opener is far shorter than this.
    _MAX_HOLD = 64

    def __init__(self) -> None:
        self._buf = ""
        self._in_block = False
        self._pending_close = ""
        self._block_name = ""

    def push(self, chunk: str) -> str:
        """Feed one streamed chunk and return the text that is safe to show."""
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []

        while self._buf:
            if self._in_block:
                # V4.1 writes the closer as `</｜DSML｜ tool_calls>` with a leading
                # space, so match with a regex instead of a fixed string.
                close_re = re.compile(
                    rf"</\s*{_DSML}\s*{re.escape(self._block_name)}\s*>",
                    re.DOTALL,
                )
                match = close_re.search(self._buf)
                if not match:
                    keep = 64
                    self._buf = self._buf[-keep:] if keep > 0 else ""
                    return "".join(out)
                self._buf = self._buf[match.end():]
                self._in_block = False
                self._block_name = ""
                continue

            opener = self._find_opener(self._buf)
            if opener is not None:
                start, block_name = opener
                out.append(self._buf[:start])
                self._buf = self._buf[start:]
                self._pending_close = block_name
                self._block_name = block_name
                self._in_block = True
                # Drop the opening tag itself.
                gt = self._buf.find(">")
                self._buf = self._buf[gt + 1:] if gt >= 0 else ""
                continue

            # No opener yet: emit everything except a possible partial marker.
            hold = self._partial_hold(self._buf)
            if hold:
                out.append(self._buf[: len(self._buf) - hold])
                self._buf = self._buf[-hold:]
            else:
                out.append(self._buf)
                self._buf = ""
            break

        return "".join(out)

    def finish(self) -> str:
        """Flush remaining buffered text at end of stream."""
        rest = self._buf
        self._buf = ""
        self._in_block = False
        self._pending_close = ""
        self._block_name = ""
        if not rest:
            return ""
        # Never surface leftover protocol markup at the very end.
        return strip_blocks(rest)

    @staticmethod
    def _find_opener(buf: str) -> tuple[int, str] | None:
        """Return (start_index, closing_tag) for the first DSML block opener."""
        match = _OPEN_RE.search(buf)
        if not match:
            return None
        return match.start(), match.group(1)


    def _partial_hold(self, buf: str) -> int:
        """Return how many trailing chars must stay buffered."""
        idx = buf.rfind("<")
        if idx < 0:
            return 0
        tail = buf[idx:]
        if ">" in tail:
            return 0
        if len(tail) > self._MAX_HOLD:
            return 0
        lowered = tail.lower()
        # Only hold fragments that actually look like the start of a tag.
        if "<" == tail[0] and (len(tail) == 1 or any(c in lowered for c in ("｜", "|", "d", "dsml"))):
            return len(tail)
        return 0
