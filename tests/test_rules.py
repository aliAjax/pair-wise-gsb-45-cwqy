import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}
FLOW = [('confirm', 'port_controller', {'pilot_id': 'P-01'}, 'confirmed'), ('berth', 'port_controller', {'actual_draft_m': 10.3}, 'berthed'), ('depart', 'port_controller', {'cargo_operation_complete': True}, 'departed')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["safety_margin_m"], 1.3)
        self.assertTrue(prepared["quay_ok"])
        self.assertEqual(prepared["window_hours"], 12)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["pilot_id"], "P-01")

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["draft_m"] = 12.0
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)

    def test_tide_assessment_earliest_berth_hour(self):
        # ETA=6，窗口[4,8]低潮-0.3导致余量不足；8点后上涨，交点应在8.67
        tides = [{"id": i + 1, "tide_hour": h, "tide_height_m": v}
                 for i, (h, v) in enumerate(((4, -0.3), (6, 1.0), (8, -0.3), (10, 1.2)))]
        result = self.rules.assess_berthing(
            berth_depth=11.5, actual_draft=11.2, eta_hour=6, tide_entries=tides)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["shortage_m"], 0.5)
        self.assertAlmostEqual(result["earliest_berth_hour"], 8.67, places=2)
        # 窗口缺潮位数据时直接报错
        with self.assertRaises(ValidationError):
            self.rules.assess_berthing(11.5, 11.2, 6, [])
