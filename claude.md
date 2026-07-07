# Project: Kudos Care — Backend (Django & DRF)

## Was ist Kudos Care?

Eine Wartungs-Tracking-App für Fahrräder/Motorräder mit Strava-Integration. Nutzer loggen
sich via Strava-OAuth ein, ihre Aktivitäten ("Rides") und Bikes werden synchronisiert,
historische Wetter-/Winddaten werden pro Ride ergänzt (Open-Meteo), und der Verschleiß von
Bike-Komponenten (Kette, Reifen, Bremsbeläge, ...) wird anhand von km/Stunden/Tagen seit
Montage getrackt, mit Status `ok` / `warn` / `critical`. UI-Sprache und viele Code-Kommentare
sind Deutsch. Zugehöriges Frontend: `kudos_care_frontend` (Angular), siehe dessen `CLAUDE.md`.

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
- Tests: `python manage.py test` (kein pytest konfiguriert, trotz anderslautender Vermutung)
- Linter/Formatter: `black . && isort . && flake8`

## Architektur — Apps

- **`app_auth`** — Strava-OAuth-Login. Model `StravaProfile` (1:1 zu Django `User`,
  speichert Access/Refresh-Token + Sync-Status). Endpoints: `POST /api/strava/auth/`,
  `GET /api/strava/me/`, `POST /api/strava/logout/`. `api/utils.py` hat
  `get_valid_access_token()`/`strava_get()` als geteilten Strava-HTTP-Helper mit
  Token-Refresh + 401-Retry — von anderen Apps wiederverwendet.
- **`app_dashboard`** — Ride-Ingestion, Geodaten, Wetter/Wind. Models `Ride` (PostGIS-Track,
  `weather_data` JSONField, FK zu `StravaProfile`+`Bike`), `RideStream` (Rohdaten-Zeitreihe).
  `api/services.py`: `StravaSyncService` (Bikes + paginierte Activities),
  `StravaImportService.sync_activity_to_db` (Polyline-Decode → Shapely-RDP-Simplify →
  Gear-Matching → Streams → Open-Meteo-Wetter → Headwind-Berechnung), `WeatherService`.
  Celery-Task `run_strava_sync` in `api/tasks.py`. Management-Command `recompute_wind.py`
  (Backfill, `--dry-run`).
- **`app_maintenance`** — Kern-Domäne Verschleiß-Tracking. Models: `Bike`,
  `ComponentTemplate` (Katalog, Fixture `fixtures/component_templates.json`), `ComponentSlot`
  (Position am Bike, unique je `(bike, template)`), `Component` (physisches Teil,
  `is_mounted` via `clean()`/`save()`-Override erzwungen: nur 1 montiertes Teil je Slot),
  `ComponentCheck` (Log eines Checks/Release, optional `condition_pct` + Snooze).
  `AthleteMixin` (`api/views.py`) scoped alle Querysets auf
  `request.session["strava_athlete_id"]`. `WarnStatus` (`api/serializers.py`) berechnet
  `ok`/`warn`/`critical`/`unknown` aus einem Ratio (≥1.0 critical, ≥0.8 warn).
  Endpoints unter `/api/maintenance/`: `bikes/`, `bikes/<id>/slots/`, `slots/<id>/mount|unmount`,
  `slots/<id>/components/`, `components/<id>/check/`, `templates/`.
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
**Nur Session-Cookie-Auth**, kein JWT. `CsrfExemptSessionAuthentication`
(`app_auth/mixins.py`) nur auf Login/Logout. Jede View setzt `permission_classes` explizit.

## Bekannte Lücken / Quirks (Stand: siehe Git-History für Aktualität)

- `app_strava_webhook/api/views.py` liest `settings.STRAVA_VERIFY_TOKEN`, das Setting ist
  **nirgends definiert** (weder `settings.py` noch `.env`) → GET-Verification würde mit
  `AttributeError` crashen. Vor Produktiv-Einsatz des Webhooks fixen.
- `ComponentSlot.wear_km` (models.py) enthält toten Code (`stravaprofile_set`-Zeile).
- `BikeListView.get_queryset` hat auskommentierten Debug-Code.
- `debug.log` ist aktuell in Git getrackt (siehe `git status`) — sollte vermutlich in
  `.gitignore`, prüfen bevor weitere Commits den Log-Diff aufblähen.

## Env Vars (`.env`, nicht committed)

`DJANGO_SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`,
`CSRF_TRUSTED_ORIGINS`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`.
Werte niemals in Doku oder Code-Kommentare übernehmen.

## Testing

Kein pytest, sondern DRF `APITestCase` über `python manage.py test`.
- `app_dashboard/tests.py`: `StravaSyncView` (Dispatch, No-Double-Dispatch, Auth-Required),
  `run_strava_sync`-Task (Success, 403-Reconnect, Generic-Failure) via `unittest.mock`.
- `app_maintenance/tests.py`: `ComponentCheckTests` (Custom-Warn-Days, Overdue/Critical,
  `condition_pct`-Rejection, Release via Check mit Snooze, Auth-Required).
- Keine Tests für `app_auth`/`app_strava_webhook`.

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
