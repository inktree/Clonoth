"""Standalone NoneBot2 launcher for the Clonoth OneBot adapter.

This file starts a NoneBot2 reverse WebSocket server, registers the OneBot V11
adapter, and loads the existing plugin code from adapters/onebot/__init__.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


REPO_ROOT = Path(__file__).resolve().parents[2]


def _set_default_environment() -> None:
    """Set safe local defaults before the plugin reads its configuration."""
    os.environ.setdefault("CLONOTH_WORKSPACE", str(REPO_ROOT))
    os.environ.setdefault("CLONOTH_BASE_URL", os.environ.get("CLONOTH_URL", "http://127.0.0.1:8765"))
    os.environ.setdefault("CLONOTH_ENTRY_NODE", "qq_assistant")

    # NoneBot / OneBot reverse WebSocket defaults.
    # NapCat or another OneBot implementation should connect to:
    # ws://127.0.0.1:8080/onebot/v11/ws
    os.environ.setdefault("ONEBOT_HOST", "127.0.0.1")
    os.environ.setdefault("ONEBOT_PORT", "8080")


def main() -> None:
    _set_default_environment()
    sys.path.insert(0, str(REPO_ROOT))

    host = os.environ.get("ONEBOT_HOST", "127.0.0.1")
    port = int(os.environ.get("ONEBOT_PORT", "8080"))

    nonebot.init(driver="~fastapi", host=host, port=port)
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    nonebot.load_plugin("adapters.onebot")

    nonebot.run(host=host, port=port)


if __name__ == "__main__":
    main()
