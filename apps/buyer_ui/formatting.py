"""Presentation helpers. Decimal values remain strings in all API requests."""
from decimal import Decimal, InvalidOperation
from datetime import datetime
import re
import os

import streamlit as st


ISSUE_MESSAGES = {
    "UNSUPPORTED_SIGNED_MOVEMENT": "Возвраты и движения с неоднозначным знаком исключены; они не учтены в прогнозе.",
    "MISSING_QUANTITY": "В части исходных строк не указано количество; эти строки не учтены.",
    "BUYER_DEMAND_COVERAGE_ASSUMPTION": "По вашему подтверждению дни без записей приняты за нулевые продажи. Полнота истории ERP не проверена; неподтверждённые движения и возвраты не учтены.",
    "BUYER_INPUTS_CONFIRMED": "Расчёт использует подтверждённые закупщиком остатки, условия и поставки. Дни без записей приняты за нулевые продажи; неподтверждённые движения не учтены.",
    "BUYER_PREPARATION_REQUIRED": "Заполните и подтвердите остатки и условия закупки в разделе «Данные».",
    "INCOMPLETE_BUYER_INPUT": "Для выбранного товара нужно заполнить остаток и обязательные условия закупки.",
    "HISTORY_CONFIRMATION_REQUIRED": "Подтвердите допущение об истории продаж при сохранении условий закупки.",
    "ASSUMPTIONS_ACKNOWLEDGEMENT_REQUIRED": "Перед утверждением подтвердите проверку введённых условий и допущений расчёта.",
    "FORECAST_PROVIDER_NOT_CONNECTED": "Прогноз — временное синтетическое приближение: рабочая модель спроса для этого расчёта не подключена.",
    "MISSING_SKU_MASTER": "Не найдена проверенная карточка товара.",
    "MISSING_PURCHASE_MAPPING": "Не подтверждены единица закупки и пересчёт в складскую единицу. Уточните карточку товара.",
    "MISSING_DEMAND_HISTORY": "Отсутствует пригодная история продаж товара.",
    "MISSING_CURRENT_STOCK": "Нет подтверждённого текущего остатка с резервами и блокировками.",
    "MISSING_SUPPLIER_TERMS": "Для заказа нужны условия поставщика: срок, минимальная партия и упаковка. Цена нужна для расчёта стоимости.",
    "SUPPLIER_MAPPING_MISMATCH": "Поставщик в условиях закупки не совпадает с карточкой товара.",
    "OBSERVED_STOCKOUTS_UNAVAILABLE": "Точные исторические периоды отсутствия товара не предоставлены. Потерянный спрос по реальным данным не восстановлен.",
    "METADATA_ONLY_SOURCE": "Источник зарегистрирован как справочный. Значения не подставлены в заказ без проверки их смысла.",
    "NON_AUTHORITATIVE_SOURCE": "Пересекающиеся продажи из справочного источника не добавлены повторно, чтобы не завысить спрос.",
    "INCOMPLETE_DEMAND_COVERAGE": "Предварительная выборка не охватывает всю историю спроса.",
    "SALES_UOM_MISMATCH": "Единица продажи не совпадает с карточкой товара. Требуется подтверждённый пересчёт.",
    "DUPLICATE_CANONICAL_KEY": "В источнике есть повторяющиеся или противоречивые записи. Требуется сверка данных.",
    "SEASONALITY_NEUTRAL_UNVERIFIED": "Сезонность не подтверждена: расчёт без сезонного увеличения.",
    "SEASONALITY_MISSING_MONTHS_NEUTRAL": "Для части месяцев нет коэффициентов сезонности; дополнительная поправка не применена.",
    "SEASONALITY_ESTIMATED_UNVALIDATED": "Сезонность оценена по истории и ещё не проверена на отдельном периоде.",
    "NEGATIVE_NET_DEMAND_CLAMPED_FOR_FORECAST": "Отрицательный спрос из-за возвратов ограничен нулём только при прогнозировании.",
    "UNKNOWN_HISTORY_EXCLUDED": "Дни с неизвестным спросом исключены из оценки, а не приняты за нулевые продажи.",
    "GAP_BETWEEN_HISTORY_AND_FORECAST": "Между последними данными и датой плана есть промежуток. Проверьте актуальность истории.",
    "STALE_HISTORY_RECENT_LEVEL_UNAVAILABLE": "История устарела: недавний уровень спроса неизвестен.",
    "SHORT_HISTORY_LOW_CONFIDENCE": "История продаж короткая. Надёжность оценки спроса ограничена.",
    "GROWTH_NOT_SUSTAINED_NEUTRAL": "Устойчивый рост спроса не подтверждён; дополнительное увеличение не применено.",
    "GROWTH_INSUFFICIENT_COMPLETE_HISTORY_NEUTRAL": "Для оценки роста недостаточно полных периодов; дополнительное увеличение не применено.",
    "UNCERTAINTY_STOCKOUT_STATUS_UNKNOWN": "Неизвестно, был ли товар в наличии во все дни оценки. Неопределённость спроса требует проверки.",
    "UNCERTAINTY_INSUFFICIENT_OBSERVED_HISTORY": "Недостаточно наблюдений для надёжной оценки изменчивости спроса.",
    "MISSING_OR_DUPLICATE_SKU": "Для товара не найдено однозначное соответствие в справочнике. Уточните карточку товара.",
    "INELIGIBLE_LIFECYCLE": "Товар исключён из автоматического пополнения по статусу ассортимента.",
    "MISSING_SUPPLIER": "Для товара не указан поставщик.",
    "MISSING_STOCK": "Нет подтверждённого доступного остатка. Нужны актуальные остатки с учётом резервов и блокировок.",
    "INVALID_STOCK_ACCOUNTING": "Не удалось проверить остаток и учёт резервов. Уточните данные склада.",
    "MISSING_TERMS": "Не заданы действующие условия поставщика: срок поставки, минимальная партия и упаковка.",
    "INVALID_OR_MISSING_TERMS": "Условия поставки неполны или противоречат единицам товара. Проверьте срок, упаковку и минимальную партию.",
    "MISSING_FORECAST": "Для товара и склада нет пригодного прогноза спроса.",
    "ASSUMED_PLANNING_INPUT": "Часть условий принята как допущение. Заказ по реальным данным требует проверки закупщиком.",
    "UNCALIBRATED_SCENARIOS": "Уровень наличия в сценарии задан как цель; его достижение ещё не подтверждено проверкой на истории.",
    "UNKNOWN_ETA_EXCLUDED": "Поставка без подтверждённой даты прибытия не уменьшает потребность в заказе.",
    "PAST_DUE_PIPELINE_EXCLUDED": "Просроченная поставка не учтена в доступном запасе. Запросите у поставщика новую дату прибытия.",
    "ASSUMED_PIPELINE": "Количество или дата прибытия поставки приняты как допущение. Подтвердите их до закупки.",
    "DELAY_SCENARIO": "Срок пополнения и даты ожидаемых поставок сдвинуты на выбранную задержку.",
    "MAX_COVER_LIMIT": "Заказ уменьшен лимитом покрытия запасом. Целевой уровень наличия может не быть достигнут.",
    "UNKNOWN_ORDER_COST": "Для суммы заказа и проверки бюджета нужны подтверждённые закупочные цены в одной валюте.",
    "BUDGET_DEFERRED": "Количество уменьшено или отложено из-за общего лимита закупки. Риск дефицита сохраняется.",
    "FEASIBLE_HEURISTIC": "Общий бюджет распределён сначала на наиболее срочные товары. Ограничения соблюдены; математическая оптимальность не заявляется.",
    "SYNTHETIC_LOG_COMPLETENESS": "В учебном наборе считается, что известны все периоды отсутствия товара.",
    "STOCKOUT_LOG_COMPLETENESS_UNVERIFIED": "Полнота истории дефицита не подтверждена. Восстановление потерянного спроса не применялось.",
    "UNKNOWN_DEMAND_COVERAGE": "Есть данные только о наблюдавшихся продажах; спрос в остальные дни неизвестен. Расчёт заказа недоступен.",
    "NO_DEMAND_HISTORY": "Недостаточно истории спроса для этого товара и склада.",
    "NO_KNOWN_DEMAND_HISTORY": "История спроса неизвестна. Прогноз не был рассчитан.",
    "UNCERTAINTY_UNAVAILABLE": "Недостаточно истории для оценки изменчивости спроса и страхового запаса.",
    "NEGATIVE_NET_HISTORY": "Возвраты превышают продажи. Проверьте историю движений перед расчётом.",
    "RECOVERY_UNAVAILABLE": "Потерянный спрос неизвестен. Отсутствие расчётной поправки не означает отсутствие потерь.",
    "HISTORY_WINDOW": "Расчёт использует завершённые дни истории до даты плана; неполный текущий день исключён.",
    "SEASONALITY_NOT_POINT_IN_TIME": "Сезонность на дату расчёта не подтверждена. Дополнительная сезонная поправка не применена.",
    "IID_NORMAL_ASSUMPTION": "Страховой запас рассчитан при допущении независимых ежедневных ошибок прогноза "
                             "с нормальным распределением. Целевой уровень наличия — не измеренный результат.",
    "PROJECTED_STOCKOUT": "По прогнозу, товар закончится до поступления одной из поставок. "
                          "Более поздняя поставка не предотвратит этот дефицит.",
}


