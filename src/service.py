"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, number, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 潮位看板 ----

    def _ensure_tide_role(self, actor: Actor) -> None:
        if actor.role not in {"admin", "port_controller", "duty_officer"}:
            raise PermissionDenied("角色无权管理潮位")

    def add_tide(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_tide_role(actor)
        entry = self.rules.validate_tide(payload or {})
        # 同一泊位同一潮时重复录入直接提示已有数据
        existing = self.repository.get_tide_entry(entry["berth"], entry["tide_hour"])
        if existing is not None:
            raise Conflict("泊位%s潮时%s已有潮位数据（记录#%s，潮高%s米），如需调整请使用修正功能"
                           % (entry["berth"], _fmt_hour(entry["tide_hour"]), existing["id"], existing["tide_height_m"]))
        return self.repository.create_tide_entry(
            entry["berth"], entry["tide_hour"], entry["tide_height_m"], actor.user_id
        )

    def list_tides(self, actor: Actor, berth: str = None, limit: int = 500) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_tides(berth=berth, limit=limit)

    def revise_tide(self, actor: Actor, tide_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_tide_role(actor)
        new_height = number(payload or {}, "tide_height_m", -10, 15)
        reason = text(payload or {}, "reason")
        return self.repository.revise_tide(int(tide_id), new_height, reason, actor.user_id)

    def tide_history(self, actor: Actor, tide_id: int = None, berth: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.tide_revisions(tide_id=tide_id, berth=berth)

    # ---- 靠泊潮位校核 ----

    def assess_berthing(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        actual = number(data or {}, "actual_draft_m", 0)
        payload = record["payload"]
        tides = self.repository.tides_for_berth(payload["berth"])
        assessment = self.rules.assess_berthing(
            berth_depth=float(payload["berth_depth_m"]),
            actual_draft=actual,
            eta_hour=float(payload["eta_hour"]),
            tide_entries=tides,
        )
        assessment["berth"] = payload["berth"]
        assessment["record_id"] = record_id
        return assessment

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if int(record["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        assessment = None
        if action == "berth":
            # 靠泊审批：取ETA前后两小时最低潮位与实际吃水比较
            actual = number(data or {}, "actual_draft_m", 0)
            payload = record["payload"]
            tides = self.repository.tides_for_berth(payload["berth"])
            assessment = self.rules.assess_berthing(
                berth_depth=float(payload["berth_depth_m"]),
                actual_draft=actual,
                eta_hour=float(payload["eta_hour"]),
                tide_entries=tides,
            )
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, assessment)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state,
                     "berthing_assessment": assessment},
        )


def _fmt_hour(value: float) -> str:
    hour = int(value)
    minute = int(round((float(value) - hour) * 60))
    if minute == 60:
        hour += 1
        minute = 0
    return "%02d:%02d" % (hour, minute)
