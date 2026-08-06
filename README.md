# 🚴 Kudos Care Backend

**REST-API für eine Wartungs-Tracking-App für Fahrräder/Motorräder mit Strava-Integration**, gebaut mit Django, DRF, PostGIS und Celery.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-6.0-092E20?logo=django&logoColor=white)
![DRF](https://img.shields.io/badge/Django%20REST%20Framework-3.17-A30000?logo=django&logoColor=white)
![PostGIS](https://img.shields.io/badge/PostgreSQL-PostGIS-4169E1?logo=postgresql&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-Redis-37814A?logo=celery&logoColor=white)
![Strava](https://img.shields.io/badge/Strava-OAuth%20%2B%20API-FC4C02?logo=strava&logoColor=white)

📁 **Repository:** [github.com/Marc-Schaar/kudos_care_backend](https://github.com/Marc-Schaar/kudos_care_backend) · 🖥️ **Frontend:** [kudos_care_frontend](https://github.com/Marc-Schaar/kudos-care) · 🌐 **Portfolio:** [marc-schaar.com](https://marc-schaar.com)

> Persönliches Projekt mit echten Strava-Trainingsdaten – kein öffentlicher Live-Demo-Link, aber vollständig produktiv im Einsatz (eigener Server, Gunicorn + Celery + Supervisor).

---

> 🇬🇧 **English:** Kudos Care is a maintenance-tracking API for bikes/motorcycles with Strava integration. Users log in via Strava OAuth, their rides and bikes get synced, historical weather/wind data is attached per ride (Open-Meteo), and component wear (chain, tires, brake pads, …) is tracked from distance/time/weather exposure — weighted by rain/heat/cold/wind — with a resulting `ok`/`warn`/`critical` status. An optional AI layer (Gemini, with a Groq fallback) narrates the computed numbers in plain language on request, but never calculates anything itself. Built with Django, DRF, PostGIS, and Celery/Redis for async processing.

## Über das Projekt

Kudos Care ist eine Wartungs-Tracking-App für Fahrräder/Motorräder mit Strava-Integration. Nutzer loggen sich via Strava-OAuth ein, ihre Aktivitäten ("Rides") und Bikes werden synchronisiert, historische Wetter-/Winddaten werden pro Ride ergänzt (Open-Meteo), und der Verschleiß von Bike-Komponenten (Kette, Reifen, Bremsbeläge, …) wird anhand von Kilometern, Stunden und Tagen seit Montage getrackt – zusätzlich wetter-gewichtet nach Regen, Hitze, Kälte und Wind pro Ride. Das Ergebnis ist ein Status `ok` / `warn` / `critical` je Komponente. Eine optionale KI-Erklärung (Gemini, mit Groq-Fallback) narriert auf Anfrage die berechneten Zahlen in Worten – berechnet aber nie selbst.

Dieses Backend bedient das zugehörige Angular-Frontend **[kudos_care_frontend](https://github.com/Marc-Schaar/kudos-care)**.

## ✨ Features

- **Strava-OAuth-Login** inkl. Token-Refresh und automatischem Sync von Bikes und Aktivitäten
- **Wetter-gewichtetes Verschleiß-Tracking** – Ride-Historie seit Bauteil-Montage wird gegen Wetterdaten (Regen/Hitze/Kälte/Wind) gewichtet und zu einem Status `ok`/`warn`/`critical` verdichtet
- **Geodaten-Verarbeitung** – Ride-Tracks als PostGIS `LineString`, Polyline-Decode + Douglas-Peucker-Vereinfachung, Gegenwind-Berechnung aus Kurs und Windrichtung
- **Asynchrone Jobs via Celery/Redis** – Strava-Sync, Webhook-Import und Verschleiß-Neuberechnung laufen im Hintergrund, mit Soft/Hard-Timeouts gegen hängende Tasks
- **Strava-Push-Webhook** für Echtzeit-Import neuer/gelöschter Aktivitäten
- **Optionale KI-Erklärungen** (Wetter-Verschleiß, Bike-Zustandsbericht, Prüfanleitung, Ride-Zusammenfassung) über einen austauschbaren Gemini/Groq-Adapter mit automatischem Fallback – die App bleibt ohne konfigurierte AI-Keys voll funktionsfähig (Endpoints liefern dann kontrolliert `503`)
- **Session-Cookie-Auth** (kein JWT) mit strikt gescopten Querysets pro Strava-Athlet

## 🛠️ Tech-Stack

| Bereich         | Technologie                                              |
|------------------|-----------------------------------------------------------|
| Sprache/Framework | Python 3.11+, Django 6.0, Django REST Framework 3.17     |
| Datenbank        | PostgreSQL + PostGIS (`django.contrib.gis`), Geodaten als `LineString`/`Point` |
| Async/Jobs       | Celery 5.6 + Redis (Broker & Result-Backend)               |
| Externe APIs     | Strava API (OAuth + Activities/Streams), Open-Meteo (historisches Wetter/Wind) |
| KI               | Google Gemini (Flash-Lite) mit automatischem Groq-Fallback, austauschbar über `AI_PROVIDER` |
| Auth             | Django-Session-Cookie-Auth (`SessionAuthentication`), kein JWT |
| Deployment       | Gunicorn + Supervisor + Nginx, GitHub Actions (SSH-Deploy) |

## 🏗️ Architektur — Apps

| App | Verantwortung |
|---|---|
| `app_auth` | Strava-OAuth-Login, `StravaProfile` (Access/Refresh-Token, Sync-Status), geteilter Strava-HTTP-Helper mit Token-Refresh |
| `app_dashboard` | Ride-Ingestion, Geodaten, Wetter/Wind, KI-Fahrt-Zusammenfassung |
| `app_maintenance` | Kern-Domäne: Bikes, Component-Slots, Verschleiß-Berechnung (km-/Tage-/Wetter-Achse), KI-Erklärungen |
| `app_strava_webhook` | Strava-Push-Webhook (`/strava/webhook/`, außerhalb von `/api/`) für Echtzeit-Import |
| `core` | Projekt-Config: Settings, URL-Routing, Celery-App |

## 🚀 Lokal starten

**Voraussetzungen:** Python 3.11+, PostgreSQL mit PostGIS-Extension, Redis, GDAL/GEOS (für `django.contrib.gis`)

```bash
git clone https://github.com/Marc-Schaar/kudos_care_backend.git
cd kudos_care_backend

python3 -m venv venv
source venv/bin/activate        # Windows: .\venv\Scripts\activate
pip install -r requirements.txt
```

`.env` mit den benötigten Variablen anlegen (siehe [Konfiguration](#-konfiguration)), dann:

```bash
python manage.py migrate
python manage.py runserver
```

Für Strava-Sync und Webhook-Import zusätzlich Redis starten und den Celery-Worker ausführen:

```bash
celery -A core worker -l info
```

## ⚙️ Konfiguration

Alle sensiblen und umgebungsabhängigen Werte kommen aus einer nicht committeten `.env`-Datei:

`DJANGO_SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `AI_PROVIDER` (`gemini`|`groq`, Default `gemini`), `GEMINI_API_KEY`, `GEMINI_MODEL`, `GROQ_API_KEY`, `GROQ_MODEL`

Die AI-Keys werden nur für die optionalen KI-Erklärungen gebraucht – ohne sie liefert der jeweilige Endpoint kontrolliert `503`, der Rest der App funktioniert unabhängig davon.

## ✅ Testing

```bash
python manage.py test
```

Die Testsuite deckt Strava-Sync (inkl. Fehlerfälle), Verschleiß-Berechnung (reine Formel und Recompute gegen echte Ride-Historie), sowie die KI-Endpoints (Caching, Invalidierung, gemockte HTTP-Calls) ab.

## 👤 Kontakt

**Marc Schaar**
📧 [kontakt@marc-schaar.com](mailto:kontakt@marc-schaar.com) · 🌐 [marc-schaar.com](https://marc-schaar.com) · 💻 [GitHub](https://github.com/Marc-Schaar)

Dieses Projekt ist ein persönliches Side-Project, das im Alltag zur eigenen Fahrrad-/Motorrad-Wartung genutzt wird – gleichzeitig Teil meines Portfolios als Beispiel für asynchrone Job-Verarbeitung, Geodaten und den pragmatischen Einsatz von KI-Endpoints.