def human_issue_message(code, message=None):
    """Translate diagnostics without showing machine codes as buyer advice."""
    if code in ISSUE_MESSAGES:
        return ISSUE_MESSAGES[code]
    if isinstance(message, str) and re.search(r"[А-Яа-яЁё]", message):
        return message
    return "Есть дополнительное замечание к данным. Уточните его у ответственного за исходные данные."


def human_reasons(reasons, issues=None):
    """Do not repeat codes already explained by issues; translate remaining reasons."""
    shown_codes = {issue.get("code") for issue in issues or [] if isinstance(issue, dict)}
    result = []
    for reason in [reasons] if isinstance(reasons, str) else reasons or []:
        if not isinstance(reason, str):
            continue
        parts = reason.split()
        codes = parts if parts and all(re.fullmatch(r"[A-Z][A-Z0-9_]*", part) for part in parts) else None
        for value in codes or [reason]:
            if value in shown_codes:
                continue
            message = human_issue_message(value, None if codes else value)
            if message not in result:
                result.append(message)
    return result


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
    grouped = {}
    for issue in issues or []:
        if not isinstance(issue, dict):
            st.warning(human_issue_message(None, str(issue)))
            continue

        message = human_issue_message(issue.get("code"), issue.get("message"))
        if issue.get("severity") == "info" and str(issue.get("message", "")).startswith("Исходное ограничение:"):
            message = "Исходное ограничение: " + message + " Для этого плана вы ввели данные и подтвердили допущения."
        if issue.get("code") == "PLANNING_ASSUMPTION":
            # The backend prefixes these explanations with its internal field key.
            # The buyer needs the assumption itself, not its serialized address.
            message = str(message).partition(":")[2].strip() or "Расчёт использует указанное допущение об условиях закупки."
        if issue.get("code") == "EXCLUDED_LINES":
            count = re.match(r"^(\d+) SKU/warehouse records excluded;", str(issue.get("message", "")))
            message = (f"Исключено позиций: {count.group(1)}. " if count else "Часть товаров исключена из расчёта. ")
            message += "Проверьте причины в плане закупки: эти товары не учтены в итоге."
        scopes = issue.get("scope_ids") or []
        severity = issue.get("severity", "warning")
        grouped.setdefault((severity, message), []).extend(str(scope) for scope in scopes)
    for (severity, message), scoped in grouped.items():
        scopes = list(dict.fromkeys(scoped))
        if scopes:
            message += " · " + ", ".join(scopes[:5])
            if len(scopes) > 5:
                message += f" и ещё {len(scopes) - 5} товаров"
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
    show_error_details(getattr(error, "details", None))
    if os.getenv("BUYER_DEVELOPER_MODE", "").lower() in ("1", "true", "yes"):
        st.caption(f"Код: {getattr(error, 'code', 'UNKNOWN')}" + (f" · HTTP {status}" if status else ""))


