"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_hour(value: float) -> str:
    hour = int(value)
    minute = int(round((float(value) - hour) * 60))
    if minute == 60:
        hour += 1
        minute = 0
    return "%02d:%02d" % (hour, minute)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tide_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    berth TEXT NOT NULL,
                    tide_hour REAL NOT NULL,
                    tide_height_m REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(berth, tide_hour)
                );
                CREATE TABLE IF NOT EXISTS tide_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tide_entry_id INTEGER NOT NULL REFERENCES tide_entries(id) ON DELETE CASCADE,
                    berth TEXT NOT NULL,
                    tide_hour REAL NOT NULL,
                    old_height_m REAL,
                    new_height_m REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    revised_by TEXT NOT NULL,
                    revised_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_tide_berth ON tide_entries(berth, tide_hour);
                CREATE INDEX IF NOT EXISTS idx_tide_revision_entry ON tide_revisions(tide_entry_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    @staticmethod
    def _tide_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["tide_hour"] = round(float(item["tide_hour"]), 2)
        item["tide_height_m"] = round(float(item["tide_height_m"]), 2)
        return item

    def create_tide_entry(self, berth: str, tide_hour: float, tide_height: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tide_entries(berth,tide_hour,tide_height_m,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (berth, tide_hour, tide_height, actor_id, actor_id, now, now),
                )
                tide_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO tide_revisions(tide_entry_id,berth,tide_hour,old_height_m,new_height_m,reason,revised_by,revised_at) VALUES(?,?,?,?,?,?,?,?)",
                    (tide_id, berth, tide_hour, None, tide_height, "初始录入", actor_id, now),
                )
                row = connection.execute("SELECT * FROM tide_entries WHERE id=?", (tide_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            existing = self.get_tide_entry(berth, tide_hour)
            raise Conflict("泊位%s潮时%s已有潮位数据（记录#%s，潮高%s米），请使用修正功能"
                           % (berth, _format_hour(tide_hour), existing["id"], existing["tide_height_m"])) from exc
        return self._tide_row(row)

    def get_tide_entry(self, berth: str, tide_hour: float) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tide_entries WHERE berth=? AND ABS(tide_hour-?) < 0.001",
                (berth, tide_hour),
            ).fetchone()
        return self._tide_row(row) if row is not None else None

    def get_tide_entry_by_id(self, tide_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tide_entries WHERE id=?", (tide_id,)).fetchone()
        if row is None:
            raise NotFound("潮位记录不存在")
        return self._tide_row(row)

    def list_tides(self, berth: str = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        with self._connect() as connection:
            if berth:
                rows = connection.execute(
                    "SELECT * FROM tide_entries WHERE berth=? ORDER BY berth, tide_hour LIMIT ?",
                    (berth, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tide_entries ORDER BY berth, tide_hour LIMIT ?", (limit,)
                ).fetchall()
        return [self._tide_row(row) for row in rows]

    def tides_for_berth(self, berth: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tide_entries WHERE berth=? ORDER BY tide_hour", (berth,)
            ).fetchall()
        return [self._tide_row(row) for row in rows]

    def revise_tide(self, tide_id: int, new_height: float, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tide_entries WHERE id=?", (tide_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("潮位记录不存在")
            old_height = float(row["tide_height_m"])
            if abs(old_height - float(new_height)) < 1e-9:
                connection.rollback()
                raise ValidationError("修正潮高与当前潮高相同，无需修正")
            connection.execute(
                "UPDATE tide_entries SET tide_height_m=?,updated_by=?,updated_at=? WHERE id=?",
                (new_height, actor_id, now, tide_id),
            )
            # 修正历史只追加，不覆盖，修改前后潮高都保留
            connection.execute(
                "INSERT INTO tide_revisions(tide_entry_id,berth,tide_hour,old_height_m,new_height_m,reason,revised_by,revised_at) VALUES(?,?,?,?,?,?,?,?)",
                (tide_id, row["berth"], float(row["tide_hour"]), old_height, new_height, reason, actor_id, now),
            )
            result = connection.execute("SELECT * FROM tide_entries WHERE id=?", (tide_id,)).fetchone()
            connection.commit()
        return self._tide_row(result)

    def tide_revisions(self, tide_id: int = None, berth: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        query = ("SELECT r.* FROM tide_revisions r "
                 "JOIN tide_entries e ON e.id = r.tide_entry_id WHERE 1=1")
        params: List[Any] = []
        if tide_id is not None:
            query += " AND r.tide_entry_id=?"
            params.append(tide_id)
        if berth:
            query += " AND r.berth=?"
            params.append(berth)
        query += " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tide_hour"] = round(float(item["tide_hour"]), 2)
            item["new_height_m"] = round(float(item["new_height_m"]), 2)
            if item["old_height_m"] is not None:
                item["old_height_m"] = round(float(item["old_height_m"]), 2)
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
