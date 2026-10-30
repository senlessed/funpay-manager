def telegram_text(text: str, max_units: int = 3500) -> str:
    """Bound display previews by UTF-16 units without splitting surrogate pairs."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= max_units * 2:
        return text
    return encoded[: (max_units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
