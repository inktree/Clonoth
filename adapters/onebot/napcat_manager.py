"""Project-local NapCat process launcher.

This module keeps the QQ OneBot implementation inside the Clonoth workspace.
It does not contain QQ credentials. The user still has to complete QQ login by
scanning the QR code shown by NapCat or its Web UI.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ONEBOT_DATA_DIR = REPO_ROOT / "data" / "onebot"
NAPCAT_DIR = ONEBOT_DATA_DIR / "napcat" / "onekey"
BOOTMAIN_DIR = NAPCAT_DIR / "bootmain"
INSTALLER_EXE = NAPCAT_DIR / "NapCatInstaller.exe"
BOOT_EXE = BOOTMAIN_DIR / "NapCatWinBootMain.exe"
QQ_EXE = BOOTMAIN_DIR / "QQ.exe"
PID_FILE = ONEBOT_DATA_DIR / "napcat.pid"
LOG_DIR = REPO_ROOT / "data" / "logs"
CONFIG_DIR = ONEBOT_DATA_DIR / "config"
MANAGED_CONFIG = CONFIG_DIR / "napcat-onebot.json"


def _napcat_onebot_config(reverse_host: str, reverse_port: int, reverse_suffix: str) -> dict[str, Any]:
    """Build a NapCat OneBot11 config using reverse WebSocket mode."""
    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "name": "ClonothReverseWebSocket",
                    "enable": True,
                    "url": f"ws://{reverse_host}:{reverse_port}{reverse_suffix}",
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "reconnectInterval": 5000,
                    "token": "",
                    "debug": False,
                    "heartInterval": 30000,
                    "verifyCertificate": True,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
    }


def _discover_runtime_dirs() -> list[Path]:
    """Return NapCat runtime directories created by the one-key installer."""
    dirs: list[Path] = []
    for candidate in sorted(NAPCAT_DIR.glob("NapCat*.Shell"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if (candidate / "NapCatWinBootMain.exe").exists():
            dirs.append(candidate)
    if BOOT_EXE.exists():
        dirs.append(BOOTMAIN_DIR)
    # Keep order and remove duplicates.
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item.resolve())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _select_runtime_dir() -> Path | None:
    """Select the best runtime directory for quick-login startup."""
    for candidate in _discover_runtime_dirs():
        if (candidate / "NapCatWinBootMain.exe").exists() and (candidate / "QQ.exe").exists():
            return candidate
    return None


def _write_default_config(reverse_host: str, reverse_port: int, reverse_suffix: str, account: str = "") -> None:
    """Write project, default, and account-specific NapCat OneBot config files."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    onebot_config = _napcat_onebot_config(reverse_host, reverse_port, reverse_suffix)
    data: dict[str, Any] = {
        "network": {
            "reverse_websocket_url": f"ws://{reverse_host}:{reverse_port}{reverse_suffix}",
            "host": reverse_host,
            "port": reverse_port,
            "suffix": reverse_suffix,
        },
        "onebot": {
            "type": "reverse_websocket",
            "access_token": "",
        },
        "napcat_onebot11": onebot_config,
        "note": "This file records the Clonoth-managed OneBot target. QQ credentials are not stored here.",
    }
    MANAGED_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # NapCat v4.5.3+ can load ./config/onebot11.json as the default OneBot11
    # configuration. Write it to the installer directory, the bootstrap directory,
    # and every runtime directory created by the one-key package.
    for base_dir in (BOOTMAIN_DIR, NAPCAT_DIR, *_discover_runtime_dirs()):
        cfg_dir = base_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "onebot11.json").write_text(
            json.dumps(onebot_config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    account = str(account or "").strip()
    if account:
        for runtime_dir in _discover_runtime_dirs():
            for cfg_dir in runtime_dir.glob("versions/*/resources/app/napcat/config"):
                cfg_dir.mkdir(parents=True, exist_ok=True)
                (cfg_dir / f"onebot11_{account}.json").write_text(
                    json.dumps(onebot_config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )


def _is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def status() -> int:
    pid = _read_pid()
    running = _is_running(pid)
    print(json.dumps({"pid": pid, "running": running, "pid_file": str(PID_FILE)}, ensure_ascii=False))
    return 0 if running else 1


def start(reverse_host: str, reverse_port: int, reverse_suffix: str, quick_login: str = "") -> int:
    ONEBOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    quick = str(quick_login or "").strip()
    _write_default_config(reverse_host, reverse_port, reverse_suffix, quick)

    old_pid = _read_pid()
    if _is_running(old_pid):
        print(f"NapCat already running: pid={old_pid}")
        return 0

    runtime_dir = _select_runtime_dir()
    runtime_boot = runtime_dir / "NapCatWinBootMain.exe" if runtime_dir else None

    if not runtime_boot and not BOOT_EXE.exists() and not INSTALLER_EXE.exists():
        print("NapCat runtime is missing. Expected one of:")
        print(str(runtime_boot or ""))
        print(str(BOOT_EXE))
        print(str(INSTALLER_EXE))
        print("Run the setup command or download NapCat.Shell.Windows.OneKey.zip into data/onebot/napcat/onekey.")
        return 2

    # NapCatWinBootMain.exe expects a quickLogin argument. Before a runtime
    # directory and QQ.exe exist, run the one-key installer so setup can finish.
    # After a QQ account has logged in once, pass the configured quick_login QQ
    # number to BootMain for unattended startup.
    use_boot = runtime_boot is not None and bool(quick)
    exe = runtime_boot if use_boot and runtime_boot is not None else INSTALLER_EXE
    if exe is None or not exe.exists():
        print("NapCat installer is missing and quick-login runtime is not ready.")
        return 2
    cmd = [str(exe)]
    if use_boot:
        cmd.append(quick)
    stdout_path = LOG_DIR / "napcat.out.log"
    stderr_path = LOG_DIR / "napcat.err.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(exe.parent),
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(json.dumps({"started": True, "pid": proc.pid, "exe": str(exe), "cmd": cmd, "quick_login": bool(quick), "runtime_dir": str(runtime_dir or ""), "log": str(stdout_path)}, ensure_ascii=False))
    return 0


def stop() -> int:
    pid = _read_pid()
    if not _is_running(pid):
        print("NapCat is not running")
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    else:
        os.kill(pid, 15)
    print(f"Stopped NapCat pid={pid}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage project-local NapCat runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--reverse-host", default=os.getenv("ONEBOT_HOST", "127.0.0.1"))
    p_start.add_argument("--reverse-port", type=int, default=int(os.getenv("ONEBOT_PORT", "18080")))
    p_start.add_argument("--reverse-suffix", default=os.getenv("ONEBOT_SUFFIX", "/onebot/v11/ws"))
    p_start.add_argument("--quick-login", default=os.getenv("NAPCAT_QUICK_LOGIN", os.getenv("CLONOTH_NAPCAT_QQ", "")))

    sub.add_parser("stop")
    sub.add_parser("status")

    args = parser.parse_args()
    if args.command == "start":
        raise SystemExit(start(args.reverse_host, args.reverse_port, args.reverse_suffix, args.quick_login))
    if args.command == "stop":
        raise SystemExit(stop())
    if args.command == "status":
        raise SystemExit(status())


if __name__ == "__main__":
    main()
