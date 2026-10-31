from string import Formatter

FIELDS = frozenset({"buyer", "seller", "chat_id"})


class TemplateError(ValueError):
    """Only fixed, non-secret text may be displayed to the owner."""


def render_template(body, values):
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-16-le")) > 5600:
        raise TemplateError("Шаблон пустой или слишком длинный.")
    result = []
    try:
        for literal, field, spec, conversion in Formatter().parse(body):
            result.append(literal)
            if field is not None:
                if field not in FIELDS or spec or conversion:
                    raise TemplateError(
                        "Разрешены только {buyer}, {seller}, {chat_id}, без форматирования."
                    )
                value = values.get(field)
                if not isinstance(value, str) or not value:
                    raise TemplateError("Для подстановки не хватает данных чата или аккаунта.")
                result.append(value)
    except ValueError as error:
        if isinstance(error, TemplateError):
            raise
        raise TemplateError(
            "Проверьте фигурные скобки. Для обычных скобок используйте {{ и }}."
        ) from error
    text = "".join(result).strip()
    if not text or len(text.encode("utf-16-le")) > 5600:
        raise TemplateError("Результат подстановки пустой или слишком длинный. Сократите шаблон.")
    return text


def validate_template(title, body):
    title, body = title.strip(), body.strip()
    if not 1 <= len(title) <= 60:
        raise TemplateError("Название должно содержать от 1 до 60 символов.")
    render_template(body, {"buyer": "Покупатель", "seller": "Продавец", "chat_id": "12345"})
    return title, body
