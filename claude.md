# Project: Django & DRF Backend

## Tech Stack

- Python 3.11+ (mit modernen Type Hints)
- Django 5.x
- Django REST Framework (DRF)
- PostgreSQL (oder SQLite für lokale Entwicklung)

## Commands

- Build/Install: `pip install -r requirements.txt` oder `poetry install`
- Database Migrations: `python manage.py makemigrations` && `python manage.py migrate`
- Dev Server: `python manage.py runserver`
- Tests: `python manage.py test` oder `pytest`
- Linter/Formatter: `black . && isort . && flake8`

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
