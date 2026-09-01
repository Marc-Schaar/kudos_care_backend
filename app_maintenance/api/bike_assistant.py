"""
"Kudo" — der KI-Assistent, der Laien ein komplettes Bike vorbefuellt.

Zwei Schritte:
1. `suggest_models()`  — Hersteller + Baujahr -> Liste plausibler Modelle zur Auswahl.
2. `suggest_setup()`   — gewaehltes Modell -> Vorbelegung fuer den bestehenden
   Setup-Stepper (welche Katalog-Templates, mit welcher Marke/Modell/Lebensdauer).

WICHTIGE GRENZE: Die KI darf **nur aus dem bestehenden Katalog auswaehlen**. Sie bekommt
die erlaubten `ComponentGroup`/`ComponentTemplate`-IDs im Prompt und darf ausschliesslich
diese referenzieren. `_filter_to_catalog()` verwirft anschliessend serverseitig jede ID,
die nicht in der erlaubten Menge liegt — halluzinierte Templates erreichen die DB also
selbst dann nicht, wenn das Modell sich nicht an den Prompt haelt. Dasselbe Schutzmuster
wie `views.py::_validate_assembly_items()`.

Die Marken-/Modellangaben je Zeile sind dagegen echtes Modellwissen und damit ein
bewusster Bruch mit der sonstigen Regel "die KI erfindet nichts": sie sind Vorschlaege,
die der Nutzer im Stepper Zeile fuer Zeile korrigieren kann. Deshalb liefert jede Zeile
ein `confidence`-Feld, das die UI als "bitte pruefen" ausweisen kann.
"""

import logging

from app_maintenance.models import (
    ComponentGroup,
    ComponentTemplate,
    MaintenanceKind,
)
from .ai_providers import get_ai_provider

logger = logging.getLogger("my_app_debug")

# Mehr Modellvorschlaege als das ueberfordern die Auswahl mehr als sie hilft.
MAX_MODEL_SUGGESTIONS = 8

VALID_CONFIDENCE = {"high", "medium", "low"}


def suggest_models(
    manufacturer: str, year: int | None, bike_type: str
) -> list[dict] | None:
    """
    Schritt 1: plausible Modelle eines Herstellers fuer Bike-Typ und Baujahr.

    Gibt None zurueck, wenn keine KI verfuegbar ist (fehlender Key, Timeout, kaputtes
    JSON) — der Aufrufer liefert dann 503 und das Frontend faellt auf die manuelle
    Einrichtung zurueck.
    """
    system_prompt = (
        "Du bist ein Fahrrad-Experte und hilfst beim Anlegen eines Fahrrads in einer "
        "Wartungs-App. Du bekommst Hersteller, Baujahr und Fahrradtyp und nennst "
        "plausible Modellreihen dieses Herstellers, die dazu passen. Antworte "
        "ausschliesslich mit einem JSON-Objekt der Form "
        '{"models": [{"model": "...", "year_range": "...", "note": "..."}]}. '
        f"Nenne hoechstens {MAX_MODEL_SUGGESTIONS} Modelle, das gaengigste zuerst. "
        "'note' ist eine sehr kurze deutsche Einordnung (max. 8 Woerter). Kennst du den "
        'Hersteller nicht, gib {"models": []} zurueck — erfinde keine Hersteller.'
    )
    user_prompt = (
        f"Hersteller: {manufacturer}\n"
        f"Baujahr: {year if year else 'unbekannt'}\n"
        f"Fahrradtyp: {bike_type}\n"
    )

    data = get_ai_provider().generate_json(system_prompt, user_prompt)
    if data is None:
        return None

    models = data.get("models")
    if not isinstance(models, list):
        logger.warning("Kudo-Modellvorschlaege ohne 'models'-Liste: %r", data)
        return []

    results = []
    for entry in models[:MAX_MODEL_SUGGESTIONS]:
        if not isinstance(entry, dict) or not entry.get("model"):
            continue
        results.append(
            {
                "model": str(entry["model"])[:100],
                "year_range": str(entry.get("year_range") or "")[:50],
                "note": str(entry.get("note") or "")[:120],
            }
        )
    return results


