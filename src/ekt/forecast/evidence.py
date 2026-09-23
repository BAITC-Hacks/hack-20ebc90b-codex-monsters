"""Reproducible synthetic evidence for the buyer demo, without corporate inputs.

Run ``python -m ekt.forecast.evidence --output-dir /tmp/ekt-b-evidence``.
The JSON tables are also a small read-only handoff for A/C, not a new API DTO.
No order, financial benefit or real-world forecast accuracy is inferred here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from math import fsum, isclose
from pathlib import Path
from statistics import mean

from .baseline import forecast_daily
from .classification import classify_events
from .evaluation import evaluate_forecast, evaluate_recovery, rolling_origin_evaluation
from .recovery import recover_daily
from .service import MODEL_VERSION

AS_OF = "2026-09-22T23:59:59+00:00"
HISTORY_END = date(2026, 9, 23)  # Exclusive; every input day is complete.
HISTORY_START = HISTORY_END - timedelta(days=84)
SCOPE = {"sku_id": "SYNTHETIC-B-001", "warehouse_id": "SYNTHETIC-WH", "uom": "piece"}


def _events(quantities, start=HISTORY_START):
    """Ordinary transactions only: no expected label or latent-demand fields."""
    return [{
        "event_id": f"synthetic-event-{index:04d}",
        "event_at": (start + timedelta(days=index)).isoformat() + "T12:00:00Z",
        "sku_id": SCOPE["sku_id"], "warehouse_id": SCOPE["warehouse_id"],
        "base_uom": SCOPE["uom"], "quantity_base": str(quantity),
        "demand_effect": "increase", "doc_id": f"synthetic-document-{index:04d}",
    } for index, quantity in enumerate(quantities)]


def _horizon(start=HISTORY_END, days=90):
    return [start + timedelta(days=index) for index in range(days)]


def _regular_forecast(events):
    classified = classify_events(events)
    daily, _ = recover_daily(classified, [], HISTORY_START, HISTORY_END)
    return classified, daily, forecast_daily(daily, _horizon())


def _check(name, description, passed, *, expected, actual):
    return {"check_id": name, "description": description, "passed": bool(passed),
            "expected": expected, "actual": actual}


def _table(title, columns, rows):
    return {"title": title, "columns": columns, "rows": rows}


def _case(case_id, title, acceptance_ids, description, tables, checks, **details):
    return {"case_id": case_id, "title": title, "acceptance_ids": acceptance_ids,
            "description": description, "status": "PASS" if all(c["passed"] for c in checks) else "FAIL",
            "tables": tables, "checks": checks, **details}


def _project_case():
    source = _events([10] * 84)
    extra = dict(source[70], event_id="synthetic-event-0084", doc_id="synthetic-document-0084",
                 quantity_base="1000", event_at=source[70]["event_at"].replace("12:00", "13:00"))
    clean, _, clean_forecast = _regular_forecast(source)
    classified, daily, prediction = _regular_forecast(source + [extra])
    spike = next(row for row in classified if row["event_id"] == extra["event_id"])
    # The two forecasts use the exact same last 28 calendar dates. The injected
    # transaction is additional to the unchanged 10 pieces/day regular demand.
    observed = [sum(float(event["quantity_base"]) for event in source + [extra]
                    if event["event_at"][:10] == row["date"]) for row in daily[-28:]]
    naive = mean(observed)
    model = prediction["daily"][0]["mean"]
    regular = sum(row["regular_qty"] for row in classified)
    conserved = all(row["observed_qty"] == row["regular_qty"] + row["project_qty"] + row["uncertain_qty"] for row in classified)
    checks = [
        _check("blind_spike", "100-кратная разовая продажа требует проверки закупщика",
               spike["label"] == "suspected_project" and spike["review_status"] == "pending",
               expected="suspected_project / pending", actual=f"{spike['label']} / {spike['review_status']}"),
        _check("regular_preserved", "Дополнительная проектная продажа не меняет регулярные 840 единиц",
               regular == Decimal(840) and conserved, expected=840, actual=regular),
        _check("same_window_baseline", "Обычный прогноз остаётся 10; среднее включает добавленные 1000/28",
               isclose(model, 10) and isclose(naive, 10 + 1000 / 28)
               and prediction["daily"] == clean_forecast["daily"],
               expected={"b_daily": 10, "recent_mean": 10 + 1000 / 28},
               actual={"b_daily": model, "recent_mean": naive}),
    ]
    return _case("B-E01", "Разовый проект: среднее завышает регулярную потребность", ["AT-05"],
                 "84 полных дня по 10 штук; в день 71 добавлена отдельная продажа 1000 штук. "
                 "Оба метода используют последние 28 дней. Подозрение не означает подтверждённый проект.", [
                     _table("Прогноз регулярного спроса, штуки/день", ["scenario", "recent_mean_28", "b_forecast"], [
                         {"scenario": "Без дополнительной продажи", "recent_mean_28": 10, "b_forecast": clean_forecast["daily"][0]["mean"]},
                         {"scenario": "С дополнительной продажей 1000", "recent_mean_28": naive, "b_forecast": model}]),
                     _table("Разовая продажа не удалена", ["observed_qty", "regular_qty", "uncertain_qty", "label", "review_status", "confidence"],
                            [{key: spike[key] for key in ("observed_qty", "regular_qty", "uncertain_qty", "label", "review_status", "confidence")}]),
                 ], checks, recent_window={"start": daily[-28]["date"], "end_exclusive": HISTORY_END.isoformat(), "days": 28},
                 reason_codes=spike["reason_codes"], input_event_count=len(clean) + 1,
                 forecast_warnings=prediction["warnings"],
                 limitations=["Confidence — эвристический балл детектора, не откалиброванная "
                              "вероятность подтверждённого проекта."])


def _recurrence_case():
    recurrent_days = {28, 49, 70}
    examples = [
        ("Повторные крупные продажи", [1000 if day in recurrent_days else 10 for day in range(84)], recurrent_days),
        ("Устойчивый уровень 10 → 30", [10] * 56 + [30] * 28, set(range(56, 84))),
    ]
    tables, checks = [], []
    for index, (title, quantities, selected) in enumerate(examples, 1):
        classified, _, prediction = _regular_forecast(_events(quantities))
        rows = [classified[day] for day in sorted(selected)]
        total = sum(row["observed_qty"] for row in rows)
        regular = sum(row["regular_qty"] for row in rows)
        tables.append({"scenario": title, "changed_days": len(rows), "observed_qty": total,
                       "regular_qty": regular, "project_or_uncertain_qty": total - regular,
                       "b_next_day_forecast": prediction["daily"][0]["mean"]})
        checks.append(_check(f"recurrence_{index}", f"{title}: классификатор сохраняет все продажи регулярными",
                             all(row["label"] == "regular" for row in rows) and regular == sum(quantities[day] for day in selected),
                             expected={"regular_count": len(selected), "regular_qty": sum(quantities[day] for day in selected)},
                             actual={"regular_count": sum(row["label"] == "regular" for row in rows), "regular_qty": regular}))
        if index == 2:
            checks.append(_check("new_level", "Последние 28 дней по 30 дают новый прогноз 30",
                                 isclose(prediction["daily"][0]["mean"], 30), expected=30,
                                 actual=prediction["daily"][0]["mean"]))
    return _case("B-E02", "Контрпримеры: повторяемость и новый уровень", ["AT-05"],
                 "Одинаковые крупные покупки в три разные даты сохраняются классификатором. "
                 "Отдельный ряд повышается с 10 до 30 штук на последние 28 дней.",
                 [_table("Классификация и следующий прогноз", list(tables[0]), tables)], checks,
                 limitations=["Сохранение регулярной allocation не обещает переноса всей суммы в прогноз: "
                              "robust baseline отдельно ограничивает редкие экстремальные значения. "
                              "Повторные крупные заказы требуют проверки закупщиком."])


def _recovery_case():
    hidden_days = (70, 71, 72)
    control_day = 69
    observed = [10] * 84
    observed[control_day] = 0
    for day in hidden_days:
        observed[day] = 0
    source = _events(observed)
    classified = classify_events(source)
    interval = {"sku_id": SCOPE["sku_id"], "warehouse_id": SCOPE["warehouse_id"],
                "start_at": (HISTORY_START + timedelta(days=70)).isoformat(),
                "end_at": (HISTORY_START + timedelta(days=73)).isoformat(), "unavailable_fraction": 1}
    daily, summary = recover_daily(classified, [interval], HISTORY_START, HISTORY_END)
    duplicate, _ = recover_daily(classified, [interval, interval], HISTORY_START, HISTORY_END)
    # This oracle is independent of the donor estimate. It is deliberately not
    # equal to 10: a useful recovery estimate is not known lost demand.
    oracle = [12, 11, 13]
    recovered = [float(daily[day]["corrected_demand"]) for day in hidden_days]
    score = evaluate_recovery(recovered, oracle, truth_source="synthetic_latent_truth", uom=SCOPE["uom"])
    zero = daily[control_day]
    checks = [
        _check("positive_loss", "Во время известного отсутствия восстановление положительно",
               all(daily[day]["estimated_lost"] > 0 and daily[day]["corrected_demand"] > daily[day]["observed_regular"] for day in hidden_days),
               expected="3 дня с положительной оценкой потерь", actual=[daily[day]["estimated_lost"] for day in hidden_days]),
        _check("available_zero", "Нулевой спрос при наличии остаётся нулевым",
               zero["estimated_lost"] == 0 and zero["corrected_demand"] == 0,
               expected={"lost": 0, "corrected": 0}, actual={"lost": zero["estimated_lost"], "corrected": zero["corrected_demand"]}),
        _check("interval_union", "Дублированный интервал не удваивает оценку потерь", daily == duplicate,
               expected="одинаковый дневной ряд", actual="одинаковый" if daily == duplicate else "различается"),
        _check("recovery_identity", "Исправленный спрос = наблюдаемый + оценка потерь",
               all(row["corrected_demand"] == row["observed_regular"] + row["estimated_lost"] for row in daily),
               expected=True, actual=all(row["corrected_demand"] == row["observed_regular"] + row["estimated_lost"] for row in daily)),
    ]
    columns = ["date", "observed_regular", "estimated_lost", "corrected_demand", "unavailable_fraction", "recovery_status", "donor_count"]
    return _case("B-E03", "Stockout: оценка скрытого спроса и нулевой контроль", ["AT-06"],
                 "84 полностью покрытых дня; известное отсутствие на 3 дня. Контрольный ноль находится "
                 "в доступном дне. Независимая скрытая истина передана только оценке качества.", [
                     _table("Наблюдение → потери → исправленный спрос", columns,
                            [{key: daily[day][key] for key in columns} for day in range(69, 74)]),
                     _table("Независимая оценка, штуки", ["date", "latent_truth", "recovered"],
                            [{"date": daily[day]["date"], "latent_truth": truth, "recovered": estimate}
                             for day, truth, estimate in zip(hidden_days, oracle, recovered)]),
                 ], checks, recovery_metrics=score, diagnostics=summary,
                 limitations=["12/11/13 — синтетическая скрытая истина, не вход восстановления. "
                              "Положительное восстановление не доказывает точную величину потерь. "
                              "На реальных данных без stockout logs этот механизм недоступен."])


def _components_case():
    history = [{"date": (HISTORY_START + timedelta(days=index)).isoformat(), "corrected_demand": 10} for index in range(84)]
    seasonality = {"verified": True, "provenance": "synthetic", "source": "analytical-scenarios-v1",
                   "factors": {str(month): 1.2 if month == 10 else 1 for month in range(1, 13)}}
    growth = [{"category_id": "SYNTHETIC-CATEGORY", "mode": "replace", "rate": 0.1,
               "valid_from": HISTORY_END.isoformat(), "valid_to": _horizon()[-1].isoformat()}]
    settings = [{}, {"seasonality": seasonality}, {"seasonality": seasonality, "growth_overrides": growth}]
    variants = [forecast_daily(history, _horizon(), category_id="SYNTHETIC-CATEGORY", **setting) for setting in settings]
    selected = [next(row for row in result["daily"] if row["date"] == "2026-10-01") for result in variants]
    rows = [dict(scenario=title, **row) for title, row in zip(("Без поправок", "Сезонный фактор 1.2", "Сезонность + рост replace 10%"), selected)]
    sums_match = all(isclose(row["mean"], row["baseline_mean"] + row["seasonal_delta"] + row["growth_delta"], abs_tol=1e-9)
                     for result in variants for row in result["daily"])
    checks = [
        _check("independent_components", "На 1 октября: 10 → 12 → 13.2; рост применяется один раз",
               all(isclose(row["mean"], expected, abs_tol=1e-9) for row, expected in zip(selected, (10, 12, 13.2))),
               expected=[10, 12, 13.2], actual=[row["mean"] for row in selected]),
        _check("additive_components", "На всех 90 датах baseline + seasonal + growth = mean", sums_match,
               expected=True, actual=sums_match),
        _check("complete_horizon", "90 последовательных дат без пропусков",
               all([row["date"] for row in result["daily"]] == [day.isoformat() for day in _horizon()] for result in variants),
               expected=90, actual=[len(result["daily"]) for result in variants]),
    ]
    return _case("B-E04", "Сезонность и рост объясняются отдельно", ["AT-04"],
                 "Ровная история 10 штук/день. Фактор октября 1.2 и будущий рост replace 10% заданы "
                 "синтетически; это проверка механизма, а не выученная рыночная сезонность.",
                 [_table("Компоненты на контрольную дату, штуки/день", ["scenario", "date", "baseline_mean", "seasonal_delta", "growth_delta", "mean"], rows)],
                 checks, forecast_warnings=variants[-1]["warnings"], uncertainty=variants[-1]["metadata"]["uncertainty_assumption"])


def _backtest_forecaster(train, horizon):
    start = date(2026, 1, 1)
    history = [{"date": (start + timedelta(days=index)).isoformat(), "corrected_demand": value} for index, value in enumerate(train)]
    predicted = forecast_daily(history, _horizon(start + timedelta(days=len(train)), horizon))
    return [row["mean"] for row in predicted["daily"]]


def _evaluation_case():
    history = [10 + 0.05 * index + 2 * (index % 7 == 0) for index in range(180)]
    result = rolling_origin_evaluation(history, [120, 150], 14, _backtest_forecaster, uom=SCOPE["uom"])
    changed_future = history[:164] + [99999] * 16
    replay = rolling_origin_evaluation(changed_future, [120, 150], 14, _backtest_forecaster, uom=SCOPE["uom"])
    rows, scales_match = [], True
    for origin in result["origins"]:
        cutoff = origin["origin"]
        expected_scale = fsum(abs(history[index] - history[index - 1]) for index in range(1, cutoff)) / (cutoff - 1)
        for model, output in origin["models"].items():
            metrics = output["metrics"]
            scales_match &= isclose(metrics["mase_train_scale"], expected_scale, abs_tol=1e-12) and metrics["train_n"] == cutoff
            rows.append({"train_days": cutoff, "holdout_start": (date(2026, 1, 1) + timedelta(days=cutoff)).isoformat(),
                         "horizon_days": 14, "model": model, "wape": metrics["wape"], "bias": metrics["bias"],
                         "mase": metrics["mase"], "mase_train_scale": metrics["mase_train_scale"]})
    undefined = evaluate_forecast([0, 0], [0, 0], train=[0, 0, 0], uom=SCOPE["uom"])
    checks = [
        _check("chronological_holdouts", "Два непересекающихся holdout по 14 дней после обучающего префикса",
               [origin["origin"] for origin in result["origins"]] == [120, 150]
               and all(len(origin["actual"]) == 14 for origin in result["origins"]),
               expected=[120, 150], actual=[origin["origin"] for origin in result["origins"]]),
        _check("train_only_scale", "Знаменатель MASE вычислен только на train", scales_match,
               expected=True, actual=scales_match),
        _check("future_independence", "Изменение данных после обоих holdout не меняет результат", result == replay,
               expected=True, actual=result == replay),
        _check("undefined_not_zero", "Нулевые знаменатели дают null, а не идеальную точность",
               all(undefined[name] is None for name in ("wape", "bias", "mase")),
               expected={name: None for name in ("wape", "bias", "mase")},
               actual={name: undefined[name] for name in ("wape", "bias", "mase")}),
    ]
    return _case("B-E05", "Честное сравнение с простым средним", [],
                 "Независимый ряд: y[i] = 10 + 0.05*i + 2*(i % 7 == 0), 180 дней от 2026-01-01. "
                 "Forecaster получает только train-prefix. WAPE/bias — доли, не проценты; "
                 "положительный bias означает завышение. Превосходство модели не является условием PASS.",
                 [_table("Два rolling origins, одна UOM piece", list(rows[0]), rows)], checks,
                 evaluation=result, zero_denominator_control=undefined,
                 limitations=["Два выбранных синтетических отрезка не оценивают реальную точность, "
                              "денежную выгоду или достигнутый сервис. MASE > 1 означает, что ошибка "
                              "на holdout выше средней lag-1 ошибки на train. Это не сравнение "
                              "с отдельным наивным прогнозом на holdout; выигрыш над mean28 не отменяет этого."])


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def build_evidence():
    """Return deterministic, JSON-safe evidence; no files/network are read."""
    cases = [_project_case(), _recurrence_case(), _recovery_case(), _components_case(), _evaluation_case()]
    report = {
        "schema_version": "b-evidence-v1", "generator_version": "analytical-scenarios-v1",
        "mode": "synthetic_demo", "as_of": AS_OF, "model_version": MODEL_VERSION,
        "seed": 42, "seed_usage": "Analytical deterministic inputs; no random draws.",
        "scope": SCOPE, "history_start": HISTORY_START.isoformat(), "history_end_exclusive": HISTORY_END.isoformat(),
        "status": "PASS" if all(case["status"] == "PASS" for case in cases) else "FAIL",
        "criteria": ["ценность", "результат и качество", "инновационность", "презентация/демо"],
        "cases": cases,
        "limitations": [
            "Все числа и документы синтетические. Это доказательство работы механизмов, не результат компании.",
            "PASS означает перечисленные инварианты, а не прохождение всей приёмки A/B/C или качество на реальных данных.",
            "Отчёт вызывает реальные classify_events/recover_daily/forecast_daily/evaluation; API, planner и UI проверяются отдельно.",
            "Неопределённость IID-normal не откалибрована; 95%/99% — целевые настройки, не измеренный сервис.",
            "Реальные данные без подтверждённого покрытия, остатков и условий остаются ограниченным preview.",
        ],
    }
    serial = json.dumps(report, default=_json_default, sort_keys=True, ensure_ascii=False, allow_nan=False)
    result = json.loads(serial)
    result["report_sha256"] = hashlib.sha256(serial.encode()).hexdigest()
    return result


def _display(value):
    if value is None:
        return "не определено"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        def rounded(item):
            if isinstance(item, float):
                return round(item, 6)
            if isinstance(item, dict):
                return {key: rounded(value) for key, value in item.items()}
            return [rounded(value) for value in item] if isinstance(item, list) else item

        value = json.dumps(rounded(value), ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report):
    labels = {
        "scenario": "Пример", "recent_mean_28": "Среднее за 28 дней", "b_forecast": "Прогноз B",
        "observed_qty": "Продажи", "regular_qty": "Регулярная часть", "uncertain_qty": "На проверку",
        "label": "Метка", "review_status": "Проверка", "confidence": "Уверенность детектора",
        "changed_days": "Изменённых дней", "project_or_uncertain_qty": "Проект / на проверку",
        "b_next_day_forecast": "Прогноз B на следующий день", "date": "Дата",
        "observed_regular": "Наблюдаемый спрос", "estimated_lost": "Оценка потерь",
        "corrected_demand": "Исправленный спрос", "unavailable_fraction": "Доля отсутствия",
        "recovery_status": "Метод", "donor_count": "Дней-доноров", "latent_truth": "Скрытая истина",
        "recovered": "Восстановлено", "baseline_mean": "База", "seasonal_delta": "Сезонная прибавка",
        "growth_delta": "Прибавка роста", "mean": "Итог", "train_days": "Дней train",
        "holdout_start": "Начало holdout", "horizon_days": "Горизонт, дней", "model": "Метод",
        "wape": "WAPE", "bias": "Bias", "mase": "MASE", "mase_train_scale": "Шкала MASE из train",
    }
    lines = ["# Доказательства B: данные и прогноз", "", "**DEMO — все данные синтетические. Не измерение бизнес-эффекта.**", "",
             f"Статус инвариантов: **{report['status']}** · модель `{report['model_version']}` · as_of `{report['as_of']}` · seed {report['seed']}.",
             "", f"Report SHA256: `{report['report_sha256']}`", "",
             "PASS относится к перечисленным проверкам. Полный путь API → UI → утверждение → CSV принимается отдельно."]
    for case in report["cases"]:
        lines.extend(["", f"## {case['case_id']} · {case['title']} · {case['status']}", "", case["description"]])
        if case["acceptance_ids"]:
            lines.extend(["", "Связь с приёмкой: " + ", ".join(case["acceptance_ids"]) + "."])
        for table in case["tables"]:
            columns = table["columns"]
            lines.extend(["", f"**{table['title']}**", "", "| " + " | ".join(labels.get(column, column) for column in columns) + " |",
                          "| " + " | ".join("---" for _ in columns) + " |"])
            lines.extend("| " + " | ".join(_display(row.get(column)) for column in columns) + " |" for row in table["rows"])
        if case.get("recovery_metrics"):
            score = case["recovery_metrics"]
            lines.extend(["", f"Независимая оценка восстановления: MAE={_display(score['mae'])} piece; WAPE={_display(score['wape'])}; bias={_display(score['bias'])}."])
        lines.extend(["", "Проверки:", ""])
        lines.extend(f"- {'PASS' if check['passed'] else 'FAIL'} — {check['description']}; ожидание: {_display(check['expected'])}; получено: {_display(check['actual'])}." for check in case["checks"])
        if case.get("limitations"):
            lines.extend(["", *case["limitations"]])
    lines.extend(["", "## Ограничения", "", *[f"- {item}" for item in report["limitations"]], ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic-only reproducible evidence for B; no raw files or orders.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build_evidence()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "forecast-evidence.json"
    markdown_path = args.output_dir / "forecast-evidence.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "mode": report["mode"], "report_sha256": report["report_sha256"],
                      "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
