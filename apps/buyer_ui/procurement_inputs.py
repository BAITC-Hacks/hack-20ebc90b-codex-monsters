"""Buyer-entered procurement inputs, saved as an immutable server snapshot."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from .client import ApiError
from .formatting import format_date, show_api_error
from .secondary_views import _fingerprint, _request_key, _select_snapshot


FIELDS = {
    "purchase_uom": "Ед. закупки",
    "base_units_per_purchase_uom": "Базовых единиц в закупочной",
    "quantity_quantum": "Шаг базовой единицы",
    "free_base": "Доступный остаток",
    "moq_purchase": "Минимальная партия",
    "pack_multiple_purchase": "Кратность упаковки",
    "lead_time_days": "Срок поставки, дней",
    "review_days": "Период проверки, дней",
    "cost_per_base": "Цена за базовую единицу",
    "currency": "Валюта",
    "incoming_base_qty": "В пути, базовых единиц",
    "incoming_eta": "Прибытие (ГГГГ-ММ-ДД)",
}
DECIMALS = {"base_units_per_purchase_uom", "quantity_quantum", "free_base", "moq_purchase",
            "pack_multiple_purchase", "cost_per_base", "incoming_base_qty"}
REQUIRED = set(FIELDS) - {"cost_per_base", "currency", "incoming_base_qty", "incoming_eta"}
READONLY = {"sku_id": "Артикул", "name": "Товар", "warehouse_id": "Склад", "base_uom": "Ед. склада", "supplier_id": "Поставщик"}
COMMON = ("purchase_uom", "base_units_per_purchase_uom", "quantity_quantum", "moq_purchase",
          "pack_multiple_purchase", "lead_time_days", "review_days", "currency")


def editable_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Null remains blank and decimals remain exact text; no guessed procurement values."""
    return [{key: "" if item.get(key) is None else str(item[key])
             for key in (*READONLY, *FIELDS)} for item in items]


def fill_common(rows: list[dict[str, Any]], common: dict[str, str]) -> list[dict[str, Any]]:
    """Only fill explicitly entered shared terms; individual values take precedence."""
    return [{**row, **{key: value.strip() for key, value in common.items()
                       if value.strip() and (row.get(key) is None or pd.isna(row.get(key)) or not str(row[key]).strip())}}
            for row in rows]


def input_payload(rows: list[dict[str, Any]], reason: str, accepted: bool) -> dict[str, Any]:
    if not accepted:
        raise ValueError("Подтвердите допущение об истории продаж, чтобы продолжить расчёт.")
    if not reason.strip():
        raise ValueError("Укажите источник остатков и условий либо причину принятых допущений.")
    if not rows:
        raise ValueError("Выберите хотя бы один товар для закупки.")
    items = []
    for row in rows:
        item = {key: str(row[key]) for key in READONLY}
        for field, label in FIELDS.items():
            raw = row.get(field)
            text = "" if raw is None or pd.isna(raw) else str(raw).strip()
            if not text:
                if field in REQUIRED:
                    raise ValueError(f"{row['sku_id']}: заполните поле «{label}». Ноль указывайте только при подтверждённом отсутствии.")
                item[field] = None if field != "incoming_base_qty" else "0"
                continue
            if field in DECIMALS or field in {"lead_time_days", "review_days"}:
                try:
                    number = Decimal(text.replace(",", ".").replace(" ", "").replace("\u202f", ""))
                    if not number.is_finite():
                        raise InvalidOperation
                    if field in {"lead_time_days", "review_days"}:
                        if number != number.to_integral_value():
                            raise InvalidOperation
                        item[field] = int(number)
                    else:
                        item[field] = str(number)
                except (InvalidOperation, ValueError):
                    raise ValueError(f"{row['sku_id']}: в поле «{label}» нужно число" +
                                     (" целых дней." if field in {"lead_time_days", "review_days"} else ".")) from None
            else:
                item[field] = text.upper() if field == "currency" else text
        items.append(item)
    return {"items": items, "reason": reason.strip(), "accept_history_estimate": True}


