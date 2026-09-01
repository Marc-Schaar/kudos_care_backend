"""
Reine Fenster-Logik aus `api/usage.py` — ohne DB, analog zu
`app_dashboard/test_wind.py`. Die Modelle werden durch schlanke Stubs ersetzt,
weil hier ausschließlich die Schnitt-Arithmetik geprüft wird: welche
Odometer-/Datums-Abschnitte zählen, wenn eine Baugruppe zwischendurch abgezogen
war und einzelne Teile mitten in einem Zeitraum getauscht wurden.
"""

from datetime import date, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from app_maintenance.api.usage import (
    assembly_km_windows,
    component_active_km,
    component_date_windows,
    component_km_windows,
    date_in_windows,
)

TODAY = date(2026, 6, 1)


class _Periods(list):
    """Ersetzt den RelatedManager: `assembly.periods.all()` muss funktionieren."""

    def all(self):
        return self


def _period(started_at, started_km, ended_at=None, ended_km=None):
    return SimpleNamespace(
        started_at=started_at,
        started_distance_km=started_km,
        ended_at=ended_at,
        ended_distance_km=ended_km,
    )


def _assembly(*periods):
    return SimpleNamespace(periods=_Periods(periods))


def _component(
    distance_at_install=0.0,
    installed_at=TODAY - timedelta(days=100),
    assembly=None,
    is_mounted=True,
    retired_at=None,
    distance_at_retire=None,
):
    return SimpleNamespace(
        distance_at_install=distance_at_install,
        installed_at=installed_at,
        is_mounted=is_mounted,
        retired_at=retired_at,
        distance_at_retire=distance_at_retire,
        slot=SimpleNamespace(assembly=assembly),
    )


class AssemblyKmWindowTests(SimpleTestCase):
    def test_open_period_runs_to_current_odometer(self):
        assembly = _assembly(_period(TODAY - timedelta(days=30), 100.0))
        self.assertEqual(assembly_km_windows(assembly, 250.0), [(100.0, 250.0)])

    def test_closed_period_stops_at_its_end(self):
        assembly = _assembly(
            _period(
                TODAY - timedelta(days=60), 100.0, TODAY - timedelta(days=30), 150.0
            )
        )
        self.assertEqual(assembly_km_windows(assembly, 400.0), [(100.0, 150.0)])

    def test_parked_gap_is_not_counted(self):
        """Der Kernfall: 50 km gefahren, abgezogen, später wieder aufgezogen."""
        assembly = _assembly(
            _period(
                TODAY - timedelta(days=90), 100.0, TODAY - timedelta(days=60), 150.0
            ),
            _period(TODAY - timedelta(days=10), 300.0),
        )
        windows = assembly_km_windows(assembly, 340.0)
        self.assertEqual(windows, [(100.0, 150.0), (300.0, 340.0)])
        self.assertEqual(sum(end - start for start, end in windows), 90.0)

    def test_no_periods_yields_no_windows(self):
        self.assertEqual(assembly_km_windows(_assembly(), 300.0), [])

    def test_unknown_odometer_yields_no_windows(self):
        assembly = _assembly(_period(TODAY, 100.0))
        self.assertEqual(assembly_km_windows(assembly, None), [])