def _catalog_for_bike_type(bike_type: str) -> list[ComponentGroup]:
    """Baugruppen + Templates, die zu diesem Bike-Typ passen — wie ComponentGroupListView."""
    groups = ComponentGroup.objects.prefetch_related("templates").order_by(
        "sort_order", "name"
    )
    return [group for group in groups if group.applies_to(bike_type)]


def _catalog_prompt_block(groups: list[ComponentGroup], bike_type: str) -> str:
    """Der erlaubte Katalog als Text — die KI darf nur diese IDs referenzieren."""
    lines = []
    for group in groups:
        lines.append(f"Baugruppe {group.id}: {group.name}")
        for template in group.templates.all():
            if not template.applies_to(bike_type):
                continue
            kind = (
                "Verschleissteil"
                if template.maintenance_kind == MaintenanceKind.PART
                else "Verbrauchsmaterial"
            )
            lifetime = (
                f"{template.warn_km or '?'} km / {template.warn_days or '?'} Tage"
            )
            lines.append(
                f"  - Template {template.id}: {template.name} [{kind}, "
                f"Standard-Lebensdauer {lifetime}]"
            )
    return "\n".join(lines)


def _allowed_template_ids(
    groups: list[ComponentGroup], bike_type: str
) -> dict[int, dict]:
    """Erlaubte Template-IDs -> Gruppen-/Art-Zuordnung, für die Validierung der Antwort."""
    allowed: dict[int, dict] = {}
    for group in groups:
        for template in group.templates.all():
            if template.applies_to(bike_type):
                allowed[template.id] = {
                    "group_id": group.id,
                    "maintenance_kind": template.maintenance_kind,
                }
    return allowed


def _coerce_confidence(value) -> str:
    text = str(value or "").lower()
    return text if text in VALID_CONFIDENCE else "medium"


def _clean_row(
    entry: dict, allowed: dict[int, dict], group_id: int, kind: str
) -> dict | None:
    """
    Prueft eine einzelne KI-Zeile gegen den Katalog. Verwirft alles, was nicht zu dieser
    Gruppe und dieser Art gehoert — das ist die Stelle, an der halluzinierte oder
    verwechselte Template-IDs herausfallen.
    """
    try:
        template_id = int(entry.get("template_id"))
    except (TypeError, ValueError):
        return None

    meta = allowed.get(template_id)
    if meta is None or meta["group_id"] != group_id or meta["maintenance_kind"] != kind:
        logger.info(
            "Kudo schlug Template %s ausserhalb von Gruppe %s (%s) vor, verworfen.",
            template_id,
            group_id,
            kind,
        )
        return None

    row = {
        "template_id": template_id,
        "include": bool(entry.get("include", True)),
        "confidence": _coerce_confidence(entry.get("confidence")),
        "note": str(entry.get("note") or "")[:120],
    }
    if kind == MaintenanceKind.PART:
        row["brand"] = str(entry.get("brand") or "")[:100]
        row["model_name"] = str(entry.get("model_name") or "")[:100]
        row["custom_warn_km"] = _positive_number(entry.get("custom_warn_km"))
        row["custom_warn_days"] = _positive_number(entry.get("custom_warn_days"))
    else:
        row["interval_km"] = _positive_number(entry.get("interval_km"))
        row["interval_days"] = _positive_number(entry.get("interval_days"))
    return row