def show_error_details(details):
    """Present actionable error fields, never raw responses or diagnostic trees."""
    if not isinstance(details, dict):
        return
    field_names = {"purchase_uom": "единица закупки", "base_units_per_purchase_uom": "пересчёт единиц",
                   "quantity_quantum": "шаг базовой единицы", "free_base": "доступный остаток",
                   "moq_purchase": "минимальная партия", "pack_multiple_purchase": "кратность упаковки",
                   "lead_time_days": "срок поставки", "review_days": "период проверки",
                   "cost_per_base": "закупочная цена", "currency": "валюта", "incoming_eta": "дата поставки",
                   "incoming_base_qty": "количество в пути"}
    if details.get("sku_id"):
        st.caption("Товар: " + str(details["sku_id"]))
    if isinstance(details.get("fields"), list):
        fields = [field_names[field] for field in details["fields"] if field in field_names]
        if fields:
            st.warning("Заполните: " + ", ".join(fields) + ".")
    for reason in human_reasons(details.get("reasons")):
        st.warning(reason)
    version = details.get("current_version")
    if isinstance(version, int) and not isinstance(version, bool):
        st.caption(f"Текущая версия заказа: {version}.")
    errors = details.get("errors")
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, dict):
            message = error.get("msg") or error.get("message")
            if isinstance(message, str):
                st.warning(message)


