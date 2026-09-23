"""Register known supplied workbooks without treating discovery as validation."""
from pathlib import Path


def readable_name(value: str) -> str:
    try:
        restored = value.encode("mac_roman").decode("cp866")
        return restored if any("А" <= ch <= "я" for ch in restored) else value
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def discover_sources(root: Path) -> dict[str, dict]:
    specifications = [
        ("IEK", "S01", "MOQ", "supplier_terms", "Условия упаковки"),
        ("IEK", "S02", "Динамика", "sales_events", "История продаж"),
        ("IEK", "S03", "Ежемесячные остатки", "monthly_balances", "История остатков"),
        ("IEK", "S04", "Ежемесячные продажи", "monthly_sales", "Продажи по месяцам"),
        ("IEK", "S05", "Путь", "pipeline_lines", "Поставки в пути"),
        ("IEK", "S06", "Сезонность", "seasonality", "Сезонность"),
        ("Systeme electric", "S07", "MOQ", "supplier_terms", "Условия упаковки"),
        ("Systeme electric", "S08", "Динамика", "sales_events", "История продаж"),
        ("Systeme electric", "S09", "Ежемесячные остатки", "monthly_balances", "История остатков"),
        ("Systeme electric", "S10", "Ежемесячные продажи", "monthly_sales", "Продажи по месяцам"),
        ("Systeme electric", "S11", "Сезонность", "seasonality", "Сезонность"),
        ("Systeme electric", "S12", "Товар в пути", "inventory_snapshots", "Остатки и поставки: отчёт для сверки"),
    ]
    registered = {}
    for folder, source_id, prefix, kind, title in specifications:
        matches = [p for p in (root / folder).glob("*.xlsx")
                   if readable_name(p.name).startswith(prefix) and not p.name.startswith("~$")]
        if len(matches) != 1:
            continue  # Ambiguous inputs must be explicitly registered, never chosen arbitrarily.
        iek = source_id in {"S01", "S02", "S05"}
        systeme = source_id in {"S07", "S08", "S12"}
        supported = iek or systeme
        supplier = "IEK" if folder == "IEK" else "Systeme Electric"
        registered[source_id] = {
            "source_id": source_id, "name": f"{supplier} · {title}", "kind": kind,
            "path": str(matches[0].resolve()), "mode": "real_preview", "supplier_name": supplier,
            "dataset_id": "iek-preview" if iek else "systeme-preview" if systeme else "reference-files",
            "default_as_of": "2026-09-22T23:59:59+05:00",
            "mapping_version": "iek-preview-v1" if iek else "systeme-preview-v1" if systeme else None,
            "available_for_import": supported,
            "description": (
                f"История продаж {supplier}: выберите 20, 100 или все поддерживаемые товары. Заполните остатки и условия закупки, затем рассчитайте и утвердите локальный заказ."
                if supported else "Справочный файл: учтён при проверке источников; автоматическое включение в заказ пока недоступно."
            ),
        }
    for required in ({"S01", "S02", "S05"}, {"S07", "S08", "S12"}):
        if not required.issubset(registered):
            for source_id in required.intersection(registered):
                registered[source_id]["available_for_import"] = False
    return registered
