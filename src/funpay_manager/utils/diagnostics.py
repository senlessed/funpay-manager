from enum import StrEnum


class FailureCode(StrEnum):
    UNEXPECTED = "unexpected"
    FP_KEY = "fp_key"
    FP_AUTH = "fp_auth_unconfirmed"
    FP_HTTP_401 = "fp_http_401"
    FP_HTTP_403 = "fp_http_403"
    FP_HTTP_429 = "fp_http_429"
    FP_HTTP = "fp_http"
    FP_FORMAT = "fp_format"
    FP_TIMEOUT = "fp_timeout"
    FP_TLS = "fp_tls"
    FP_NETWORK = "fp_network"
    FP_CREDENTIALS = "fp_credentials"
    FP_CONNECTION = "fp_connection"


class DiagnosticError(RuntimeError):
    """Only a fixed diagnostic code may cross the backend/UI boundary."""

    default_code = FailureCode.UNEXPECTED

    def __init__(self, message="", *, code=None):
        super().__init__(message)
        self.diagnostic_code = code if isinstance(code, FailureCode) else self.default_code


FUNPAY_FAILURE_TEXT = {
    FailureCode.FP_KEY: (
        "Неверный формат ключа. Скопируйте только значение cookie golden_key из колонки Value, "
        "без других cookies, пробелов и переносов строк."
    ),
    FailureCode.FP_AUTH: (
        "FunPay вернул страницу без подтверждённого профиля. Сессия могла истечь либо сайт "
        "потребовал дополнительную проверку. Войдите на funpay.com вручную и заново "
        "скопируйте значение golden_key из текущего браузерного профиля."
    ),
    FailureCode.FP_HTTP_401: (
        "FunPay отклонил авторизацию (HTTP 401). Войдите на сайт вручную и скопируйте "
        "актуальное значение golden_key."
    ),
    FailureCode.FP_HTTP_403: (
        "FunPay запретил запрос (HTTP 403). Причина может быть в сессии или защите сайта. "
        "Откройте funpay.com в браузере, проверьте вход и наличие CAPTCHA. "
        "Если браузер работает, а ошибка остаётся, прекратите повторные попытки."
    ),
    FailureCode.FP_HTTP_429: "FunPay ограничил частоту запросов (HTTP 429). Повторите позже.",
    FailureCode.FP_HTTP: "FunPay вернул неожиданный HTTP-статус. Повторите проверку позже.",
    FailureCode.FP_FORMAT: (
        "Ответ FunPay не соответствует ожидаемому формату. Нужна проверка адаптера; "
        "повторный ввод ключа может не помочь."
    ),
    FailureCode.FP_TIMEOUT: "FunPay не ответил за 20 секунд. Проверьте сеть и повторите позже.",
    FailureCode.FP_TLS: (
        "Не удалось проверить защищённое TLS-соединение. Проверьте дату Windows и сертификаты "
        "сети. Проверка сертификатов не отключается."
    ),
    FailureCode.FP_NETWORK: (
        "Не удалось установить соединение с FunPay. Проверьте интернет, DNS и доступ "
        "к funpay.com на этом компьютере."
    ),
    FailureCode.FP_CREDENTIALS: (
        "Профиль FunPay проверен, но Windows Credential Manager не сохранил ключ. "
        "Проверьте доступ к Диспетчеру учётных данных Windows."
    ),
    FailureCode.FP_CONNECTION: "Не удалось обработать ответ FunPay. Повторите проверку позже.",
    FailureCode.UNEXPECTED: (
        "Внутренняя ошибка подключения. Передайте разработчику код ошибки и тип из логов, "
        "без ключа и содержимого cookies."
    ),
}


def funpay_failure_text(code):
    try:
        reason = FailureCode(code)
    except ValueError:
        reason = FailureCode.UNEXPECTED
    return f"{FUNPAY_FAILURE_TEXT[reason]}\nКод: {reason.value}. Ключ не передавайте в чат."
