import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'vessel': 'HaiYun', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': '', 'berth': 'B12'}
DUTY = Actor('duty-li', 'duty_officer')
CTRL = Actor('ctrl-wang', 'port_controller')


class TideBoardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_same_berth_and_hour_rejected(self):
        created = self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': 6, 'tide_height_m': 1.2})
        self.assertEqual(created['tide_height_m'], 1.2)
        with self.assertRaises(Conflict) as ctx:
            self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': 6, 'tide_height_m': 1.4})
        self.assertIn('已有潮位数据', str(ctx.exception))
        # 不同泊位、不同潮时不受影响
        self.service.add_tide(DUTY, {'berth': 'B13', 'tide_hour': 6, 'tide_height_m': 1.4})
        self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': 6.5, 'tide_height_m': 1.4})

    def test_revision_keeps_before_and_after(self):
        entry = self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': 6, 'tide_height_m': 1.2})
        revised = self.service.revise_tide(DUTY, entry['id'], {'tide_height_m': 0.8, 'reason': '潮位站订正'})
        self.assertEqual(revised['tide_height_m'], 0.8)
        with self.assertRaises(ValidationError):
            self.service.revise_tide(DUTY, entry['id'], {'tide_height_m': 0.8, 'reason': '重复修正'})
        history = self.service.tide_history(DUTY, tide_id=entry['id'])
        self.assertEqual([r['new_height_m'] for r in history], [0.8, 1.2])
        self.assertIsNone(history[-1]['old_height_m'])
        self.assertEqual(history[0]['old_height_m'], 1.2)
        self.assertEqual(history[0]['reason'], '潮位站订正')

    def test_duty_officer_role_and_outsider_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.add_tide(Actor('x', 'outsider'), {'berth': 'B12', 'tide_hour': 6, 'tide_height_m': 1.2})
        self.service.add_tide(CTRL, {'berth': 'B12', 'tide_hour': 6, 'tide_height_m': 1.2})


class BerthingTideAssessmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _confirm(self, data=CREATE_DATA, ref='VOY-1'):
        record = self.service.create(CTRL, ref, data)
        return self.service.act(CTRL, record['id'], record['version'], 'confirm', {'pilot_id': 'P-01'})

    def test_missing_tide_data_blocks_berth(self):
        record = self._confirm()
        with self.assertRaises(ValidationError) as ctx:
            self.service.act(CTRL, record['id'], record['version'], 'berth', {'actual_draft_m': 10.3})
        self.assertIn('缺少潮位数据', str(ctx.exception))

    def test_low_tide_shortage_and_earliest_berth_hour(self):
        # 深度11.5，实际吃水10.3，需要潮高>= -0.7；低潮-0.3时余量0.9...
        # 取低潮-0.3：11.5-0.3=11.2，余量0.9 -> 可行。改用更深吃水制造不足：
        for hour, height in ((4, -0.3), (6, -0.3), (8, -0.3), (10, 1.2)):
            self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': hour, 'tide_height_m': height})
        record = self._confirm(ref='VOY-2')
        assessment = self.service.assess_berthing(CTRL, record['id'], {'actual_draft_m': 11.2})
        self.assertFalse(assessment['feasible'])
        # 11.5 + (-0.3) = 11.2，余量0.0，差0.5米
        self.assertEqual(assessment['safety_margin_m'], 0.0)
        self.assertEqual(assessment['shortage_m'], 0.5)
        # 需要潮高0.2，在[8,10]段从-0.3升到1.2：交点 8 + (0.2+0.3)/1.5*2 = 8.6667
        self.assertAlmostEqual(assessment['earliest_berth_hour'], 8.67, places=2)

        with self.assertRaises(ValidationError) as ctx:
            self.service.act(CTRL, record['id'], record['version'], 'berth', {'actual_draft_m': 11.2})
        self.assertIn('还差0.5米', str(ctx.exception))
        self.assertIn('berthing_assessment', ctx.exception.details)
        self.assertEqual(ctx.exception.details['berthing_assessment']['shortage_m'], 0.5)

    def test_window_uses_minimum_even_at_edges(self):
        # 窗口[4,8]：端点4处最低，验证插值边界也参与取最低
        for hour, height in ((4, -0.3), (6, 0.0), (8, 0.8)):
            self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': hour, 'tide_height_m': height})
        record = self._confirm(ref='VOY-3')
        assessment = self.service.assess_berthing(CTRL, record['id'], {'actual_draft_m': 11.2})
        self.assertEqual(assessment['min_tide_height_m'], -0.3)
        self.assertEqual(assessment['min_water_depth_m'], 11.2)
        self.assertEqual(assessment['shortage_m'], 0.5)

    def test_feasible_tide_allows_berth_and_records_snapshot(self):
        for hour, height in ((4, 1.0), (6, 0.8), (8, 1.1)):
            self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': hour, 'tide_height_m': height})
        record = self._confirm(ref='VOY-4')
        assessment = self.service.assess_berthing(CTRL, record['id'], {'actual_draft_m': 11.2})
        # 最低0.8 → 水深12.3，余量1.1
        self.assertTrue(assessment['feasible'])
        self.assertEqual(assessment['safety_margin_m'], 1.1)
        self.assertIsNone(assessment['earliest_berth_hour'])
        berthed = self.service.act(CTRL, record['id'], record['version'], 'berth', {'actual_draft_m': 11.2})
        self.assertEqual(berthed['state'], 'berthed')
        self.assertEqual(berthed['payload']['berthing_assessment']['min_tide_height_m'], 0.8)

    def test_no_recovery_within_known_tides_returns_none(self):
        for hour, height in ((4, -0.5), (6, -0.5), (8, -0.5)):
            self.service.add_tide(DUTY, {'berth': 'B12', 'tide_hour': hour, 'tide_height_m': height})
        record = self._confirm(ref='VOY-5')
        assessment = self.service.assess_berthing(CTRL, record['id'], {'actual_draft_m': 11.2})
        self.assertFalse(assessment['feasible'])
        self.assertIsNone(assessment['earliest_berth_hour'])
