"""Opt-in current-user startup; registration is available for packaged EXEs only."""

import subprocess
import sys
from pathlib import Path


class Autostart:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    NAME = "FunPayTelegramManager"

    def __init__(self, registry=None, executable: Path | None = None):
        if registry is None:
            import winreg

            registry = winreg
        self.registry = registry
        self.executable = executable or (
            Path(sys.executable) if getattr(sys, "frozen", False) else None
        )

    def command(self) -> str:
        if self.executable is None or not self.executable.is_file():
            raise ValueError("Автозапуск доступен для собранного EXE")
        command = subprocess.list2cmdline([str(self.executable.resolve()), "--minimized"])
        if len(command) > 260:
            raise ValueError("Путь слишком длинный для автозапуска Windows")
        return command

    def read(self) -> str | None:
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, self.KEY, 0, r.KEY_READ) as key:
                value, kind = r.QueryValueEx(key, self.NAME)
                return value if kind == r.REG_SZ else "invalid"
        except FileNotFoundError:
            return None

    def enable(self) -> None:
        command = self.command()
        r = self.registry
        with r.CreateKeyEx(r.HKEY_CURRENT_USER, self.KEY, 0, r.KEY_SET_VALUE) as key:
            r.SetValueEx(key, self.NAME, 0, r.REG_SZ, command)

    def disable(self) -> None:
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, self.KEY, 0, r.KEY_SET_VALUE) as key:
                r.DeleteValue(key, self.NAME)
        except FileNotFoundError:
            pass