def render_procurement_inputs(client: Any, snapshot: dict[str, Any]) -> None:
    snapshot_id = snapshot["snapshot_id"]
    if snapshot.get("mode") != "real_preview":
        return
    with st.expander("Остатки и условия закупки", expanded=not snapshot.get("quality", {}).get("capabilities", {}).get("can_plan")):
        st.write("**Подтвердите исходные данные для заказа**")
        st.caption("Заполните условия выбранных товаров. Пустая ячейка означает, что данных нет. "
                   "Остаток и цена указаны за базовую единицу склада; партия и упаковка — в единицах закупки. "
                   "Изменения создадут отдельный план, прежние заказы сохранятся.")
        try:
            catalog = client.get_buyer_inputs(snapshot_id)
        except ApiError as error:
            show_api_error(error)
            if st.button("Повторить загрузку условий", key="buyer_inputs_retry"):
                st.rerun()
            return
        items = catalog.get("items", [])
        if not items:
            st.info("В данных нет товаров для заполнения. Подготовьте данные из другого источника ниже.")
            return
        if catalog.get("history_start") and catalog.get("history_end"):
            st.caption(f"История продаж: {format_date(catalog['history_start'])} — {format_date(catalog['history_end'])}.")
        identities = {f"{item['sku_id']} · {item['warehouse_id']}": item for item in items}
        scope_mode = st.radio("Охват нового плана", ["all", "selected"], horizontal=True,
                              format_func=lambda value: f"Все товары ({len(items)})" if value == "all" else "Выбрать товары",
                              key=f"buyer_input_scope:{snapshot_id}")
        selected = list(identities)
        if scope_mode == "selected":
            selected = st.multiselect("Найдите товары по артикулу или названию", list(identities),
                                      format_func=lambda key: f"{identities[key].get('name', '')} · {key}",
                                      key=f"buyer_input_selection:{snapshot_id}",
                                      help="Остальные товары не попадут в новый план. Можно вернуться к полному ассортименту выше.")
        st.caption(f"В новый план войдёт товаров: {len(selected)}.")
        scope = _fingerprint({"selected": selected})
        with st.form(f"buyer_input_form:{snapshot_id}", border=False):
            with st.expander("Общие условия для выбранных товаров — необязательно"):
                st.caption("При сохранении заполним только пустые ячейки таблицы. Индивидуальные значения имеют приоритет. "
                           "Указывайте только общие для выбранных товаров условия; остаток каждого товара заполните отдельно.")
                columns = st.columns(3)
                common = {field: columns[index % 3].text_input(FIELDS[field], key=f"buyer_common:{snapshot_id}:{field}")
                          for index, field in enumerate(COMMON)}
            frame = pd.DataFrame(editable_rows([identities[key] for key in selected]), columns=[*READONLY, *FIELDS])
            edited = st.data_editor(frame, hide_index=True, width="stretch", height=min(520, max(170, len(frame) * 35 + 40)),
                                    disabled=list(READONLY), num_rows="fixed",
                                    column_config={key: st.column_config.TextColumn(label, help="Обязательное поле" if key in REQUIRED else None)
                                                   for key, label in {**READONLY, **FIELDS}.items()},
                                    key=f"buyer_input_table:{snapshot_id}:{scope}")
            st.caption("Таблица прокручивается по горизонтали. Цена необязательна: без неё заказ можно утвердить и скачать, но сумма и бюджет не рассчитываются.")
            reason = st.text_area("Источник данных и обоснование", placeholder="Например: остатки сверены со складом, сроки и партии подтверждены поставщиком",
                                  key=f"buyer_input_reason:{snapshot_id}")
            accepted = st.checkbox("Для расчёта считаю дни без записей нулевыми продажами; понимаю, что возвраты и неподтверждённые движения не учтены",
                                   key=f"buyer_input_accept:{snapshot_id}")
            submit = st.form_submit_button("Сохранить условия и рассчитать заказ", type="primary")
        if not submit:
            return
        try:
            payload = input_payload(fill_common(edited.to_dict("records"), common), reason, accepted)
        except ValueError as error:
            st.error(str(error))
            return
        payload["idempotency_key"] = _request_key("buyer_inputs:" + snapshot_id, payload)
        try:
            with st.spinner("Сохраняем подтверждённые данные…"):
                saved = client.save_buyer_inputs(snapshot_id, payload)
            if not saved.get("snapshot_id"):
                raise ApiError(None, "INVALID_RESPONSE", "Сервер не подтвердил сохранение. Повторите действие с теми же данными.", ambiguous=True)
            _select_snapshot(saved["snapshot_id"])
            st.session_state.pop("buyer_workspace", None)
            from .workspace import start_plan
            policy = st.session_state.get("buyer_pending_policy") or {
                "service_metric": "cycle_service", "service_target": 0.95,
                "lead_time_delay_days": 0, "policy_version": "mvp-v1",
            }
            start_plan(client, saved["snapshot_id"], policy)
            st.session_state["pending_page"] = "Данные"
            st.rerun()
        except ApiError as error:
            show_api_error(error)
