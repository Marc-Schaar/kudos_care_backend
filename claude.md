# Project: Kudos Care — Backend (Django & DRF)

## Was ist Kudos Care?

Eine Wartungs-Tracking-App für Fahrräder/Motorräder mit Strava-Integration. Nutzer loggen
sich via Strava-OAuth ein, ihre Aktivitäten ("Rides") und Bikes werden synchronisiert,
historische Wetter-/Winddaten werden pro Ride ergänzt (Open-Meteo), und der Verschleiß von
Bike-Komponenten (Kette, Reifen, Bremsbeläge, ...) wird anhand von km/Stunden/Tagen seit
Montage getrackt, zusätzlich wetter-gewichtet (Regen/Hitze/Kälte/Wind pro Ride, siehe
`app_maintenance/api/services.py::WeatherWearService`), mit Status `ok` / `warn` / `critical`.
Eine optionale KI-Erklärung (Gemini/Groq) narriert auf Anfrage die berechneten Zahlen in
Worten, berechnet aber nie selbst. Bei Wartungsbedarf (aktuell oder anhand einer
Fahrt-Vorhersage) sowie bei Erstanmeldung verschickt die App automatisch E-Mails (Brevo SMTP,
siehe `app_notifications`). UI-Sprache und viele Code-Kommentare sind Deutsch.
Zugehöriges Frontend: `kudos_care_frontend` (Angular), siehe dessen `CLAUDE.md`.

## Tech Stack

- Python 3.11+, Django 6.0, Django REST Framework 3.17
- **PostgreSQL + PostGIS** (django.contrib.gis) — kein SQLite-Fallback trotz generischer
  Annahme, Ride-Tracks sind `LineStringField`/`PointField`
- Celery 5.6 + Redis (Broker & Result-Backend) für asynchrone Jobs (Strava-Sync, Webhook-Import)
- Session-Cookie-Auth (kein JWT, kein `djangorestframework-simplejwt`)
- Windows-Dev: `core/settings.py` hardcodet einen QGIS-Pfad für GDAL/GEOS — nur lokal relevant

## Commands

- Install: `pip install -r requirements.txt`
- Migrations: `python manage.py makemigrations && python manage.py migrate`
- Dev Server: `python manage.py runserver`
- Celery Worker (nötig für Strava-Sync/Webhook): `celery -A core worker -l info` (Redis muss laufen)
- Celery Beat (nötig für die täglichen `app_notifications`-Checks): `celery -A core beat -l info`
  — zusätzlich zum Worker; in Produktion als eigenes Supervisor-Programm `celery-beat`
  eingerichtet, wird von `deploy.yml` mitneugestartet
- Tests: `python manage.py test` (kein pytest konfiguriert, trotz anderslautender Vermutung)
- Linter/Formatter: `black . && isort . && flake8`

## Architektur — Apps

- **`app_auth`** — Strava-OAuth-Login. Model `StravaProfile` (1:1 zu Django `User`,
  speichert Access/Refresh-Token + Sync-Status). Endpoints: `POST /api/strava/auth/`,
  `GET|PATCH /api/strava/me/`, `POST /api/strava/logout/`. `GET me/` liefert neben der
  `athlete_id` auch `email`, `email_notifications_enabled` und `needs_email`; `PATCH me/`
  (`UserSettingsSerializer`) schreibt beides — `email` an den Django-`User`, das Flag ans
  `StravaProfile`. Wird zum ersten Mal eine Adresse gesetzt und `welcome_email_sent_at`
  ist noch leer, wird die bei der Erstanmeldung ins Leere gelaufene Willkommens-Mail
  nachgeholt. `api/utils.py` hat
  `get_valid_access_token()`/`strava_get()` als geteilten Strava-HTTP-Helper mit
  Token-Refresh + 401-Retry — von anderen Apps wiederverwendet.
- **`app_dashboard`** — Ride-Ingestion, Geodaten, Wetter/Wind. Models `Ride` (PostGIS-Track,
  `weather_data` JSONField, FK zu `StravaProfile`+`Bike`), `RideStream` (Rohdaten-Zeitreihe).
  **`api/wind.py` ist die einzige Quelle für alles Windbezogene** (ersetzt das frühere
  `api/utils.py`, das dabei entfallen ist). `build_wind_segments()` teilt den vollen
  GPS-Stream in Abschnitte *gleicher Distanz* und rechnet je Abschnitt den dortigen Kurs
  gegen den auf diesen Zeitpunkt interpolierten Wind (Richtung zirkulär interpoliert, sonst
  läge zwischen 350° und 10° die Südrichtung). Davon abgeleitet: `average_headwind()`
  (distanzgewichtet → die Kopfzeilen-Zahl) und `hourly_headwind()` (→ das Chart). Dadurch
  können Text, Chart und Karte nicht mehr auseinanderlaufen — vorher gab es drei
  unabhängige Berechnungen mit drei verschiedenen Ergebnissen. Ohne GPS-Stream greift
  `build_coarse_wind_segment()` (Start-Ziel-Gerade), erkennbar an `wind_source="coarse"`.
  Fehlen die Wetterdaten ganz, entstehen **reine Geometrie-Abschnitte**
  (`wind_source="none"`, `headwind=None`) und die Route wird neutral gezeichnet — die
  Einfärbung ist ein Overlay, kein Existenzgrund für die Strecke. Genau daran hing der
  Prod-Bug „Streckenverlauf: keine Daten": Bestandsfahrten lieferten vorher gar keine
  Abschnitte und die Karte blieb leer.
  Die Abschnitte werden **nicht persistiert**, sondern im Detail-Request aus `RideStream`
  + `weather_data` rekonstruiert (`hourly_from_weather_data()`); dafür liegt seit dem
  Refactoring `wind_direction_10m` mit in `weather_data`. **Altfahrten ohne dieses Feld
  bleiben auf `wind_source="none"` (Strecke sichtbar, keine Einfärbung), bis
  `recompute_wind` gelaufen ist — der Backfill gehört zu jedem Deploy dieser Änderung.**
  `api/services.py`: `StravaSyncService` (Bikes + paginierte Activities),
  `StravaImportService.sync_activity_to_db` (Polyline-Decode → Shapely-RDP-Simplify →
  Gear-Matching → Streams → Open-Meteo-Wetter → Windabschnitte), `WeatherService`,
  `build_ride_summary_prompt` (baut den Prompt für die optionale KI-Fahrt-Zusammenfassung,
  nutzt denselben `app_maintenance.api.ai_providers.get_ai_provider()` wie die
  Wetter-Verschleiß-Erklärung — App-Grenze bewusst überschritten statt dupliziert).
  Celery-Task `run_strava_sync` in `api/tasks.py`. Management-Command `recompute_wind.py`
  (Backfill, `--dry-run`) — rechnet `weather_data` inkl. Windrichtung und
  `RideStream.avg_headwind` neu. `GET /api/activities/<id>/` liefert neben
  `geo_json_full` (nur noch für die Bounds) das Feld `wind_segments`: eine
  GeoJSON-FeatureCollection mit einem Feature je Abschnitt
  (`headwind`/`wind_direction`/`bearing`/`precipitation`), die das Frontend direkt
  rendert — die Karte interpoliert selbst nichts mehr.
  `GET /api/activities/<id>/wear-impact/?refresh=true` liefert, was **diese eine Fahrt**
  die Komponenten gekostet hat (siehe `app_maintenance`); der Endpoint gibt die Zahlen
  auch dann zurück, wenn die KI ausfällt (`ai_unavailable: true` statt 503) — der
  Erzähltext ist die Zugabe, die Zahlen sind die Aussage.
  Endpoint `GET /api/activities/<id>/summary/?refresh=true`
  liefert eine gecachte KI-Zusammenfassung der Fahrt (Distanz/Dauer/Wetter/Gegenwind);
  Cache-Felder `Ride.ai_summary`/`ai_summary_generated_at`, keine Staleness-Prüfung nötig
  da Ride-Zahlen nach dem Import unveränderlich sind (anders als beim Bike-Zustandsbericht).
