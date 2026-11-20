import unittest
import sqlite3

from src.database import apply_schema, connect
from src.demo_medications import MEDICATION_NAME, MEDICATION_SOURCE
from src.enrich_medications import enrich
from src.identifiers import identifier
from src.seed_data import generate, synchronize
from src.patient_summary import medication_events


class MedicationDetailsTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        apply_schema(self.db)
        with self.db:
            synchronize(self.db, generate())
        self.patient = identifier("patients", "P003")

    def test_seed_has_explicit_synthetic_schedule(self):
        self.db.row_factory = sqlite3.Row
        rows = medication_events(self.db, self.patient)
        self.assertEqual([row["treatment_name"] for row in rows], [MEDICATION_NAME] * 3)
        self.assertEqual([row["medication_frequency"] for row in rows], ["Once daily", "Twice daily", None])
        self.assertTrue(all(row["medication_details_source"] == MEDICATION_SOURCE for row in rows))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM treatments WHERE event_type <> 'medication' AND medication_frequency IS NOT NULL"
        ).fetchone()[0], 0)

    def test_enrichment_is_repeatable_and_preserves_other_data(self):
        before = {table: self.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                  for table in ("patients", "checkins", "weight", "symptoms")}
        with self.db:
            self.db.execute(
                "UPDATE treatments SET treatment_name = 'Pain medication', medication_frequency = NULL, "
                "medication_details_source = NULL WHERE event_type = 'medication'"
            )
        self.assertEqual(enrich(self.db), 3)
        self.assertEqual(enrich(self.db), 0)
        after = {table: self.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in before}
        self.assertEqual(before, after)

    def test_conflicting_edits_roll_back_the_batch(self):
        with self.db:
            self.db.execute(
                "UPDATE treatments SET treatment_name = 'Pain medication', medication_frequency = NULL, "
                "medication_details_source = NULL WHERE event_type = 'medication'"
            )
            self.db.execute(
                "UPDATE treatments SET treatment_name = 'Existing edit' WHERE event_type = 'medication' AND treatment_day = 26"
            )
        before = self.db.execute("SELECT * FROM treatments ORDER BY 1").fetchall()
        with self.assertRaisesRegex(ValueError, "already edited"):
            enrich(self.db)
        self.assertEqual(self.db.execute("SELECT * FROM treatments ORDER BY 1").fetchall(), before)
