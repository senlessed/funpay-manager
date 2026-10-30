from contextlib import nullcontext

import pytest

from funpay_manager.app.autostart import Autostart


class Registry:
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 3, 4

    def __init__(self):
        self.values = {}

    def OpenKey(self, hive, path, reserved, rights):
        assert hive == self.HKEY_CURRENT_USER
        assert path == Autostart.KEY
        return nullcontext(self)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


def test_startup_quotes_path_and_repairs_move(tmp_path):
    executable = tmp_path / "folder with spaces" / "FunPayManager.exe"
    executable.parent.mkdir()
    executable.touch()
    registry = Registry()
    startup = Autostart(registry, executable)
    assert startup.read() is None
    startup.enable()
    assert startup.read() == f'"{executable}" --minimized'
    moved = tmp_path / "moved.exe"
    moved.touch()
    repair = Autostart(registry, moved)
    assert repair.read() != repair.command()
    repair.enable()
    assert repair.read() == repair.command()
    registry.values["AnotherApp"] = "leave intact"
    repair.disable()
    repair.disable()
    assert registry.values == {"AnotherApp": "leave intact"}


def test_missing_exe_does_not_register(tmp_path):
    registry = Registry()
    with pytest.raises(ValueError):
        Autostart(registry, tmp_path / "absent.exe").enable()
    assert not registry.values
