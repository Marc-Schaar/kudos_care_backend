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

from app_maintenance.models import ComponentGroup, MaintenanceKind

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
        "Modellreihen dieses Herstellers, die es dafuer WIRKLICH GAB.\n\n"
        "Recherchiere die Modellpalette, statt sie zu erinnern, wenn dir eine Suche "
        "zur Verfuegung steht. Der Nutzer waehlt aus deiner Liste sein eigenes Rad "
        "aus — ein erfundenes Modell schickt ihn in eine Ausstattung, die es nie gab, "
        "und das faellt ihm erst auf, wenn die Wartungsdaten nicht passen.\n\n"
        "REGELN:\n"
        "1. Nenne nur real existierende Modellreihen dieses Herstellers. Erfinde "
        "keine Namen und keine Varianten, und haenge keine Ausstattungskuerzel an, "
        "die du nicht belegen kannst.\n"
        "2. Lieber weniger als unsicher: drei belegte Modelle sind besser als acht "
        f"halb geratene. Hoechstens {MAX_MODEL_SUGGESTIONS}, das gaengigste zuerst.\n"
        "3. Passe zum angefragten Baujahr und Fahrradtyp. Ein Modell, das es in dem "
        "Jahr nicht gab oder das ein anderer Fahrradtyp ist, gehoert nicht in die "
        "Liste.\n"
        "4. 'spec' ist die Serienausstattung in Stichworten: Schaltgruppe, Bremsart, "
        "gegebenenfalls Federung (max. 12 Woerter, deutsch). Genau daraus leitet der "
        "naechste Schritt die Verschleissteile ab, also nur hineinschreiben, was du "
        "belegen kannst — sonst leer lassen.\n"
        "5. 'confidence': 'high' = Modellreihe und Ausstattung belegt, 'medium' = "
        "Modellreihe sicher, Ausstattung ungenau, 'low' = unsicher.\n"
        "6. 'note' ist eine sehr kurze deutsche Einordnung (max. 8 Woerter), z.B. die "
        "Positionierung im Sortiment.\n"
        "7. Kennst du den Hersteller nicht, gib eine leere Liste zurueck statt zu "
        "raten.\n\n"
        "Antworte ausschliesslich mit einem JSON-Objekt der Form:\n"
        '{"models": [{"model": "Grail CF SL 7", "year_range": "2021-2023", '
        '"spec": "Shimano GRX 1x11, hydraulische Scheibenbremsen", '
        '"confidence": "high", "note": "Gravel-Mittelklasse"}]}'
    )
    user_prompt = (
        f"Hersteller: {manufacturer}\n"
        f"Baujahr: {year if year else 'unbekannt'}\n"
        f"Fahrradtyp: {bike_type}\n"
    )

    data = get_ai_provider().generate_json_researched(system_prompt, user_prompt)
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
                # Serienausstattung in Stichworten. Wird dem Nutzer bei der Auswahl
                # angezeigt UND an Schritt 2 durchgereicht, damit die Komponenten zu
                # genau dem Rad passen, das er hier ausgewaehlt hat.
                "spec": str(entry.get("spec") or "")[:200],
                "confidence": _coerce_confidence(entry.get("confidence")),
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


def _format_lifetime(template) -> str:
    """
    Standard-Lebensdauer knapp und ohne Rauschen. Frueher stand hier
    "2000.0 km / ? Tage": die Nachkommastelle ist bedeutungslos, und das "?"
    liest sich wie ein auszufuellender Platzhalter statt wie "diese Achse gilt
    fuer dieses Teil nicht".
    """
    parts = []
    if template.warn_km:
        parts.append(f"{template.warn_km:g} km")
    if template.warn_days:
        parts.append(f"{template.warn_days:g} Tage")
    return " oder ".join(parts) if parts else "keine Vorgabe"


