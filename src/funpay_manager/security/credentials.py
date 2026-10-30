import sys


class CredentialStore:
    """Explicit Windows backend: never fall back to plaintext keyrings."""

    SERVICE = "FunPayTelegramManager"
    NAMES = frozenset({"telegram_token", "funpay_session"})

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Защищённое хранилище доступно только на Windows")
        from keyring.backends.Windows import WinVaultKeyring

        self.backend = WinVaultKeyring()

    def set(self, name: str, value: str) -> None:
        if name not in self.NAMES or not value.strip():
            raise ValueError("Недопустимый секрет")
        self.backend.set_password(self.SERVICE, name, value)

    def get(self, name: str) -> str | None:
        if name not in self.NAMES:
            raise ValueError("Неизвестный секрет")
        return self.backend.get_password(self.SERVICE, name)
