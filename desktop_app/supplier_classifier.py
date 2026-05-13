from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from desktop_app.constants import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
from desktop_app.document_text import build_documents_text
from desktop_app.lm_table_analysis import call_lm_studio_chat


PRODUCER_LABEL = "01 Производитель МТР"
SERVICE_EXECUTOR_LABEL = "06 Исполнитель услуг (собственными силами)"


@dataclass
class SupplierCharacteristic:
    label: str = ""
    confidence: float = 0.0
    source: str = "lm_studio"
    reason: str = ""
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(float(self.confidence), 2),
            "source": self.source,
            "reason": self.reason,
            "files": self.files,
        }


def classify_supplier_characteristic(technical_dir: Path, progress=None) -> SupplierCharacteristic:
    files = _collect_files(technical_dir)
    result = SupplierCharacteristic(files=[str(path) for path in files])
    if not files:
        result.reason = f"В папке технических документов нет файлов: {technical_dir}"
        return result

    if progress:
        progress("Извлекаю текст технических документов для определения характеристики поставщика...")
    text = build_documents_text(files, progress=None)
    if not text.strip():
        result.reason = "Не удалось извлечь текст из технических документов."
        return result

    rule_label, rule_confidence = _fallback_by_rules(text)
    if rule_label and rule_confidence >= 0.85:
        result.label = rule_label
        result.confidence = rule_confidence
        result.source = "rules"
        result.reason = "Характеристика определена по явным ключевым признакам в технических документах."
        return result

    if progress:
        progress("Определяю характеристику поставщика через LM Studio...")
    try:
        parsed = _ask_lm_studio(text)
        result.label = _normalize_label(str(parsed.get("supplier_characteristic") or ""))
        result.confidence = _safe_confidence(parsed.get("confidence"))
        result.reason = str(parsed.get("reason") or "").strip()
    except Exception as e:
        result.source = "rules_fallback"
        result.reason = f"LM Studio не смог определить характеристику: {e}"
        result.label, result.confidence = _fallback_by_rules(text)

    if not result.label:
        result.label, result.confidence = _fallback_by_rules(text)
        if result.label:
            result.source = result.source + "+rules"
            result.reason = (result.reason + " Использовано резервное правило.").strip()
    return result


def _collect_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and not path.name.startswith("~$")),
        key=lambda path: str(path).casefold(),
    )


def _ask_lm_studio(documents_text: str) -> dict[str, Any]:
    system = (
        "Ты классифицируешь заявку поставщика по техническим документам для ЭТП ТЭК-Торг. "
        "Нужно выбрать строго один вариант характеристики поставщика: "
        f"1) {PRODUCER_LABEL}; 2) {SERVICE_EXECUTOR_LABEL}. "
        "Если документы описывают поставку оборудования/МТР/товара, изготовление или производителя продукции, выбирай производителя МТР. "
        "Если основная суть документов — выполнение работ или оказание услуг собственными силами, выбирай исполнителя услуг. "
        "Ответь строго JSON без markdown с ключами supplier_characteristic, confidence, reason. "
        "supplier_characteristic должен быть ровно одним из двух вариантов."
    )
    user = (
        "Текст технических документов:\n"
        "-----\n"
        f"{documents_text[:60000]}\n"
        "-----"
    )
    raw = call_lm_studio_chat(LM_STUDIO_BASE_URL, LM_STUDIO_MODEL, system, user, timeout_sec=240)
    return _first_json(raw)


def _first_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.I)
    if match:
        text = match.group(1).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
        if isinstance(value, dict):
            return value
    raise ValueError("В ответе LM Studio не найден JSON-объект.")


def _normalize_label(value: str) -> str:
    text = value.lower().replace("mtr", "мтр").replace("mtp", "мтр")
    if "06" in text or "исполнитель услуг" in text:
        return SERVICE_EXECUTOR_LABEL
    if "01" in text or "производитель" in text or "мтр" in text:
        return PRODUCER_LABEL
    return ""


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(str(value or "0").replace(",", "."))))
    except ValueError:
        return 0.0


def _fallback_by_rules(text: str) -> tuple[str, float]:
    low = text.lower()
    service_score = len(re.findall(r"\b(услуг|услуги|работ|шмр|пнр|монтаж|пусконалад)", low))
    producer_score = len(re.findall(r"\b(мтр|оборудован|товар|поставка|изготовител|производител|продукц)", low))
    if producer_score >= 6 and producer_score >= service_score * 3:
        return PRODUCER_LABEL, 0.9
    if service_score >= 6 and service_score >= producer_score * 3:
        return SERVICE_EXECUTOR_LABEL, 0.9
    if service_score > producer_score * 1.3:
        return SERVICE_EXECUTOR_LABEL, 0.55
    if producer_score:
        return PRODUCER_LABEL, 0.55
    return "", 0.0
