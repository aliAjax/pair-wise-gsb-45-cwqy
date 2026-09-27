"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
SAFETY_MARGIN_M = 0.5
TIDE_WINDOW_HOURS = 2
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        all_roles.add('duty_officer')
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

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

    def validate_tide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "berth": text(payload, "berth"),
            "tide_hour": number(payload, "tide_hour", -2, 26),
            "tide_height_m": number(payload, "tide_height_m", -10, 15),
        }

    @staticmethod
    def _interp_height(entries: list, hour: float) -> float:
        """按相邻潮时线性插值；落在潮时覆盖范围外返回None。"""
        ordered = sorted(entries, key=lambda item: item["tide_hour"])
        if hour < ordered[0]["tide_hour"] or hour > ordered[-1]["tide_hour"]:
            return None
        for left, right in zip(ordered, ordered[1:]):
            h0, h1 = float(left["tide_hour"]), float(right["tide_hour"])
            if h0 <= hour <= h1:
                if h1 == h0:
                    return float(left["tide_height_m"])
                ratio = (hour - h0) / (h1 - h0)
                return float(left["tide_height_m"]) + ratio * (float(right["tide_height_m"]) - float(left["tide_height_m"]))
        return float(ordered[-1]["tide_height_m"])

    def assess_berthing(self, berth_depth: float, actual_draft: float, eta_hour: float, tide_entries: list) -> Dict[str, Any]:
        """预计到达前后两小时内取最低潮位校核实际吃水，并求最早可靠泊时刻。

        水深 = 泊位基准水深 + 潮高；安全余量 = 水深 - 实际吃水，要求 >= 0.5m。
        窗口为[eta-2, eta+2]，窗口内潮位曲线由相邻潮时线性插值得到。
        最早可靠泊时刻从ETA起沿时间轴寻找首个满足余量要求的点，
        不向已有潮时数据之外外推。
        """
        window_start = float(eta_hour) - TIDE_WINDOW_HOURS
        window_end = float(eta_hour) + TIDE_WINDOW_HOURS
        ordered = sorted(tide_entries, key=lambda item: item["tide_hour"])
        if not ordered:
            raise ValidationError("该泊位ETA前后两小时缺少潮位数据，无法校核")
        # 分段线性曲线的最低值只可能出现在窗口内潮时点或窗口边界上
        candidates = {float(item["tide_hour"]) for item in ordered
                      if window_start <= float(item["tide_hour"]) <= window_end}
        coverage_start = float(ordered[0]["tide_hour"])
        coverage_end = float(ordered[-1]["tide_hour"])
        if coverage_start <= window_start <= coverage_end:
            candidates.add(window_start)
        if coverage_start <= window_end <= coverage_end:
            candidates.add(window_end)
        min_height = None
        for hour in candidates:
            height = self._interp_height(ordered, hour)
            if height is not None and (min_height is None or height < min_height):
                min_height = height
        observed = [
            {"tide_entry_id": item["id"], "tide_hour": round(float(item["tide_hour"]), 2),
             "tide_height_m": round(float(item["tide_height_m"]), 2)}
            for item in ordered if window_start <= float(item["tide_hour"]) <= window_end
        ]
        if min_height is None:
            raise ValidationError("该泊位ETA前后两小时缺少潮位数据，无法校核")
        depth = round(float(berth_depth) + min_height, 2)
        margin = round(depth - float(actual_draft), 2)
        feasible = margin >= SAFETY_MARGIN_M
        return {
            "feasible": feasible,
            "actual_draft_m": round(float(actual_draft), 2),
            "berth_depth_m": round(float(berth_depth), 2),
            "window_start_hour": round(window_start, 2),
            "window_end_hour": round(window_end, 2),
            "min_tide_height_m": round(min_height, 2),
            "min_water_depth_m": depth,
            "safety_margin_m": margin,
            "shortage_m": round(max(0.0, SAFETY_MARGIN_M - margin), 2),
            "earliest_berth_hour": None if feasible else self._earliest_berth_hour(ordered, float(eta_hour), float(berth_depth), float(actual_draft)),
            "observed_tides": observed,
        }

    def _earliest_berth_hour(self, ordered: list, eta_hour: float, berth_depth: float, actual_draft: float) -> Any:
        need = actual_draft + SAFETY_MARGIN_M - berth_depth
        if not ordered:
            return None
        data_start = float(ordered[0]["tide_hour"])
        if data_start > eta_hour:
            return None
        if len(ordered) == 1:
            only = ordered[0]
            height = float(only["tide_height_m"])
            return round(max(data_start, eta_hour), 2) if height + 1e-9 >= need else None
        # 在分段线性曲线上求 t >= eta_hour 且潮高 >= need 的最早时刻。
        # 下降段若不能覆盖到段末，船刚靠泊就会失水深，不作为可等待时刻，跳过；
        # 上升段以与need的交点为最早可行时刻。
        for left, right in zip(ordered, ordered[1:]):
            h0, h1 = float(left["tide_hour"]), float(right["tide_hour"])
            if h1 <= eta_hour:
                continue
            v0, v1 = float(left["tide_height_m"]), float(right["tide_height_m"])
            if v0 >= need and v1 >= need:
                return round(max(h0, eta_hour), 2)
            if v0 < need <= v1:
                cross = h1 if v0 == v1 else h0 + (need - v0) / (v1 - v0) * (h1 - h0)
                candidate = round(max(cross, eta_hour), 2)
                if candidate >= eta_hour and h1 > eta_hour:
                    return candidate
        return None

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], assessment: Dict[str, Any] = None) -> Tuple[str, Dict[str, Any], str]:
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
            if assessment is not None:
                if abs(float(assessment.get("actual_draft_m", -1)) - actual) > 1e-6:
                    raise ValidationError("潮位校核与实际吃水不一致，请重新校核")
                if not assessment.get("feasible"):
                    raise ValidationError(
                        "预计到达前后两小时潮位导致安全余量不足0.5米，还差%s米" % assessment.get("shortage_m"),
                        details={"berthing_assessment": assessment},
                    )
            elif float(p["berth_depth_m"]) - actual < SAFETY_MARGIN_M:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            if assessment is not None:
                # 校核快照固化在记录上，后续补录/修正潮位不影响本航次
                changes["berthing_assessment"] = assessment
            summary = "船舶已靠泊" if assessment is None else "船舶已靠泊（已通过潮位校核）"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
