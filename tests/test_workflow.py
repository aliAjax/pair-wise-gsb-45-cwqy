import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'vessel': 'HaiYun', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': '', 'berth': 'B12'}
FLOW = [('confirm', 'port_controller', {'pilot_id': 'P-01'}, 'confirmed'), ('berth', 'port_controller', {'actual_draft_m': 10.3}, 'berthed'), ('depart', 'port_controller', {'cargo_operation_complete': True}, 'departed')]


def seed_ok_tides(service):
    """ETA=6 的窗口[4,8]内潮高恒为1.0米：11.5+1.0-10.3=2.2，余量充足。"""
    duty = Actor('duty-zhang', 'duty_officer')
    for hour in (4, 6, 8):
        service.add_tide(duty, {'berth': 'B12', 'tide_hour': hour, 'tide_height_m': 1.0})


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        seed_ok_tides(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "port_controller"), "VOY-21001", CREATE_DATA)
        self.assertEqual(record["state"], "draft")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "port_controller"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])

    def test_berth_snapshot_is_frozen_against_later_tide_changes(self):
        # 历史航次：靠泊时固化校核快照，事后补录/修正潮位不影响已靠泊记录
        record = self.service.create(Actor("creator", "port_controller"), "VOY-21001", CREATE_DATA)
        record = self.service.act(Actor("operator", "port_controller"), record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.service.act(Actor("operator", "port_controller"), record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        frozen = record["payload"]["berthing_assessment"]
        self.assertTrue(frozen["feasible"])
        self.assertEqual(frozen["min_tide_height_m"], 1.0)

        # 事后把同一泊位潮高全部改小并补录，历史航次记录保持不变
        tides = self.service.list_tides(Actor("duty-zhang", "duty_officer"), berth="B12")
        for tide in tides:
            self.service.revise_tide(Actor("duty-zhang", "duty_officer"), tide["id"],
                                     {"tide_height_m": -2.0, "reason": "潮位站订正"})
        self.service.add_tide(Actor("duty-zhang", "duty_officer"),
                              {"berth": "B12", "tide_hour": 5, "tide_height_m": -3.0})
        historical = self.service.get_record(Actor("creator", "port_controller"), record["id"])
        self.assertEqual(historical["payload"]["berthing_assessment"], frozen)
        self.assertEqual(historical["state"], "berthed")
        timeline = self.service.timeline(Actor("creator", "port_controller"), record["id"])
        berth_event = next(event for event in timeline if event["action"] == "berth")
        self.assertTrue(berth_event["details"]["berthing_assessment"]["feasible"])