def _catalog_prompt_block(groups: list[ComponentGroup], bike_type: str) -> str:
    """
    Der erlaubte Katalog als Text — die KI darf nur diese IDs referenzieren.

    `default_in_group` steht bewusst mit drin: das ist die kuratierte Aussage,
    ob ein Teil an so einem Rad ueblich oder ein Sonderfall ist. Das Frontend
    nimmt `hint.include` der KI und ueberschreibt damit genau diesen Default
    (siehe `AssemblyChecklistComponent`) — fehlt die Angabe im Prompt,
    ueberstimmt also ein Rateergebnis eine gepflegte Vorgabe.
    """
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
            usual = "ueblich" if template.default_in_group else "Sonderfall"
            lines.append(
                f"  - Template {template.id}: {template.name} "
                f"[{kind}, {usual}, Standard: {_format_lifetime(template)}]"
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
                    # Fuer die Ausstattungs-Pruefung (siehe SPEC_REQUIREMENTS) und
                    # als lesbarer Kontext in Logs.
                    "name": template.name,
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
        # Aus dem Katalog, nicht aus der KI-Antwort — dient der
        # Ausstattungs-Pruefung in `_apply_spec_consistency()`.
        "template_name": meta["name"],
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


#: Teile, deren Existenz von der Ausstattung abhaengt. Der Schluessel ist ein
#: Namensfragment des Templates (kleingeschrieben), der Wert eine Funktion, die auf
#: der gemeldeten `spec` entscheidet, ob das Teil an so einem Rad ueberhaupt sein
#: kann. Namensbasiert wie `views.py::_interval_kind_for_template()` — der Katalog
#: hat fuer diese Semantik kein eigenes Feld, und eines dafuer einzufuehren waere
#: mehr Schema, als die Handvoll Faelle rechtfertigt.
SPEC_REQUIREMENTS = {
    "umwerfer": lambda s: not str(s.get("drivetrain", "")).startswith("1x"),
    "di2": lambda s: bool(s.get("electronic_shifting")),
    "axs": lambda s: bool(s.get("electronic_shifting")),
    "dichtmilch": lambda s: bool(s.get("tubeless")),
    "zahnriemen": lambda s: bool(s.get("belt_drive")),
}


def _contradicts_spec(template_name: str, spec: dict) -> bool:
    """
    Ob dieses Teil der gemeldeten Ausstattung widerspricht. Ohne `spec` (oder ohne
    passende Regel) immer False — im Zweifel bleibt die Auswahl der KI stehen.
    """
    if not spec:
        return False
    name = template_name.lower()
    for fragment, is_possible in SPEC_REQUIREMENTS.items():
        if fragment in name and not is_possible(spec):
            return True
    return False


def _apply_spec_consistency(groups: list[dict], spec: dict) -> list[dict]:
    """
    Prueft die Teileauswahl gegen die Ausstattung, die das Modell selbst gemeldet
    hat — die eigentliche Antwort auf "passen die Komponenten zum Modell".

    Ein Widerspruch wird **abgewaehlt, nicht geloescht** (`include=False` plus
    Begruendung in `note`): die Zeile bleibt im Stepper sichtbar, und wer es
    besser weiss, hakt sie wieder an. Stilles Loeschen wuerde dem Nutzer die
    Korrekturmoeglichkeit nehmen, ohne dass er merkt, dass ueberhaupt etwas
    entfernt wurde.
    """
    if not spec:
        return groups

    for group in groups:
        for row in group["parts"] + group["intervals"]:
            if not row["include"]:
                continue
            if _contradicts_spec(row.get("template_name", ""), spec):
                row["include"] = False
                row["note"] = (
                    row["note"] or "Passt nicht zur ermittelten Ausstattung."
                )[:120]
                logger.info(
                    "Kudo: %s abgewaehlt, widerspricht der Ausstattung %r.",
                    row.get("template_name"),
                    spec,
                )
    return groups


def suggest_setup(
    bike, manufacturer: str, model: str, year: int | None, spec: str = ""
) -> dict | None:
    """
    Schritt 2: Vorbelegung fuer den Setup-Stepper.

    Die Antwortform entspricht bewusst dem, was `AssemblyChecklistComponent` im Frontend
    ohnehin bindet (`parts`/`intervals` je Baugruppe) — so braucht der Stepper kein
    zweites Datenmodell und bleibt in jedem Feld editierbar.

    `spec` ist die Serienausstattung aus Schritt 1 (`suggest_models`), falls der
    Nutzer ein vorgeschlagenes Modell gewaehlt hat. Sie ankert die Auswahl an genau
    dem Rad, das er angeklickt hat, statt das Modell den Namen erneut interpretieren
    zu lassen.

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
        "Waehle daraus die Teile aus, die an diesem Fahrrad tatsaechlich verbaut sind. "
        "Du darfst AUSSCHLIESSLICH group_id- und template_id-Werte aus dem KATALOG "
        "verwenden, und ein Template nur unter der Baugruppe, unter der es dort steht. "
        "Ein Template, das im Katalog als 'Verschleissteil' gefuehrt wird, gehoert nach "
        "'parts', eines mit 'Verbrauchsmaterial' nach 'intervals'. Erfundene, "
        "vertauschte oder falsch einsortierte IDs werden serverseitig verworfen.\n\n"
        "REGELN:\n"
        "1. include beantwortet: ist dieses Teil an DIESEM Rad vorhanden und sinnvoll "
        "zu tracken? Das ist unabhaengig davon, ob du die Marke kennst. Ein Gravelbike "
        "hat immer eine Kette (include true), auch wenn dir der Hersteller unbekannt "
        "ist.\n"
        "2. confidence beantwortet etwas anderes: wie sicher bist du bei Marke und "
        "Modellbezeichnung? 'high' = du kennst die Serienausstattung dieses Modelljahrs "
        "wirklich, 'medium' = begruendete Annahme aus Preisklasse und Baujahr, 'low' = "
        "geraten. confidence sagt nichts ueber include aus.\n"
        "3. Kennst du Marke oder Modellbezeichnung nicht, lass brand und model_name "
        "LEER und setze confidence auf 'low'. Ein leeres Feld ist besser als ein "
        "falscher Markenname, den der Nutzer ungeprueft uebernimmt.\n"
        "4. Der Katalog markiert jedes Template als 'ueblich' oder 'Sonderfall'. Folge "
        "dieser Vorgabe, solange du keinen konkreten Grund hast, davon abzuweichen: ein "
        "'Sonderfall' bekommt include true nur, wenn dieses Modell ihn wirklich hat "
        "(Umwerfer nur bei 2-fach-Kurbel, Zahnriemen nur bei Riemenantrieb, Di2/AXS-Akku "
        "nur bei elektronischer Schaltung).\n"
        "5. Einander ausschliessende Varianten: waehle hoechstens eine. Ein Rad wird auf "
        "genau eine Art geschmiert (Kettenoel ODER Heisswachs ODER Tropfwachs) und hat "
        "entweder Kette oder Zahnriemen. Tubeless-Dichtmilch nur, wenn das Modell ab "
        "Werk tubeless faehrt.\n"
        "6. custom_warn_km/custom_warn_days bzw. interval_km/interval_days nur setzen, "
        "wenn dieses konkrete Teil deutlich vom Katalog-Standard abweicht. Sonst Feld "
        "weglassen — der Standardwert ist gepflegt und besser als eine Schaetzung.\n"
        "7. 'note' nur fuellen, wenn es eine echte Einschraenkung zu sagen gibt "
        "(max. 100 Zeichen, deutsch). Sonst leerer String.\n"
        "8. Gib fuer JEDE Baugruppe des Katalogs einen Eintrag zurueck, auch wenn darin "
        "alle Teile include false haetten — der Nutzer laeuft im Stepper jede Baugruppe "
        "durch.\n"
        "9. Kennst du das Modell gar nicht, liefere die typische Ausstattung fuer diesen "
        "Fahrradtyp: include nach der 'ueblich'-Markierung, brand und model_name leer, "
        "confidence 'low'.\n\n"
        "ZUERST die Ausstattung, DANN die Teile: Fuelle das Feld 'spec' aus, bevor du "
        "Komponenten waehlst, und leite die Auswahl daraus ab. Recherchiere die "
        "Ausstattung, wenn dir eine Suche zur Verfuegung steht, statt sie zu erinnern. "
        "'spec' hat die Felder drivetrain ('1x11', '2x12', 'unbekannt'), "
        "electronic_shifting (true/false), belt_drive (true/false), tubeless "
        "(true/false), suspension ('keine', 'vorne', 'voll'). Diese Angaben werden "
        "serverseitig gegen deine Teileauswahl geprueft: ein Umwerfer bei 1x, ein "
        "Di2-Akku ohne elektronische Schaltung oder Dichtmilch ohne Tubeless werden "
        "automatisch abgewaehlt. Setze 'unbekannt' bzw. false, wenn du es nicht "
        "belegen kannst.\n\n"
        "Antworte ausschliesslich mit einem JSON-Objekt dieser Form. Die IDs im Beispiel "
        "sind PLATZHALTER, die nur das Format zeigen — verwende ausschliesslich die "
        "echten IDs aus dem KATALOG:\n"
        '{"spec": {"drivetrain": "1x11", "electronic_shifting": false, '
        '"belt_drive": false, "tubeless": true, "suspension": "keine"}, '
        '"groups": [{"group_id": 9001, "parts": [{"template_id": 9002, '
        '"include": true, "brand": "Shimano", "model_name": "CN-HG601", '
        '"confidence": "high", "note": ""}], "intervals": [{"template_id": 9003, '
        '"include": true, "interval_km": 300, "confidence": "medium", '
        '"note": ""}]}]}'
    )
    user_prompt = (
        f"Hersteller: {manufacturer}\n"
        f"Modell: {model}\n"
        f"Baujahr: {year if year else 'unbekannt'}\n"
        f"Fahrradtyp: {bike.get_bike_type_display()} ({bike.bike_type})\n"
        + (f"Bekannte Serienausstattung: {spec}\n" if spec else "")
        + f"\nKATALOG:\n{_catalog_prompt_block(groups, bike.bike_type)}\n"
    )

    provider = get_ai_provider()
    data = provider.generate_json_researched(system_prompt, user_prompt)
    if data is None:
        return None

    reported_spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
    suggested = _filter_to_catalog(data, groups, allowed)

    return {
        "manufacturer": manufacturer,
        "model": model,
        "year": year,
        "spec": reported_spec,
        # Ob die Ausstattung wirklich nachgeschlagen wurde oder aus dem
        # Modellwissen kam — damit die UI keine Recherche behaupten kann, die
        # nicht stattgefunden hat (siehe settings.AI_GROUNDING_ENABLED).
        "researched": provider.last_call_was_researched,
        "groups": _apply_spec_consistency(suggested, reported_spec),
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