- **`app_maintenance`** — Kern-Domäne Verschleiß-Tracking. Ein Bike besteht aus
  **Baugruppen** (`BikeAssembly`), jede Baugruppe bündelt ihre `ComponentSlot`s
  (physische Verschleißteile) und `MaintenanceInterval`s (Verbrauchsmaterial/Pflege
  ohne "Zustand"). Models: `Bike`,
  `ComponentGroup` (Katalog-Blueprint einer Baugruppe, z.B. "Laufrad vorne"/"Antrieb" —
  verbindet mehrere `ComponentTemplate`s; `category`/`applicable_bike_types`/`sort_order`/
  `recommended` steuern UI-Filter + den Neu-Bike-Stepper; bewusst generisch, weitere
  Gruppen rein über Admin/Migration anlegbar; voller Satz in Migration `0016` geseedet),
  `BikeAssembly` (per-Bike-Instanz einer `ComponentGroup`: eigener `name`, `installed_at`,
  `is_active` — **mehrere Instanzen je `(bike, group)` sind erlaubt** (Sommer-/Winter-LRS),
  aber max. eine *aktive*, via `clean()` erzwungen. Drei Zustände aus zwei Feldern:
  `is_active=True` = am Rad; `is_active=False, retired_at=None` = **geparkt** (abgezogen,
  Komponenten bleiben `is_mounted=True`, weil sie ja weiter auf dem Laufradsatz sitzen);
  `retired_at` gesetzt = ausgemustert (Komponenten ausgebaut). `compute_km()`/
  `worst_status()`/`is_parked`/`open_period()`/`ensure_open_period()` als Fat-Model-Methoden),
  `AssemblyUsagePeriod` (Zeitraum, in dem eine Baugruppe tatsächlich am Rad war:
  `started_at`/`started_distance_km` + `ended_at`/`ended_distance_km`, offene Periode =
  aktuell montiert, max. eine je Baugruppe. **Ohne dieses Model würde ein abgezogener
  Laufradsatz im Keller weiter km sammeln**, da `wear_km` am Bike-Odometer hängt.
  Angelegt/geschlossen von den `_mount_assembly`/`_park_assembly`/`_retire_assembly`-Helfern
  in `api/views.py`; `ensure_open_period()` zieht sie für Altbestände nach, die außerhalb der
  API entstanden sind — Schema in Migration `0018`, Backfill in `0019`),
  `ComponentTemplate` (Katalog, Fixture `fixtures/component_templates.json`,
  optionales FK `group`; `maintenance_kind` `part`|`consumable` — `consumable` wird als
  `MaintenanceInterval` statt als `Component` instanziiert; `default_in_group` = Vorauswahl
  im Baugruppen-Dialog), `ComponentSlot`
  (Position innerhalb einer `BikeAssembly`, FK `assembly` null=alt/ungruppiert;
  bedingte Unique-Constraints `(assembly, template)` bzw. `(bike, template)` statt des
  früheren `unique_together`), `Component` (physisches Teil,
  `is_mounted` via `clean()`/`save()`-Override erzwungen: nur 1 montiertes Teil je Slot;
  `distance_at_retire` = km-Stand beim Ausbau, damit ein mitten in einer Nutzungsperiode
  getauschtes Teil sauber abgeschnitten wird;
  zusätzlich `weather_wear_km`/`weather_wear_ride_count`/`weather_wear_computed_at` — async
  von `WeatherWearService` befüllt, nie live berechnet),
  `MaintenanceInterval` (Verbrauchsmaterial/Pflege: `interval_km`/`interval_days` +
  `last_done_at`/`last_done_distance_km`; `status(bike_total, as_of=None)` spiegelt
  `compute_wear`; "Erledigt" = neuer `MaintenanceLog` + Baseline-Reset, kein Zustand-%),
  `MaintenanceLog` (Historie der Erledigungen), `WeatherSensitivityCoefficient`
  (Regen/Hitze/Kälte/Wind-Gewichtung je `ComponentCategory`, geseedet in Migration `0006`
  bzw. für neu hinzugekommene Kategorien in Folgemigrationen wie `0011`,
  Basis für eine spätere Kalibrierung aus `ComponentCheck.condition_pct`-Verlauf),
  `ComponentCheck` (Log eines Checks/Release, optional `condition_pct` + Snooze).
  `AthleteMixin` (`api/views.py`) scoped alle Querysets auf
  `request.session["strava_athlete_id"]`.
  **Query-Konventionen** (abgesichert durch `test_query_counts.py`, siehe Testing): die
  Bike-Endpoints dürfen nicht mit der Slot-Anzahl skalieren. Drei Regeln dahinter:
  (1) `Bike.total_distance_km` ist ein Property mit eigener `aggregate(Sum)`-Query —
  Querysets holen den Wert stattdessen per `Bike.objects.with_total_distance()` als
  Annotation (`_total_distance_m`), Serializer über den Helper
  `api/serializers.py::bike_total_km(context, bike)`, der ihn je Request cacht; nie
  direkt in einer Schleife über Slots/Intervalle lesen. (2) Ein
  `prefetch_related("rides")` auf `Bike` ist **falsch** — es lädt je Fahrt
  LineString-Track und `weather_data`-JSON, die kein Serializer anfasst; die Distanz
  kommt aus der Annotation. (3) `ComponentSlot.mounted_component` iteriert bewusst
  `components.all()` statt `.filter(is_mounted=True)`, weil ein `.filter()` auf einem
  prefetchten Related-Manager den Prefetch-Cache umgeht. Prefetch-Ketten brauchen
  entsprechend `slots__template__group` (für `ComponentTemplateSerializer.group_name`)
  und `slots__components__checks`; Baugruppen-Ketten zusätzlich `slots__bike`, da Django
  bei diesem Pfad nur `slot.assembly` zurückcacht.
  `WarnStatus` (`api/serializers.py`) berechnet
  `ok`/`warn`/`critical`/`unknown` aus einem Ratio (≥1.0 critical, ≥0.8 warn);
  `compute_wear()` kombiniert km-/Tage-/Wetter-Achse zum schlechtesten Einzelwert; optionaler
  `as_of`-Parameter (Default: heute) projiziert nur die Tage-Achse auf ein zukünftiges Datum —
  genutzt von `app_notifications` für die Fahrt-Vorhersage-Warnung, km-/Wetter-Achse bleiben
  auf dem aktuellen Stand, da zukünftige Distanz unbekannt ist.
  **`api/usage.py` ist die einzige Quelle dafür, wann ein Teil tatsächlich am Rad war**
  (analog zu `app_dashboard/api/wind.py` für alles Windbezogene): `component_km_windows()`
  schneidet die `AssemblyUsagePeriod`s gegen die Einbau-/Ausbau-Spanne der Komponente,
  `component_active_km(..., since_km=...)` summiert daraus die km (auch für die
  `ComponentCheck`-Baseline nach einer Freigabe), `component_date_windows()` liefert das
  Datums-Pendant für den Ride-Filter. Die **Tage-Achse bleibt bewusst außen vor** — Gummi
  und Dichtmilch altern auch im Keller, `wear_days` läuft über Parkzeiten hinweg weiter.
  Ohne Baugruppe (ungruppierte Alt-Slots) oder ohne Perioden greift ein Fallback auf genau
  ein Fenster ab Einbau, also das Verhalten von vor der Umstellung.
  `ComponentSlot.objects.on_bike(bike)` (bzw. `serializers.slots_on_bike()` für bereits
  geprefetchte Querysets) hält geparkte Baugruppen aus Bike-Ampel, Diagramm,
  Zustandsbericht und Warn-E-Mails heraus — sonst zählte ein Satz im Keller doppelt mit.
  `api/services.py`: `WeatherWearCalculator` (reine Formel: Wetter → Verschleiß-Multiplikator
  pro Ride; `ride_multiplier_detail()` gibt zusätzlich die Einzelbeiträge zurück,
  `ride_multiplier()` ist der dünne Wrapper darauf) + `WeatherWearService` (voller Recompute
  über die Ride-Historie seit Einbau, nur aktuell montierte Komponenten — der Ride-Filter
  läuft über `usage.component_date_windows()`, Fahrten aus einer Parkphase fallen also
  heraus; `recompute_bike(include_parked=False)` überspringt geparkte Sätze, deren Endwert
  einmalig beim Abziehen final berechnet wird); außerdem `get_new_component_warnings()`/
  `get_predicted_unsafe_bikes()` als fachliche Grundlage für `app_notifications` (siehe dort).
  Celery-Task `recompute_weather_wear_for_bike` in
  `api/tasks.py`, getriggert nach jedem Ride-Import (Hook in
  `app_dashboard/api/services.py::sync_activity_to_db`); löst nach erfolgreicher
  Neuberechnung zusätzlich `app_notifications.tasks.check_component_warnings_for_bike` aus.
  `ride_wear_breakdown(ride)` beantwortet **„was hat diese eine Fahrt gekostet"**: je
  `ComponentCategory` den wetterbedingten Aufschlag (`extra_pct`, dominanter Treiber)
  und je Komponente `share_of_life_pct` (Anteil an der empfohlenen Lebensdauer). Die
  Rechnung war immer schon fahrtbezogen — persistiert wurde nur die Summe. Anders als
  `WeatherWearService` rekonstruiert das die **Historie**: berücksichtigt werden die zum
  Fahrtzeitpunkt montierten Teile (`installed_at`/`retired_at`), nicht die heute
  montierten. KI-Erzähltext via `build_ride_wear_impact_prompt()` + Cache-Felder
  `Ride.wear_impact_summary`/`wear_impact_generated_at`, Staleness über
  `ride_wear_impact_is_stale()` (Komponenten seither geändert?).
  `api/bike_assistant.py`: **„Kudo"**, der KI-Assistent fürs Bike-Anlegen.
  `suggest_models()` (Hersteller + Baujahr → Modellauswahl) und `suggest_setup()`
  (Modell → Vorbelegung für den Setup-Stepper). Endpoints
  `POST maintenance/assistant/models/` und `POST maintenance/bikes/<id>/assistant/setup/`,
  beide `503` bei AI-Ausfall, damit das Frontend auf die manuelle Einrichtung zurückfällt.
  **Sicherheitsgrenze:** die KI bekommt den erlaubten Katalog im Prompt und darf nur
  daraus wählen; `_filter_to_catalog()` verwirft anschließend serverseitig jede
  `template_id`, die nicht zur Gruppe und zur richtigen `maintenance_kind` gehört —
  derselbe Schutz wie `views.py::_validate_assembly_items()`. Der Prompt allein ist keine
  Absicherung. Marke/Modell je Zeile sind dagegen echtes Modellwissen und damit ein
  bewusster Bruch mit „die KI erfindet nichts" — deshalb trägt jede Zeile ein
  `confidence`-Feld, und der Stepper weist sie als Vorschlag aus.
  `api/ai_providers.py`: austauschbarer
  Gemini/Groq-Adapter (`AI_PROVIDER`-Setting) für die On-Demand-KI-Erklärung.
  `generate_json()` erzwingt eine JSON-Antwort (Gemini `responseMimeType`, Groq
  `response_format`) mit deutlich höherem Token-Budget — die 300 Tokens der
  Freitext-Erklärungen reichen für ein komplettes Bike-Setup nicht. Bei
  `AI_PROVIDER=gemini` (Default) läuft intern ein `FallbackAIProvider`: primär das günstige
  Gemini-Flash-Lite-Modell (`GEMINI_MODEL`, Default `gemini-2.0-flash-lite`), bei Fehlschlag
  (fehlender Key/Timeout/Rate-Limit/...) automatisch Fallback auf Groq. `AI_PROVIDER=groq`
  nutzt direkt nur Groq, ohne Fallback-Kette. Die drei Endpoints unter `/api/maintenance/`
  (weather-explanation, check-instructions, condition-report — NICHT die Fahrt-Zusammenfassung
  in `app_dashboard`) rufen statt `get_ai_provider().generate_text()` direkt
  `ai_providers.py::generate_reviewed_text()` auf: lässt die generierte Antwort zusätzlich vom
  jeweils anderen Gemini/Groq-Modell auf Sinnhaftigkeit/Konsistenz mit den Ausgangsdaten
  gegenprüfen (fail-open, falls das Gegenstück-Modell keinen Key hat oder die Prüf-Anfrage
  selbst fehlschlägt), bei durchgefallener Prüfung genau eine Neugenerierung, danach wird
  ungeprüft ausgeliefert statt ein zweites Mal zu prüfen. Zweite KI-Erklärung analog dazu:
  `bikes/<id>/condition-report/` fasst `compute_wear()` über alle aktuell montierten
  Komponenten eines Bikes zusammen (statt nur eine Komponente/nur die Wetter-Achse);
  Cache-Felder `Bike.condition_report`/`condition_report_generated_at`, Staleness über
  `services.py::bike_condition_report_is_stale()` (Max über letzten Ride-Import,
  `weather_wear_computed_at` der montierten Komponenten, letzten `ComponentCheck`).
  Dritte KI-Erklärung: `components/<id>/check-instructions/` liefert eine praktische
  Schritt-für-Schritt-Anleitung, wie der Nutzer die Komponente selbst prüfen kann
  (`services.py::build_check_instructions_prompt`) — bekommt `compute_wear()`s
  `warn_status_overall` nur als Dringlichkeits-Kontext, berechnet sonst nichts selbst.
  Cache-Felder `Component.check_instructions`/`check_instructions_status`/
  `check_instructions_generated_at`; Staleness simpler als bei den anderen beiden KI-Endpoints
  (kein Zahlen-Vergleich nötig) — ungültig sobald sich `warn_status_overall` seit der letzten
  Generierung geändert hat (typischerweise nach einer Freigabe via `ComponentCheckView`).
  Management-Command `recompute_weather_wear.py` (Backfill, `--dry-run`).
  **Baugruppen-Flow** (`api/views.py`): `GET groups/?bike_type=` liefert den
  `ComponentGroup`-Katalog mit genesteten Templates (getrennt `parts`/`consumables`).
  `GET bikes/<id>/assemblies/` liefert die aktiven `BikeAssembly`s (Slots inkl. Wear +
  Intervalle inkl. Status), die geparkten Alternativen (`parked_assemblies` — inaktiv, aber
  nicht ausgemustert), die noch nicht zugeordneten Alt-Slots (`ungrouped_slots`), die
  ausgebauten Ersatzteile (`spare_components`, siehe unten) und die zum Bike-Typ passenden
  Katalog-Gruppen (`available_groups`). Letzteres enthält bewusst **auch Gruppen mit bereits
  aktiver Instanz** (Flag `has_active_instance`, gesetzt via `ComponentGroupSerializer`-Context
  `used_group_ids`) — sonst ließe sich über den "Baugruppe hinzufügen"-Dialog nie ein zweiter
  Satz (Sommer-/Winter-LRS) anlegen, obwohl `POST` das längst unterstützt (siehe unten). Bis
  Anfang September 2026 wurden solche Gruppen serverseitig herausgefiltert — ein Prod-Bug, bei
  dem ein zweiter Laufradsatz im "+ Baugruppe hinzufügen"-Dialog schlicht nicht mehr auftauchte.
  `POST bikes/<id>/assemblies/`
  legt eine Baugruppe komplett an (ein Dialog = eine Baugruppe): `BikeAssembly` +
  `ComponentSlot`s (+ montierte `Component`s) für die inkludierten `parts` +
  `MaintenanceInterval`s für die inkludierten `intervals`, atomar, nach demselben
  Unmount-dann-Mount-Muster wie `SlotMountView`; Templates werden serverseitig gegen
  `group.templates` geprüft, Bike-Typ-Mismatch → 400. Ist für die Gruppe **bereits eine
  Instanz aufgezogen, entsteht die neue geparkt** (kein 409 mehr) — die bestehende soll nicht
  ungefragt verdrängt werden; das optionale Body-Feld `activate: true` erzwingt den direkten
  Wechsel. Ein Part-Item kann statt `brand`/`model_name` eines von zwei
  Übernahme-Feldern mitgeben: `existing_slot_id` übernimmt eine bereits vorhandene, noch
  ungruppierte `ComponentSlot` (typischerweise ein Alt-Teil aus `ungrouped_slots`, **durchgehend
  montiert**) per Slot-Umhängen; `reuse_component_id` reaktiviert stattdessen ein bereits
  **ausgebautes** Teil (`spare_components` — z.B. der zurückgelegte Laufradsatz aus dem Keller;
  Kandidaten sind schlicht alle `is_mounted=False`-Components des Bikes je Template, unabhängig
  davon, ob deren alte Baugruppe noch existiert/aktiv ist), indem die Component in einen frisch
  angelegten Slot dieser Baugruppe umzieht. Beide Felder legen keine neue `Component` an; sie
  schließen sich gegenseitig aus. Der Unterschied bei der Nutzungsperiode:
  bei `existing_slot_id` (nie ausgebaut, durchgehender Verlauf) zieht `_build_assembly_from_request`
  den Periodenbeginn auf das früheste Einbaudatum/km unter allen übernommenen Teilen zurück,
  damit der Verlauf nicht durch die neue `AssemblyUsagePeriod` abgeschnitten wird (siehe
  `api/usage.py`) — neu angelegte Teile bleiben beim gemeinsamen `installed_at`. Bei
  `reuse_component_id` (echte Standzeit dazwischen) passiert das bewusst **nicht** — die neue
  Nutzungsperiode zählt km erst ab jetzt, sonst zählten km mit, die währenddessen ein anderer
  Satz gefahren ist. Der davor real aufgelaufene Verschleiß geht dabei aber **nicht** verloren:
  `Component.carried_over_wear_km`/`carried_over_weather_wear_km` frieren ihn einmalig ein (weil
  `component_active_km()`/`component_date_windows()` nur die Perioden der *aktuellen* Baugruppe
  sehen), `compute_wear()` bzw. `WeatherWearService.recompute_component()` addieren ihn auf den
  künftig neu berechneten Wert drauf — die km-Achse macht also am eingefrorenen Stand weiter,
  nicht bei 0. Die Tage-Achse altert ohnehin unverändert seit dem ursprünglichen `installed_at`
  weiter — exakt wie bei einer geparkten statt ausgebauten Baugruppe.
  `SpareComponentSerializer.prior_wear_km` zeigt den (voraussichtlichen) Carry-over-Wert schon
  im Vorschlag an, bevor er tatsächlich übernommen wird. `_spare_components_for_bike` liefert
  bewusst **alle** ausgebauten Kandidaten je Template, ohne auf einen zu reduzieren — ein Slot
  kann mehrere frühere Teile gleichzeitig ausgebaut haben (z.B. mehrere historische Felgen auf
  einem seit Jahren bestehenden, inzwischen ausgemusterten Slot), und keine Datums-Heuristik
  ("zuletzt ausgebaut"/"zuletzt eingebaut"/"am längsten montiert") erriet zuverlässig "die eine
  richtige" — mal gewann ein am selben Tag angelegtes Test-Artefakt, mal ein uralter
  Platzhalter-Eintrag. Der Client (`AssemblyChecklistComponent`) zeigt bei mehr als einem
  Kandidaten je Template ein Auswahlfeld statt zu raten. `_validate_assembly_items`
  prüft entsprechend, dass der referenzierte Slot/die referenzierte Component zum Bike und zum
  Template der Zeile gehört (und bei `existing_slot_id` zusätzlich ein montiertes Teil hat).
  Drei getrennte Aktionen auf einer Instanz, die man nicht verwechseln darf:
  `POST assemblies/<id>/activate/` **wechselt** zwischen zwei vorhandenen Sätzen (der bisher
  aufgezogene wird geparkt, seine Teile bleiben montiert),
  `POST assemblies/<id>/retire/` **mustert aus** (Komponenten ausgebaut,
  `retired_at`/`distance_at_retire` gesetzt), und `POST assemblies/<id>/swap/` **erneuert die
  Teile** (alte Instanz ausgemustert, neue aktive mit frischen Teilen — der alte
  „Baugruppe tauschen"-Pfad). `POST intervals/<id>/log/` = "Erledigt".
  `POST bikes/<id>/slots/` (freies Einzel-Slot-Anlegen) entfällt für den Client — Slots
  entstehen nur über den Baugruppen-Create; `GET` bleibt. Katalog + Zuordnung aller
  System-Templates zu Gruppen in Migration `0016`, Backfill bestehender Slots →
  `BikeAssembly`/`MaintenanceInterval` (erzwungen) in `0017`. Kassette gehört seit
  Migration `0020` zur Gruppe "Laufrad hinten" (nicht mehr "Antrieb") — sie sitzt physisch
  auf dem Freilaufkörper und wird beim Laufradwechsel typischerweise mitgetauscht, taucht
  also jetzt bei `assemblies/<id>/activate|swap/` auf "Laufrad hinten" mit auf. `0020` hängt
  dafür auch bestehende Kassette-Slots in den aktiven "Laufrad hinten"-Satz desselben Bikes
  um; ohne aktiven Satz bleibt der Slot ungruppiert statt eine Baugruppe zu erzwingen.
  Endpoints unter `/api/maintenance/`: `assistant/models/`,
  `bikes/<id>/assistant/setup/`, `bikes/`, `bikes/<id>/condition-report/`,
  `groups/`, `bikes/<id>/assemblies/` (GET/POST), `assemblies/<id>/` (PATCH/DELETE),
  `assemblies/<id>/activate/`, `assemblies/<id>/retire/`,
  `assemblies/<id>/swap/`, `bikes/<id>/intervals/`, `intervals/<id>/` (PATCH/DELETE),
  `intervals/<id>/log/`, `bikes/<id>/slots/` (nur GET), `slots/<id>/mount|unmount`,
  `slots/<id>/components/`, `components/<id>/check/`,
  `components/<id>/weather-explanation/`, `components/<id>/check-instructions/`, `templates/`.
- **`app_notifications`** — E-Mail-Versand, kein eigenes Domain-Model (keine Endpoints, kein
  `urls.py`). Dedupe-/Status-Felder für Benachrichtigungen liegen stattdessen direkt an den
  betroffenen Domain-Models (analog zu den KI-Cache-Feldern in `app_maintenance`):
  `StravaProfile.email_notifications_enabled`/`welcome_email_sent_at`,
  `Component.last_warn_notified_status`, `Bike.predicted_unsafe_notified_for_date`.
  `services.py::send_templated_email()` rendert ein gemeinsames HTML-Template
  (`templates/emails/base_email.html`, alle E-Mails erben per `{% extends %}` davon) mit
  Plaintext-Fallback via `html_to_plaintext()`. Letzteres statt `strip_tags` allein, weil
  `strip_tags` nur Tags entfernt, nicht den Inhalt von `<style>` — sonst landet das
  komplette Stylesheet im Plaintext-Teil; zusätzlich werden HTML-Entities aufgelöst.
  Das Layout ist **tabellenbasiert** (Outlook rendert mit Word und bricht bei
  div/flex), hell mit dunklem Marken-Band und Lime-CTA — durchgehend dunkel wie die App
  invertieren Outlook und einige Gmail-Dark-Modes teilweise. Webfonts werden in Mail-
  Clients meist nicht geladen, es zählen die Fallback-Stacks. Deutsche Status-Labels
  kommen aus dem Template-Filter `templatetags/notification_extras.py::warn_label`
  (vorher stand dort der rohe Slug „critical"). Design prüfen ohne Versand:
  `python manage.py preview_emails --text --open`. gibt bei Opt-out/fehlender E-Mail/Versandfehler `False`
  zurück statt zu werfen. Drei E-Mail-Typen, alle über `tasks.py`: (1) Willkommens-Mail
  (`send_welcome_email_task`) — automatisch bei Erstanmeldung (Hook in
  `app_auth/api/views.py::StravaAuthCallbackView`), rückwirkend für Bestandsnutzer per
  Admin-Action "Willkommens-E-Mail senden" auf `StravaProfileAdmin`; (2) Komponenten-Warnung
  (warn/critical, gebündelt als eine Sammel-Mail statt einer Mail pro Komponente) — zwei
  Auslöser teilen sich denselben Dedupe (`Component.last_warn_notified_status`): sofort nach
  einer Fahrt (`check_component_warnings_for_bike`, siehe Hook in `app_maintenance`) und
  täglich als Sicherheitsnetz für rein kalenderbasierte Fälle ohne neue Fahrt
  (`check_component_warnings`, Celery Beat); (3) Fahrt-Vorhersage
  (`check_bike_unsafe_predictions`, täglich) — sagt aus dem Ride-Verlauf eines Bikes (Median
  der Tage-Lücken zwischen den letzten Fahrten, siehe
  `app_dashboard/api/services.py::predict_next_ride_date`) das nächste voraussichtliche
  Fahrtdatum voraus und warnt, wenn eine Komponente bis dahin voraussichtlich kritisch wird
  (`compute_wear(..., as_of=predicted_date)`), obwohl sie **heute** noch nicht kritisch ist —
  ist sie das schon, deckt (2) den Fall bereits ab, keine doppelte Mail. `tasks.py` liegt
  bewusst auf oberster Ebene statt unter `api/` (siehe Celery-Autodiscover-Hinweis unten).
- **`app_strava_webhook`** — Strava-Push-Webhook, Endpoint **außerhalb** von `/api/`:
  `/strava/webhook/`. `GET` = Subscription-Challenge, `POST` → Celery-Task
  `process_strava_webhook` (max_retries=3): `delete` löscht `Ride`, `create` importiert via
  `StravaImportService`. Management-Command `resync_strava_activities.py` für manuelles
  Re-Enqueue fehlgeschlagener Imports.
- **`core`** — Projekt-Config: `settings.py`, `urls.py`, `celery.py`.

## URL-Struktur (`core/urls.py`)

```
""              -> app_strava_webhook   (/strava/webhook/, NICHT unter /api/)
"admin/"        -> Django Admin
"api/"          -> app_auth             (/api/strava/auth|me|logout/)
"api/"          -> app_dashboard        (/api/strava/sync*, /api/activities/*)
"api/"          -> app_maintenance      (/api/maintenance/*)
```

## Auth-Flow

Django Built-in `User` (kein `AUTH_USER_MODEL`-Override), 1:1 verknüpft mit `StravaProfile`.
Frontend holt Strava-OAuth-`code` → `POST /api/strava/auth/` → Backend tauscht Token,
erstellt/aktualisiert `User`+`StravaProfile`, ruft `login()`, speichert zusätzlich
`strava_athlete_id` in der Session (wird downstream statt `request.user` genutzt).
Der echte Name (`firstname`/`lastname` aus der Strava-Antwort) wird bewusst **nicht**
persistiert (kein DB-Feld, daher auch nicht im Admin sichtbar) — `StravaAuthCallbackView`
gibt ihn nur einmalig im Login-Response direkt aus `athlete_data` zurück, `GET /api/strava/me/`
liefert nur die `athlete_id`. Das Frontend cached den Namen für die Begrüßung clientseitig
in `localStorage` (`StravaService.setLoggedInUser`/`displayNameStorageKey`), nicht im Backend.
**Nur Session-Cookie-Auth**, kein JWT. `CsrfExemptSessionAuthentication`
(`app_auth/mixins.py`) nur auf Login/Logout. Jede View setzt `permission_classes` explizit.

### Dev-Mock ohne echten Strava-Login

Für lokale Entwicklung ohne Strava-OAuth-Roundtrip: `POST /api/dev/login/`
(`app_auth/api/dev_views.py::DevLoginView`) loggt einen festen Fake-Athleten
(`app_auth/dev_auth.py::DEV_ATHLETE_ID`) ein — legt `User`+`StravaProfile` on-demand an,
ruft `login()` und setzt `strava_athlete_id` in der Session, genau wie
`StravaAuthCallbackView`. Die Route wird nur bei `settings.DEBUG=True` in
`app_auth/api/urls.py` registriert (existiert in Produktion also gar nicht), die View
prüft `DEBUG` zusätzlich selbst als zweite Absicherung. Passende Testdaten dazu liefert
das Management-Command `python manage.py seed_dev_data` (`app_maintenance/management/
commands/seed_dev_data.py`, ebenfalls nur mit `DEBUG=True` lauffähig): legt ein Bike,
montierte Komponenten (aus der bestehenden `component_templates`-Fixture) und eine
Ride-Historie mit synthetischen Wetterdaten an und berechnet `weather_wear_km` synchron
(kein Celery-Worker nötig). Die Fake-Routen sind **geschlossene Schleifen** mit passendem
`RideStream` (`_loop_route()`), nicht die frühere Zwei-Punkt-Gerade — nur so drehen sich
die Kurse über die Fahrt und die Windabschnitte auf der Karte unterscheiden sich
überhaupt. `--reset` löscht vorhandene Fake-Daten vorher, `--rides N`
steuert die Anzahl der Fake-Rides. Läuft weiterhin gegen die echte Postgres/PostGIS-DB
(kein SQLite-Fallback) — "fake" bezieht sich auf Auth-Roundtrip und Dateninhalt, nicht auf
die DB-Engine.

## Bekannte Lücken / Quirks (Stand: siehe Git-History für Aktualität)

- Der Strava-Push-Webhook lief in Produktion nie: `settings.STRAVA_VERIFY_TOKEN` war
  nirgends definiert → `GET /strava/webhook/` (Strava-Verify-Callback beim Anlegen der
  Subscription) crashte mit `AttributeError`, seitdem existierte nie eine funktionierende
  Subscription (0 `process_strava_webhook`-Tasks in den Celery-Logs). Setting ist inzwischen
  in `settings.py`/`.env` ergänzt (siehe Env Vars unten). Zusätzlich meldet Strava's
  `GET /api/v3/push_subscriptions` aktuell `403 Application Status: Inactive` — die
  Strava-App selbst ist auf strava.com/settings/api als inaktiv markiert, unabhängig vom
  Server. Muss dort reaktiviert werden, bevor die Subscription per API angelegt werden kann.
- `debug.log` ist aktuell in Git getrackt (siehe `git status`) — sollte vermutlich in
  `.gitignore`, prüfen bevor weitere Commits den Log-Diff aufblähen.
- `core/celery.py` ruft `app.autodiscover_tasks()` ohne Argumente auf — das sucht bei
  Celery+Django nur `<app_label>.tasks` (eine Ebene), nicht `<app_label>.api.tasks`. Weder
  `app_dashboard/tasks.py` noch `app_maintenance/tasks.py` existieren; alle echten Task-Module
  liegen unter `api/tasks.py`. Die bestehenden Tasks funktionieren offenbar nur, weil sie
  transitiv über `views.py`-Importe (`from .tasks import ...`) geladen werden. Neue Tasks
  müssen auf derselben Import-Kette reiten (siehe `recompute_weather_wear_for_bike` als
  Beispiel) — nach jedem Deploy mit `celery -A core inspect registered` verifizieren.
  Ausnahme: `app_notifications/tasks.py` liegt bewusst auf oberster Ebene (nicht unter
  `api/`), gerade weil seine Tasks von Celery Beat direkt per Namen aufgerufen werden (kein
  View importiert sie) — `autodiscover_tasks()` findet es dort nativ, ohne auf die fragile
  Import-Kette angewiesen zu sein.
- `ComponentTemplateSerializer.Meta.fields` hatte lange `default_in_group` und
  `maintenance_kind` vergessen, obwohl beide Felder im Model existieren und das
  Frontend-Model (`ComponentTemplate` in `maintenance.models.ts`) sie als vorhanden
  voraussetzt — die API lieferte sie einfach nicht mit. Sichtbare Folge: im
  Baugruppen-Anlegen-Dialog waren **alle** Teile-/Verbrauchsmaterial-Checkboxen
  unabhängig vom Katalog-Flag unchecked (`t.default_in_group` kam im Frontend als
  `undefined` an → falsy). Gefunden erst beim Durchklicken der echten UI per
  Playwright — API-Testskripte gegen `django.test.Client` hatten das nie bemerkt, weil
  sie die Rohdaten selbst prüften statt die Anzeige. Jetzt behoben; bei künftigen
  Serializer-Feldern im Zweifel gegen das Frontend-Model gegenchecken statt anzunehmen,
  dass "das Feld existiert ja im Model" ausreicht.
## Env Vars (`.env`, nicht committed)

`DJANGO_SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`,
`CSRF_TRUSTED_ORIGINS`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
`STRAVA_VERIFY_TOKEN` (frei wählbarer Token für den `hub.verify_token`-Handshake beim
Anlegen der Push-Subscription, muss zu keinem externen Wert passen),
`AI_PROVIDER` (`gemini`|`groq`, Default `gemini`), `GEMINI_API_KEY`, `GEMINI_MODEL`,
`GROQ_API_KEY`, `GROQ_MODEL`, `EMAIL_BACKEND` (Default Djangos SMTP-Backend),
`EMAIL_HOST` (Default `smtp-relay.brevo.com`), `EMAIL_PORT` (Default `587`),
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` (Default `True`),
`DEFAULT_FROM_EMAIL` (Default = `EMAIL_HOST_USER`), `FRONTEND_URL` (für Links in E-Mails,
Default `http://localhost:3000`), `MAINTENANCE_EMAIL_CHECK_HOUR` (Default `7`, Stunde der
täglichen `app_notifications`-Checks). Die AI-Keys werden nur für die optionale
KI-Erklärung gebraucht — ohne sie liefert der Endpoint kontrolliert 503, der Rest der App
funktioniert unabhängig davon; E-Mail-Versand ist analog fehlertolerant
(`send_templated_email()` loggt und gibt `False` zurück statt zu werfen, auch bei fehlenden
`EMAIL_*`-Werten). Alle `EMAIL_*`-Settings sind bewusst generisch/env-gesteuert gehalten,
damit ein späterer Wechsel von Brevo auf einen eigenen SMTP-Server nur `.env`-Werte ändert,
keinen Code. Werte niemals in Doku oder Code-Kommentare übernehmen.

## Testing

Kein pytest, sondern DRF `APITestCase` über `python manage.py test`.
- `app_dashboard/tests.py`: `StravaSyncView` (Dispatch, No-Double-Dispatch, Auth-Required),
  `run_strava_sync`-Task (Success, 403-Reconnect, Generic-Failure) via `unittest.mock`,
  `ActivitySummaryViewTests` (KI-Fahrt-Zusammenfassung, Caching/Refresh, `requests.post`
  gemockt über `app_maintenance.api.ai_providers.requests.post`).
- `app_dashboard/test_wind.py`: reine Formel-Tests (`SimpleTestCase`, keine DB) für
  `api/wind.py`. Kernfall ist `OutAndBackTests` — Hin-und-Rück-Route bei konstantem Wind
  muss auf dem Hinweg Gegenwind, auf dem Rückweg Rückenwind und im Mittel ~0 liefern;
  genau das war mit der alten Start-Ziel-Berechnung nicht der Fall. Dazu
  zirkuläre Windrichtungs-Interpolation, grober Fallback ohne Stream,
  Distanzgewichtung des Durchschnitts und die Vorzeichen-Konvention
  (positiv = Gegenwind), an der die UI-Einfärbung hängt.
- `app_maintenance/tests.py`: `ComponentCheckTests` (Custom-Warn-Days, Overdue/Critical,
  `condition_pct`-Rejection, Release via Check mit Snooze, Auth-Required, Wetter-Achse
  treibt `warn_status_overall`), `ComputeWearAsOfProjectionTests` (Tage-Achse-Projektion für
  die Fahrt-Vorhersage-Warnung, `as_of` defaultet auf heute).
- `app_maintenance/test_weather_wear.py`: `WeatherWearCalculatorTests` (reine Formel, keine
  DB), `WeatherWearServiceTests` (Recompute gegen echte `Ride`-Objekte, inkl.
  `carried_over_weather_wear_km` wird draufaddiert statt überschrieben),
  `ComponentWeatherExplanationViewTests` (KI-Endpoint, `requests.post` gemockt in
  `ai_providers.py`, inkl. Zweit-Prüfung durch das jeweils andere Gemini/Groq-Modell:
  bestandene Prüfung liefert die Antwort direkt, durchgefallene Prüfung löst genau eine
  Neugenerierung aus), `ComponentCheckInstructionsViewTests` (KI-Prüfanleitung, Caching,
  Invalidierung durch Status-Änderung nach Freigabe), `BikeConditionReportViewTests`
  (KI-Zustandsbericht, Caching, Invalidierung durch neuen Ride-Import),
  `RecomputeWeatherWearForBikeTaskTests` (löst nach erfolgreicher Neuberechnung
  `app_notifications.tasks.check_component_warnings_for_bike` aus, aber nicht bei Fehlschlag),
  `WeatherWearParkedAssemblyTests` (Fahrten aus einer Parkphase zählen nicht mit, Altbestand
  ohne Perioden rechnet unverändert weiter, `recompute_bike` überspringt geparkte Sätze).
- `app_maintenance/test_usage_periods.py`: reine Fenster-Arithmetik aus `api/usage.py`
  (`SimpleTestCase`, keine DB, Modelle als Stubs — im Stil von `app_dashboard/test_wind.py`).
  Kernfälle: Parklücke fällt aus den km- und Datums-Fenstern, Fallback ohne Baugruppe/ohne
  Perioden entspricht exakt dem früheren Verhalten, `since_km`-Baseline nach einer Freigabe,
  frisch montiertes Teil = 0 km (nicht `unknown`).
- `app_maintenance/test_query_counts.py`: `QueryScalingTests` — Regressionstests gegen
  N+1. Bewusst **keine** festen Query-Zahlen (die wären bei jeder Serializer-Änderung rot),
  sondern die Invariante: dieselbe Route mit 3 und mit 15 Slots muss gleich viele Queries
  brauchen (`/bikes/`, `/bikes/<id>/`, `/bikes/<id>/assemblies/`). Dazu: Ride-Geometrie
  darf in der Bike-Liste nicht geladen werden, `with_total_distance()` macht
  `total_distance_km` query-frei (ohne Annotation bleibt der Aggregat-Fallback), und
  `mounted_component` muss den Prefetch-Cache nutzen.
- `app_maintenance/test_assemblies.py`: `AssemblyCreateTests` (atomar Slots+Components+
  Intervalle, überspringt `include:false`, lehnt template-fremd/Consumable-in-Parts ab,
  Bike-Typ-Mismatch → 400, zweite Instanz derselben Gruppe entsteht geparkt bzw. verdrängt
  mit `activate:true`, `BikeAssembly.clean()`-Invariante,
  Auth), `AssemblyListTests` (`assemblies`/`parked_assemblies`/`ungrouped_slots`/
  `spare_components`/`available_groups` — Gruppe mit aktiver Instanz bleibt in
  `available_groups` enthalten, nur mit `has_active_instance: true` markiert statt
  herausgefiltert, `assembly_km` = Summe der Nutzungszeiträume (nicht mehr der
  Einbau-km-Stand des ältesten Teils — ein einzelner Reifenwechsel darf die Laufleistung
  des Satzes nicht zurücksetzen), `worst_status` bezieht
  überfällige Intervalle ein), `AssemblySwapTests` (alte Instanz inaktiv + Komponenten
  ausgebaut, neue aktiv, Historie abfragbar), `AssemblyActivateTests` (Wechsel parkt die
  andere Instanz ohne sie auszumustern, Hin- und Zurückwechseln, ausgemusterte lassen sich
  nicht wieder aufziehen, Athleten-Scoping, Auth), `ParkedAssemblyWearTests` (**der Kernfall**:
  geparkter Satz sammelt keine km, altert aber weiter in Tagen; seine Slots verschwinden aus
  der Bike-Übersicht), `LegacyAssemblyWithoutPeriodsTests` (Baugruppen ohne Nutzungszeitraum
  — Altbestand, Admin, `seed_dev_data` — bekommen beim Abziehen rückwirkend einen, sonst
  liefe der Alt-Fallback weiter), `AssemblyReuseExistingComponentTests` ("vorhandene
  Komponente übernehmen": Slot wird umgehängt statt eine zweite Component anzulegen, der
  Verlauf des übernommenen Teils bleibt erhalten statt durch die neue Nutzungsperiode
  abgeschnitten zu werden, Ablehnung bei fremdem Bike/bereits gruppiertem Slot/
  Template-Mismatch/fehlendem montierten Teil, derselbe Mechanismus funktioniert auch über
  `swap/`), `AssemblyReuseSpareComponentTests` (verwandter Fall für bereits *ausgebaute*
  Teile über `reuse_component_id`: Component zieht in einen frischen Slot um statt eine
  zweite anzulegen, der vor dem Ausbau aufgelaufene Verschleiß bleibt via
  `carried_over_wear_km` erhalten (macht bei Wiedermontage am eingefrorenen Stand weiter,
  nicht bei 0) während die Tage-Achse seit dem ursprünglichen Einbau ohnehin weiterzählt,
  Ablehnung bei noch montierter/fremder Bike-Component/Template-Mismatch, gegenseitiger
  Ausschluss mit `existing_slot_id`) — Regressionstest für den Prod-Bug, bei dem ein
  ausgemustertes Teil (Mavic-Felge) nirgends mehr als Übernahme-Vorschlag auftauchte,
  `test_spare_components_lists_every_candidate_for_a_template` (mehrere ausgebaute Teile
  desselben Templates fallen in `spare_components` nicht auf einen Kandidaten zusammen —
  Regressionstest dafür, dass eine Datums-Heuristik dort regelmäßig danebenlag),
  `CassetteBelongsToRearWheelGroupTests`
  (Regressionstest gegen die echten Migrations-Daten: Kassette gehört zu "Laufrad hinten",
  nicht mehr zu "Antrieb").
- `app_maintenance/test_intervals.py`: `MaintenanceIntervalStatusTests` (km-/Tage-Ratio,
  `as_of`-Projektion der Tage-Achse, `unknown` ohne Grenzen), `MaintenanceIntervalLogViewTests`
  (`/log/` setzt Baseline zurück + hängt `MaintenanceLog` an, explizite Werte,
  Athleten-Scoping, Ad-hoc-Intervall anlegen/löschen).
- `app_maintenance/test_ride_wear_impact.py`: `RideWearBreakdownTests` (Regenfahrt kostet
  mehr als dieselbe Trockenfahrt; Historie: zum Fahrtzeitpunkt montierte Teile zählen,
  nie montierte Ersatzteile nicht), `RideMultiplierDetailTests`,
  `ActivityWearImpactViewTests` (Caching, Invalidierung bei Komponenten-Änderung, und
  vor allem: ein KI-Ausfall liefert weiterhin 200 mit den Zahlen, kein 503).
- `app_maintenance/test_bike_assistant.py`: `KudoCatalogGuardTests` — die eigentliche
  Sicherheitsgrenze. Erfundene `template_id`s, Templates aus einer fremden Gruppe und
  als Teil ausgegebenes Verbrauchsmaterial müssen serverseitig herausfallen; dazu
  ```json-Fences, kaputtes JSON und Provider-Ausfall. `KudoModelSuggestionTests`
  (leere Liste = „Hersteller unbekannt" vs. `None` = „KI kaputt" — ein Unterschied),
  `AssistantEndpointTests` (503-Pfad, fremdes Bike → 404, Auth).
- `app_notifications/tests.py`: `SendTemplatedEmailTests` (Opt-out-Flag, fehlende
  E-Mail-Adresse), `PredictNextRideDateTests` (Median-Vorhersage, Clamping bei
  Überfälligkeit auf "heute", zu wenig Historie → `None`), `CheckComponentWarningsTests`/
  `CheckComponentWarningsForBikeTests` (Sammel-Mail, Dedupe via
  `last_warn_notified_status`, Eskalation löst erneuten Versand aus, gemeinsamer Dedupe
  zwischen event- und tages-getriggertem Check), `CheckBikeUnsafePredictionsTests`
  (Projektion, Ausschluss bereits heute kritischer Bikes, Dedupe via
  `predicted_unsafe_notified_for_date`), `SendWelcomeEmailTaskTests`.
  Dazu `HtmlToPlaintextTests` (Stylesheet-Inhalt und HTML-Entities dürfen nicht im
  Plaintext-Teil landen) und `WarnLabelFilterTests` (deutsche Labels statt roher Slugs).
- `app_auth/tests.py`: `CurrentUserSettingsTests` (GET/PATCH `me/`, `needs_email`,
  Nachholen der Willkommens-Mail beim ersten Setzen einer Adresse — und nur dann),
  `StravaAuthCallbackWelcomeEmailTests` (Willkommens-Mail-Trigger nur bei
  Erstanlage eines Profils, nicht bei Re-Login) — sonst keine weiteren Tests für `app_auth`.
  Keine Tests für `app_strava_webhook`.

---

## Django & DRF Best Practices

### 1. Python & Django Code Style

- Nutze striktes Type Hinting für alle Funktions- und Methodensignaturen.
- Halte dich an das Prinzip: **"Fat Models, Thin Views"** – Business-Logik gehört in Models, Custom Managers oder Services, nicht in die View.
- Nutze `path()` anstelle von veraltetem `re_path()`, es sei denn, es ist absolut notwendig.
- Verwende immer `get_user_model()` anstelle von Direktimporten des User-Models.

### 2. ORM & Performance

- **N+1 Query-Verbot:** Verwende immer `select_related()` für ForeignKeys/OneToOne-Beziehungen und `prefetch_related()` für ManyToMany-/Reverse-ForeignKeys.
- Setze sinnvolle Datenbank-Indizes (`db_index=True` oder `Meta.indexes`) für Felder, nach denen häufig gefiltert oder sortiert wird.
- Nutze `.exists()` und `.count()` effizient, statt ganze QuerySets zu evaluieren.

### 3. Django REST Framework (DRF) Conventions

- **Class-Based Views:** Bevorzuge `ModelViewSet` oder generische Views (`ListCreateAPIView`, etc.) gegenüber API-Decorators (`@api_view`).
- **Serializers:** Bevorzuge `ModelSerializer`. Deklariere Felder immer explizit in `fields = [...]`, verwende niemals `fields = '__all__'`.
- **Validation:** Implementiere Validierungslogik in Serializer-Methoden (`validate_<field_name>` oder `validate()`).
- **Routers:** Registriere ViewSets sauber über DRF `DefaultRouter` oder `SimpleRouter`.

### 4. Security & Permissions

- Setze **niemals** `DEBUG = True` im Produktionskontext (prüfe Umgebungsvariablen via `python-dotenv` oder `django-environ`).
- Jedes API-Endpoint benötigt explizite `permission_classes`. Standardmäßig sollte `IsAuthenticated` oder sicherer aktiv sein.
- Nutze Djangos eingebaute Security-Features (CSRF-Schutz, Password Hashing, XSS-Schutz). Store Secrets niemals in `settings.py`.

### 5. Testing Requirements

- Schreibe für jede neue API-Komponente Unit- oder Integrationstests (bevorzuge DRF `APITestCase`).
- Teste sowohl den "Happy Path" (200/201 OK) als auch Edge Cases (400 Bad Request, 403 Forbidden, 404 Not Found).

---

## Pflege dieser Datei

Diese Datei soll mit dem Projekt mitwachsen. Wenn sich während einer Session etwas als
falsch/veraltet herausstellt, oder eine neue App/ein neues Model/ein wichtiger Endpoint
hinzukommt, aktualisiere den passenden Abschnitt oben (nicht nur "Bekannte Lücken" anhängen,
sondern die Doku korrigieren). Keine Secrets, keine Task-spezifischen Details, keine
chronologischen Change-Logs — nur dauerhaft gültiges Architektur-/Konventionswissen.
