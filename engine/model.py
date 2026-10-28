from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict, resolve_env_ref

from .node import Node

_current_sticky_sid_var: ContextVar[str] = ContextVar("_current_sticky_sid_var", default="")


def parse_sticky_session_config(raw: Any) -> dict[str, Any] | None:
    """Normalize sticky_session config from bool or dict.

    Returns:
      - None if not configured
      - {"enabled": False, ...} if explicitly disabled
      - {"enabled": True, "header": str, "ip_prefix": str} if enabled
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {
            "enabled": raw,
            "header": "X-Forwarded-For",
            "ip_prefix": "10",
        }
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        header = str(raw.get("header") or "X-Forwarded-For").strip() or "X-Forwarded-For"
        ip_prefix = str(raw.get("ip_prefix") or "10").strip().strip(".") or "10"
        return {
            "enabled": enabled,
            "header": header,
            "ip_prefix": ip_prefix,
        }
    return None


def compute_sticky_ip(session_key: str, ip_prefix: str = "10") -> str:
    """Deterministically map a session_key into an IPv4 string under ip_prefix."""
    clean_prefix = str(ip_prefix or "10").strip().strip(".")
    parts = [p.strip() for p in clean_prefix.split(".") if p.strip()]
    octets: list[int] = []
    for p in parts[:4]:
        try:
            val = int(p)
            if 0 <= val <= 255:
                octets.append(val)
        except ValueError:
            pass
    if not octets:
        octets = [10]
    if len(octets) >= 4:
        return ".".join(str(x) for x in octets[:4])

    digest = hashlib.md5(str(session_key or "default").encode("utf-8")).digest()
    needed = 4 - len(octets)
    for i in range(needed):
        b = digest[i]
        if i == needed - 1:
            # Avoid .0 and .255 on final host octet
            octets.append(1 + (b % 254))
        else:
            octets.append(b)
    return ".".join(str(x) for x in octets)


class StickyHttpProxy:
    """Lightweight httpx.AsyncClient proxy that injects a sticky session header
    into post/get/request/stream/build_request/send calls while sharing the
    underlying connection pool.
    """

    def __init__(self, client: Any, header_name: str, header_value: str):
        self._client = client
        self._header_name = header_name
        self._header_value = header_value

    def _merge_headers(self, headers: Any) -> dict[str, Any]:
        merged: dict[str, Any] = dict(headers) if headers else {}
        lower_keys = {str(k).lower() for k in merged.keys()}
        if self._header_name.lower() not in lower_keys:
            merged[self._header_name] = self._header_value
        return merged

    async def post(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.post(url, **kwargs)

    async def get(self, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.get(url, **kwargs)

    async def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return await self._client.request(method, url, **kwargs)

    def stream(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.stream(method, url, **kwargs)

    def build_request(self, method: str, url: Any, **kwargs: Any) -> Any:
        kwargs["headers"] = self._merge_headers(kwargs.get("headers"))
        return self._client.build_request(method, url, **kwargs)

    async def send(self, request: Any, **kwargs: Any) -> Any:
        if hasattr(request, "headers") and self._header_name not in request.headers:
            request.headers[self._header_name] = self._header_value
        return await self._client.send(request, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def wrap_http_with_sticky_session(
    http: Any,
    sticky_cfg_raw: Any,
    session_key: str,
) -> Any:
    """Wrap an httpx.AsyncClient with StickyHttpProxy if sticky_cfg_raw is enabled."""
    if http is None or not session_key:
        return http
    parsed = parse_sticky_session_config(sticky_cfg_raw)
    if not parsed or not parsed.get("enabled"):
        return http
    base_client = getattr(http, "_client", http)
    sticky_ip = compute_sticky_ip(session_key, parsed["ip_prefix"])
    return StickyHttpProxy(base_client, parsed["header"], sticky_ip)


@dataclass
class ResolvedProvider:
    model: str
    # [provider-registry 2026-05-03] provider_type 是 ProviderRegistry 的 key。
    # 原因：provider 名称已插件化；做法：这里只保存字符串，不列死内置集合；目的：保持模型解析层与具体 provider 解耦。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认
    provider_options: dict[str, Any] = field(default_factory=dict)


def resolve_provider(
    node: Node,
    provider_default: str,
    session_override: dict[str, Any] | None = None,
    provider_configs: dict[str, Any] | None = None,
    default_provider_type: str = "openai",
) -> ResolvedProvider:
    """根据全局、节点和 session 配置解析模型和可选 api_key/base_url。

    provider_configs: 从 config.yaml 加载的所有 provider section（如 openai,
    deepseek, openai-responses 等），用于在 model/base_url/api_key 为空时从对应 provider 取默认值。
    default_provider_type: config.yaml 里的全局 active provider。节点未写 provider 时使用它。
    """
    model = node.model.strip() if node.model else ""

    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None

    # 从 node.provider 获取 ProviderRegistry key；节点未写 provider 时使用全局 active provider。
    # [AutoC 2026-06-16] Why: Settings → Model → ACTIVE 表示全局默认渠道，
    # but empty node.provider used to hard-code openai. How: let callers pass the
    # config.yaml provider field as default_provider_type. Purpose: selecting
    # deepseek/openai-responses as active changes default AI nodes without editing YAML.
    provider_type = (node.provider.strip() if node.provider else "") or (default_provider_type or "").strip() or "openai"

    override = session_override if isinstance(session_override, dict) else {}
    # [AutoC 2026-06-01] Why: provider selection now has a session-scoped layer
    # used by the supervisor API. How: compute the existing global→node result
    # first, then overlay non-empty session_override fields for provider/model,
    # api_key, and base_url. Purpose: the priority is session > node > global
    # without changing older resolve_provider call sites.
    override_provider = str(override.get("provider") or override.get("provider_type") or "").strip().lower()
    override_model = str(override.get("model") or "").strip()
    override_api_key = str(override.get("api_key") or "").strip()
    override_base_url = str(override.get("base_url") or "").strip()
    if override_provider:
        provider_type = override_provider
    if override_model:
        model = override_model
    if override_api_key:
        api_key = override_api_key
    if override_base_url:
        base_url = override_base_url

    override_po: dict[str, Any] = {}
    raw_override_po = override.get("provider_options")
    if isinstance(raw_override_po, dict):
        override_po.update(raw_override_po)
    for _ov_key in ("reasoning_field", "reasoning_effort", "max_tokens", "max_completion_tokens", "thought_summary"):
        if _ov_key in override and override[_ov_key] is not None:
            override_po[_ov_key] = override[_ov_key]
    raw_override_tc = override.get("thinking_config")
    if isinstance(raw_override_tc, dict):
        existing_tc = override_po.get("thinking_config")
        if isinstance(existing_tc, dict):
            override_po["thinking_config"] = {**existing_tc, **raw_override_tc}
        else:
            override_po["thinking_config"] = dict(raw_override_tc)

    # [2026-06-07] Why: when a node sets provider but leaves model/base_url/api_key
    # empty, the old code fell back to the global active provider's values, not the
    # target provider's own config section. How: if fields are still empty after
    # node and session override, look up provider_configs[provider_type]. Purpose:
    # provider selection becomes authoritative: provider=X inherits X's defaults
    # unless overridden by session or node fields.
    if not provider_configs:
        try:
            _root_cfg_path = Path(__file__).resolve().parent.parent / "data" / "config.yaml"
            if _root_cfg_path.is_file():
                _raw_c = load_yaml_dict(_root_cfg_path)
                if isinstance(_raw_c, dict):
                    provider_configs = {
                        k: v for k, v in _raw_c.items()
                        if isinstance(v, dict) and any(pk in v for pk in ("base_url", "api_key", "model"))
                    }
        except Exception:
            pass
    pcfg = (provider_configs or {}).get(provider_type)
    if not isinstance(pcfg, dict) and provider_type in ("gemini", "gemini-vertex", "gemini-aistudio"):
        pcfg = (provider_configs or {}).get("gemini-vertex") or (provider_configs or {}).get("gemini")
    if isinstance(pcfg, dict):
        if not model and pcfg.get("model"):
            model = resolve_env_ref(str(pcfg["model"]).strip())
        if not base_url and pcfg.get("base_url"):
            base_url = resolve_env_ref(str(pcfg["base_url"]).strip())
        if not api_key and pcfg.get("api_key"):
            api_key = resolve_env_ref(str(pcfg["api_key"]).strip())
        if "sticky_session" in pcfg and "sticky_session" not in override_po:
            override_po["_provider_sticky_session"] = pcfg.get("sticky_session")
        for _opt_key in ("reasoning_field", "reasoning_effort", "max_tokens", "max_completion_tokens", "thought_summary"):
            if _opt_key in pcfg and _opt_key not in override_po:
                override_po[_opt_key] = pcfg.get(_opt_key)
    if not model:
        model = provider_default or "gpt-4o-mini"

    cur_sid = _current_sticky_sid_var.get()
    if cur_sid and getattr(node, "persistent", False) and not cur_sid.endswith(f":{node.id}"):
        override_po.setdefault("_sticky_session_id", f"{cur_sid}:{node.id}")

    return ResolvedProvider(
        model=model,
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
        provider_options=override_po,
    )