def _positive_number(value):
    """Nur echte positive Zahlen uebernehmen — 0 oder Text wuerde die Wear-Rechnung stoeren."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def suggest_setup(bike, manufacturer: str, model: str, year: int | None) -> dict | None:
    """
    Schritt 2: Vorbelegung fuer den Setup-Stepper.

    Die Antwortform entspricht bewusst dem, was `AssemblyChecklistComponent` im Frontend
    ohnehin bindet (`parts`/`intervals` je Baugruppe) — so braucht der Stepper kein
    zweites Datenmodell und bleibt in jedem Feld editierbar.

    Gibt None zurueck, wenn keine KI verfuegbar ist.
    """
    groups = _catalog_for_bike_type(bike.bike_type)
    if not groups:
        return {"groups": [], "model": model, "manufacturer": manufacturer}

    allowed = _allowed_template_ids(groups, bike.bike_type)

    system_prompt = (
        "Du bist ein Fahrrad-Experte und hilfst einem Laien, sein Fahrrad in einer "
        "Wartungs-App anzulegen. Du bekommst Hersteller, Modell, Baujahr und einen "
        "KATALOG erlaubter Baugruppen und Komponenten-Templates mit ihren IDs.\n\n"
        "Waehle daraus die Teile aus, die an diesem Fahrrad typischerweise verbaut sind, "
        "und schlage je Teil Marke und Modellbezeichnung der ab Werk verbauten Komponente "
        "vor. Du darfst AUSSCHLIESSLICH template_id-Werte aus dem Katalog verwenden — "
        "erfinde keine neuen IDs, Baugruppen oder Komponenten.\n\n"
        "Antworte ausschliesslich mit einem JSON-Objekt der Form:\n"
        '{"groups": [{"group_id": 1, "parts": [{"template_id": 2, "include": true, '
        '"brand": "Shimano", "model_name": "CN-M8100", "custom_warn_km": 4000, '
        '"confidence": "high", "note": ""}], "intervals": [{"template_id": 9, '
        '"include": true, "interval_km": 300, "interval_days": 30}]}]}\n\n'
        "'confidence' ist 'high', wenn du die Serienausstattung dieses Modells sicher "
        "kennst, 'medium' bei einer begruendeten Annahme, 'low' bei blossem Raten. "
        "Bist du dir bei einem Teil sehr unsicher, setze include auf false statt zu raten. "
        "Kennst du das Modell gar nicht, gib nur die typische Ausstattung fuer diesen "
        "Fahrradtyp mit confidence 'low' zurueck."
    )
    user_prompt = (
        f"Hersteller: {manufacturer}\n"
        f"Modell: {model}\n"
        f"Baujahr: {year if year else 'unbekannt'}\n"
        f"Fahrradtyp: {bike.get_bike_type_display()} ({bike.bike_type})\n\n"
        f"KATALOG:\n{_catalog_prompt_block(groups, bike.bike_type)}\n"
    )

    data = get_ai_provider().generate_json(system_prompt, user_prompt)
    if data is None:
        return None

    return {
        "manufacturer": manufacturer,
        "model": model,
        "year": year,
        "groups": _filter_to_catalog(data, groups, allowed),
    }


def _filter_to_catalog(
    data: dict, groups: list[ComponentGroup], allowed: dict
) -> list[dict]:
    """
    Bringt die KI-Antwort auf die Katalog-Struktur: nur bekannte Gruppen, nur erlaubte
    Templates, nur die richtige Art (Teil vs. Verbrauchsmaterial). Alles andere fliegt
    raus — die DB sieht ausschliesslich validierte IDs.
    """
    by_id = {group.id: group for group in groups}
    suggested = data.get("groups")
    if not isinstance(suggested, list):
        logger.warning("Kudo-Setup ohne 'groups'-Liste: %r", data)
        return []

    results = []
    for entry in suggested:
        if not isinstance(entry, dict):
            continue
        try:
            group_id = int(entry.get("group_id"))
        except (TypeError, ValueError):
            continue
        group = by_id.get(group_id)
        if group is None:
            logger.info("Kudo schlug unbekannte Baugruppe %s vor, verworfen.", group_id)
            continue

        parts = [
            row
            for row in (
                _clean_row(item, allowed, group_id, MaintenanceKind.PART)
                for item in entry.get("parts", [])
                if isinstance(item, dict)
            )
            if row is not None
        ]
        intervals = [
            row
            for row in (
                _clean_row(item, allowed, group_id, MaintenanceKind.CONSUMABLE)
                for item in entry.get("intervals", [])
                if isinstance(item, dict)
            )
            if row is not None
        ]
        if not parts and not intervals:
            continue

        results.append(
            {
                "group_id": group_id,
                "group_name": group.name,
                "parts": parts,
                "intervals": intervals,
            }
        )

    # Reihenfolge des Katalogs, nicht die der KI-Antwort — der Stepper laeuft die
    # Baugruppen in seiner eigenen sortierten Reihenfolge durch.
    order = {group.id: index for index, group in enumerate(groups)}
    return sorted(results, key=lambda item: order.get(item["group_id"], 999))