def show_quality(quality):
    quality = quality or {}
    labels = {"ready": "Готовы", "degraded": "Есть ограничения", "blocked": "Требуют дополнения"}
    st.write("**Качество данных:** " + labels.get(quality.get("status"), "Не сообщено сервером"))
    counts = [(title, format_decimal(quality[field])) for field, title in
              (("accepted_rows", "Принято строк"), ("rejected_rows", "Пропущено строк"),
               ("affected_skus", "Товаров с замечаниями")) if quality.get(field) is not None]
    if counts:
        st.caption(" · ".join(f"{title}: {value}" for title, value in counts))
    caps = quality.get("capabilities") or {}
    can_plan = caps.get("can_plan")
    if can_plan is True:
        st.write("Можно рассчитать закупку.")
    elif can_plan is False:
        st.info("Для расчёта нужно дополнить исходные данные. Причины и доступные действия указаны ниже.")
    else:
        st.warning("Готовность к расчёту ещё не подтверждена.")
    rows = []
    for field, title in (("can_plan", "Расчёт заказа"), ("budget_available", "Оценка бюджета"),
                         ("can_approve", "Утверждение"), ("observed_stockouts_available", "Наблюдения дефицита"),
                         ("customer_detection_available", "Классификация по клиентам")):
        value = caps.get(field)
        rows.append({"Возможность": title, "Доступность": "Да" if value is True else "Нет" if value is False else "Не сообщено"})
    reasons = human_reasons(caps.get("reasons"), quality.get("issues"))
    if reasons:
        st.caption(" ".join(reasons))
    show_issues(quality.get("issues"))
    with st.expander("Доступные расчёты и проверки"):
        st.dataframe(rows, hide_index=True, width="stretch")
        st.caption("Месячный остаток не подтверждает текущий запас или дни отсутствия товара. "
                   "Готовность прогноза отдельно не передана.")
