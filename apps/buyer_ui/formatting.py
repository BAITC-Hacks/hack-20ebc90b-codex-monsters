"""Presentation helpers. Decimal values remain strings in all API requests."""
from decimal import Decimal, InvalidOperation
from datetime import datetime

import streamlit as st


def format_decimal(value, *, signed=False, places=None):
    if value is None:
        return "Не рассчитано"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "Некорректное значение"
        if places is not None:
            number = number.quantize(Decimal(1).scaleb(-places))
        result = format(number, ",f").replace(",", "\u202f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        if signed and number > 0:
            result = "+" + result
        return result
    except (InvalidOperation, ValueError):
        return "Некорректное значение"


def format_date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return str(value or "дата не указана")


def format_uom(value):
    return {"pcs": "шт.", "m": "м", "coil": "бухт.", "pack": "уп."}.get(value, value)


def format_money(value, currency=None):
    if value is None or not currency:
        return "Не рассчитано"
    return f"{format_decimal(value)} {currency}"


def show_issues(issues):
    seen = set()
    for issue in issues or []:
        if not isinstance(issue, dict):
            st.warning(str(issue))
            continue
        message = issue.get("message") or issue.get("code", "Ограничение данных")
        scopes = issue.get("scope_ids") or []
        identity = (message, tuple(str(scope) for scope in scopes))
        if identity in seen:
            continue
        seen.add(identity)
        if scopes:
            message += " · " + ", ".join(str(scope) for scope in scopes)
        severity = issue.get("severity", "warning")
        {"blocking": st.error, "info": st.info}.get(severity, st.warning)(message)


def show_api_error(error):
    status = getattr(error, "status_code", None)
    st.error(getattr(error, "message", str(error)))
    if getattr(error, "ambiguous", False):
        st.warning("Ответ не получен. Сервер мог выполнить действие. Сверьте состояние; "
                   "не считайте операцию завершённой. Для повторного экспорта или запуска "
                   "сохранён тот же ключ запроса.")
    elif status == 409:
        st.warning("Состояние на сервере изменилось. Обновите данные и проверьте новую "
                   "версию. Ввод сохранён как неподтверждённый черновик.")
    elif status == 422:
        st.warning("Исправьте указанные поля или условия. Количество не округлялось интерфейсом.")
    elif status == 403:
        st.warning("У текущей серверной учётной записи нет полномочий на это действие.")
    details = getattr(error, "details", None)
    if details:
        with st.expander("Подробности ошибки"):
            st.json(details)
    st.caption(f"Код: {getattr(error, 'code', 'UNKNOWN')}" + (f" · HTTP {status}" if status else ""))


def show_quality(quality):
    quality = quality or {}
    labels = {"ready": "Готовы", "degraded": "Есть ограничения", "blocked": "Заблокированы"}
    st.write("**Качество данных:** " + labels.get(quality.get("status"), "Не сообщено сервером"))
    cols = st.columns(3)
    for col, field, title in zip(cols, ("accepted_rows", "rejected_rows", "affected_skus"),
                                 ("Принято строк", "Отклонено строк", "Затронуто SKU")):
        col.metric(title, format_decimal(quality.get(field)))
    caps = quality.get("capabilities") or {}
    rows = []
    for field, title in (("can_plan", "Расчёт заказа"), ("budget_available", "Оценка бюджета"),
                         ("can_approve", "Утверждение"), ("observed_stockouts_available", "Наблюдения дефицита"),
                         ("customer_detection_available", "Классификация по клиентам")):
        value = caps.get(field)
        rows.append({"Возможность": title, "Доступность": "Да" if value is True else "Нет" if value is False else "Не сообщено"})
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("Отдельная готовность прогноза не определена API. Наличие месячного остатка "
               "не подтверждает текущий запас или интервалы отсутствия товара.")
    for reason in caps.get("reasons") or []:
        st.warning(reason)
    show_issues(quality.get("issues"))
