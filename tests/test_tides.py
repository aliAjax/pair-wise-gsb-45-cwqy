import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, BerthingRejected, Conflict, PermissionDenied, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
DUTY = Actor("duty-1", "duty_officer")
CONTROLLER = Actor("op-1", "port_controller")


def tide_payload(hour, height, berth="B12"):
    return {"berth": berth, "tide_hour": hour, "height_m": height}


class TideBoardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def add_tides(self, entries, berth="B12"):
        for hour, height in entries:
            self.service.create_tide(DUTY, tide_payload(hour, height, berth))

    def confirmed_record(self):
        record = self.service.create(Actor("creator", "port_controller"), "VOY-1", CREATE_DATA)
        return self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})

    def test_duplicate_tide_rejected(self):
        self.service.create_tide(DUTY, tide_payload(8, 1.2))
        with self.assertRaises(Conflict):
            self.service.create_tide(DUTY, tide_payload(8, 1.5))

    def test_tide_entry_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_tide(Actor("x", "outsider"), tide_payload(8, 1.2))

    def test_correction_keeps_before_and_after(self):
        obs = self.service.create_tide(DUTY, tide_payload(8, 1.2))
        updated = self.service.correct_tide(DUTY, obs["id"], {"height_m": 1.6, "reason": "抄表错误"})
        self.assertEqual(updated["height_m"], 1.6)
        history = self.service.tide_history(CONTROLLER, obs["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["old_height_m"], 1.2)
        self.assertEqual(history[0]["new_height_m"], 1.6)
        self.assertEqual(history[0]["reason"], "抄表错误")

    def test_berth_approved_with_snapshot_immune_to_backfill(self):
        self.add_tides([(4, 1.0), (5, 0.8), (6, 1.2), (7, 1.5), (8, 1.8)])
        record = self.confirmed_record()
        record = self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        check = record["payload"]["tide_check"]
        self.assertEqual(check["min_tide_m"], 0.8)
        self.assertEqual(check["min_tide_hour"], 5)
        self.assertEqual(check["margin_m"], 2.0)
        self.assertTrue(check["ok"])
        obs = [t for t in self.service.list_tides(DUTY, berth="B12") if t["tide_hour"] == 5][0]
        self.service.correct_tide(DUTY, obs["id"], {"height_m": -3.0, "reason": "补录修正"})
        historical = self.service.get_record(CONTROLLER, record["id"])
        self.assertEqual(historical["payload"]["tide_check"]["min_tide_m"], 0.8)
        self.assertEqual(historical["payload"]["tide_check"]["margin_m"], 2.0)

    def test_berth_rejected_reports_shortfall_and_earliest(self):
        self.add_tides([(4, -0.5), (5, -0.9), (6, -0.8), (7, -0.6), (8, 0.0), (9, 0.5)])
        record = self.confirmed_record()
        with self.assertRaises(BerthingRejected) as ctx:
            self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        exc = ctx.exception
        self.assertEqual(exc.details["shortfall_m"], 0.2)
        self.assertEqual(exc.details["earliest_berth_hour"], 7)
        self.assertIn("还差", str(exc))

    def test_berth_rejected_without_any_window(self):
        self.add_tides([(hour, -5.0) for hour in range(24)])
        record = self.confirmed_record()
        with self.assertRaises(BerthingRejected) as ctx:
            self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        self.assertIsNone(ctx.exception.details["earliest_berth_hour"])

    def test_berth_requires_tide_data(self):
        record = self.confirmed_record()
        with self.assertRaises(ValidationError):
            self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})


class TideRulesTest(unittest.TestCase):
    def test_window_wraps_midnight(self):
        rules = DomainRules()
        payload = {"berth_depth_m": 11.5, "eta_hour": 0}
        tides = [{"tide_hour": 22, "height_m": 0.4}, {"tide_hour": 1, "height_m": 0.2}]
        decision = rules.evaluate_berthing(payload, 10.3, tides)
        self.assertEqual(decision["min_tide_m"], 0.2)
        self.assertEqual(decision["min_tide_hour"], 1)
        self.assertTrue(decision["ok"])