class ComponentKmWindowTests(SimpleTestCase):
    def test_falls_back_to_install_baseline_without_assembly(self):
        """Alt-Slots ohne Baugruppe rechnen exakt wie vor der Umstellung."""
        comp = _component(distance_at_install=40.0)
        self.assertEqual(component_km_windows(comp, 100.0), [(40.0, 100.0)])
        self.assertEqual(component_active_km(comp, 100.0), 60.0)

    def test_falls_back_when_assembly_has_no_periods(self):
        comp = _component(distance_at_install=40.0, assembly=_assembly())
        self.assertEqual(component_active_km(comp, 100.0), 60.0)

    def test_component_installed_mid_period_starts_at_its_own_baseline(self):
        assembly = _assembly(_period(TODAY - timedelta(days=30), 100.0))
        comp = _component(distance_at_install=180.0, assembly=assembly)
        self.assertEqual(component_km_windows(comp, 250.0), [(180.0, 250.0)])

    def test_parked_gap_is_not_counted_for_the_component(self):
        assembly = _assembly(
            _period(
                TODAY - timedelta(days=90), 100.0, TODAY - timedelta(days=60), 150.0
            ),
            _period(TODAY - timedelta(days=10), 300.0),
        )
        comp = _component(distance_at_install=100.0, assembly=assembly)
        self.assertEqual(component_active_km(comp, 340.0), 90.0)

    def test_retired_component_stops_at_its_retire_odometer(self):
        assembly = _assembly(_period(TODAY - timedelta(days=90), 100.0))
        comp = _component(
            distance_at_install=100.0,
            assembly=assembly,
            is_mounted=False,
            retired_at=TODAY - timedelta(days=5),
            distance_at_retire=220.0,
        )
        self.assertEqual(component_active_km(comp, 400.0), 120.0)

    def test_fresh_component_is_zero_km_not_unknown(self):
        assembly = _assembly(_period(TODAY, 100.0))
        comp = _component(distance_at_install=100.0, assembly=assembly)
        self.assertEqual(component_active_km(comp, 100.0), 0.0)

    def test_unknown_install_baseline_stays_unknown(self):
        comp = _component(distance_at_install=None)
        self.assertIsNone(component_active_km(comp, 100.0))


class SinceKmBaselineTests(SimpleTestCase):
    """`since_km` = Freigabe-Baseline (ComponentCheck.checked_at_distance_km)."""

    def test_baseline_clips_the_windows(self):
        comp = _component(distance_at_install=0.0)
        self.assertEqual(component_active_km(comp, 300.0, since_km=200.0), 100.0)

    def test_parked_gap_after_a_check_is_not_counted(self):
        assembly = _assembly(
            _period(TODAY - timedelta(days=90), 0.0, TODAY - timedelta(days=60), 150.0),
            _period(TODAY - timedelta(days=10), 300.0),
        )
        comp = _component(distance_at_install=0.0, assembly=assembly)
        # Freigabe bei 100 km: 50 km bis zum Abziehen + 40 km seit dem Aufziehen.
        self.assertEqual(component_active_km(comp, 340.0, since_km=100.0), 90.0)

    def test_check_at_current_odometer_is_zero_not_unknown(self):
        comp = _component(distance_at_install=0.0)
        self.assertEqual(component_active_km(comp, 300.0, since_km=300.0), 0.0)


class ComponentDateWindowTests(SimpleTestCase):
    def test_without_assembly_the_window_stays_open_from_install(self):
        installed = TODAY - timedelta(days=100)
        comp = _component(installed_at=installed)
        self.assertEqual(component_date_windows(comp), [(installed, None)])

    def test_windows_are_clipped_to_the_install_date(self):
        assembly = _assembly(_period(TODAY - timedelta(days=90), 100.0))
        comp = _component(installed_at=TODAY - timedelta(days=30), assembly=assembly)
        self.assertEqual(
            component_date_windows(comp), [(TODAY - timedelta(days=30), None)]
        )

    def test_ride_during_the_parked_gap_is_excluded(self):
        assembly = _assembly(
            _period(TODAY - timedelta(days=90), 0.0, TODAY - timedelta(days=60), 150.0),
            _period(TODAY - timedelta(days=10), 300.0),
        )
        comp = _component(installed_at=TODAY - timedelta(days=90), assembly=assembly)
        windows = component_date_windows(comp)

        self.assertTrue(date_in_windows(windows, TODAY - timedelta(days=70)))
        self.assertFalse(date_in_windows(windows, TODAY - timedelta(days=30)))
        self.assertTrue(date_in_windows(windows, TODAY - timedelta(days=1)))

    def test_without_install_date_there_are_no_windows(self):
        self.assertEqual(component_date_windows(_component(installed_at=None)), [])
