"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, BerthingRejected, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TIDE_ROLES = {'duty_officer'}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}
SAFETY_MARGIN_M = 0.5
TIDE_WINDOW_HOURS = 2


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(TIDE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_enter_tide(self, role: str) -> bool:
        return role == "admin" or role in TIDE_ROLES

    def validate_tide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        berth = text(p, "berth")
        tide_hour = integer(p, "tide_hour", 0, 23)
        height_m = number(p, "height_m", -10, 20)
        return {"berth": berth, "tide_hour": tide_hour, "height_m": round(height_m, 2)}

    def validate_tide_correction(self, payload: Dict[str, Any]) -> Tuple[float, str]:
        p = dict(payload or {})
        height_m = number(p, "height_m", -10, 20)
        reason = optional_text(p, "reason")
        return round(height_m, 2), reason

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def evaluate_berthing(self, payload: Dict[str, Any], actual_draft_m: float, observations: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """取预计到达前后两小时内的最低潮位，叠加基准水深后与实际吃水比较。"""
        berth_depth = float(payload["berth_depth_m"])
        eta = int(payload["eta_hour"])
        actual = float(actual_draft_m)
        window = sorted({(eta + offset) % 24 for offset in range(-TIDE_WINDOW_HOURS, TIDE_WINDOW_HOURS + 1)})
        by_hour: Dict[int, float] = {}
        for obs in observations or []:
            by_hour[int(obs["tide_hour"])] = float(obs["height_m"])
        window_heights = [(hour, by_hour[hour]) for hour in window if hour in by_hour]
        if not window_heights:
            raise ValidationError("预计到达前后两小时缺少潮位数据，无法审批靠泊")
        min_hour, min_tide = min(window_heights, key=lambda item: (item[1], item[0]))
        available = round(berth_depth + min_tide, 2)
        margin = round(available - actual, 2)
        decision: Dict[str, Any] = {
            "window_hours": window,
            "observed_hours": sorted(by_hour),
            "min_tide_hour": min_hour,
            "min_tide_m": round(min_tide, 2),
            "available_depth_m": available,
            "actual_draft_m": round(actual, 2),
            "required_margin_m": SAFETY_MARGIN_M,
            "margin_m": margin,
            "ok": margin >= SAFETY_MARGIN_M,
        }
        if not decision["ok"]:
            decision["shortfall_m"] = round(SAFETY_MARGIN_M - margin, 2)
            decision["earliest_berth_hour"] = self._earliest_berth_hour(berth_depth, actual, by_hour, eta)
        return decision

    def _earliest_berth_hour(self, berth_depth: float, actual_draft_m: float, by_hour: Dict[int, float], eta: int) -> Optional[int]:
        """自预计到达时刻起向后搜索首个满足安全余量的整点，无则返回None。"""
        for offset in range(24):
            hour = (eta + offset) % 24
            height = by_hour.get(hour)
            if height is not None and round(berth_depth + height - actual_draft_m, 2) >= SAFETY_MARGIN_M:
                return hour
        return None

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], tides: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            decision = self.evaluate_berthing(p, actual, tides)
            if not decision["ok"]:
                message = "安全余量不足半米，还差%.2f米" % decision["shortfall_m"]
                earliest = decision["earliest_berth_hour"]
                if earliest is None:
                    message += "，未来24小时内暂无可靠泊窗口"
                else:
                    message += "，最早可靠泊时刻为%s时" % earliest
                raise BerthingRejected(message, details=decision)
            changes["actual_draft_m"] = actual
            changes["tide_check"] = decision
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
