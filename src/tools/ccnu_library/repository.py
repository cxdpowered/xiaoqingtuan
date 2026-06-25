"""CCNU 图书馆功能组的 SQLite 读写（表 DDL 见 src/storage/db.py §6）。

只做数据存取，不含业务编排。所有写入都打 ``created_at`` / ``updated_at``，
person_id 分区。JSON 字段（time_slots / locations / raw_payload）在边界处编解码。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from src.storage.db import Database, new_id, now_iso


def _dumps(value: Any) -> Optional[str]:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# ---- user settings --------------------------------------------------------
def get_settings(db: Database, person_id: str) -> Optional[dict[str, Any]]:
    row = db.query_one("SELECT * FROM library_user_settings WHERE person_id = ?", (person_id,))
    return dict(row) if row else None


def upsert_settings(db: Database, person_id: str, *, user_key: str, **fields: Any) -> None:
    now = now_iso()
    existing = get_settings(db, person_id)
    cols = {
        "default_library": fields.get("default_library"),
        "default_area_filter": fields.get("default_area_filter"),
        "default_location_id": fields.get("default_location_id"),
        "default_strategy": fields.get("default_strategy", "favorite_first"),
        "enabled": 1 if fields.get("enabled", True) else 0,
    }
    if existing:
        sets = ", ".join(f"{k} = ?" for k in cols)
        db.execute(
            f"UPDATE library_user_settings SET user_key = ?, {sets}, updated_at = ? WHERE person_id = ?",
            (user_key, *cols.values(), now, person_id),
        )
    else:
        db.execute(
            "INSERT INTO library_user_settings (person_id, user_key, enabled, default_library,"
            " default_area_filter, default_location_id, default_strategy, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (person_id, user_key, cols["enabled"], cols["default_library"],
             cols["default_area_filter"], cols["default_location_id"],
             cols["default_strategy"], now, now),
        )


# ---- challenge sessions ---------------------------------------------------
def create_challenge(
    db: Database, *, person_id: str, user_key: str, session_id: str,
    challenge_id: str, challenge_type: str, prompt: Optional[str],
    image_path: Optional[str], image_mime: Optional[str], image_sha256: Optional[str],
    phone_hint: Optional[str], expires_at: Optional[str],
    pending_action: Optional[str] = None, pending_context: Optional[Any] = None,
    status: str = "pending",
) -> str:
    cid = new_id("lch_")
    now = now_iso()
    db.execute(
        "INSERT INTO library_challenge_sessions (id, person_id, user_key, session_id,"
        " challenge_id, challenge_type, pending_action, pending_context, prompt, image_path,"
        " image_mime, image_sha256, phone_hint, status, expires_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, person_id, user_key, session_id, challenge_id, challenge_type, pending_action,
         _dumps(pending_context), prompt, image_path, image_mime, image_sha256, phone_hint,
         status, expires_at, now, now),
    )
    return cid


def pending_to_deliver(db: Database, session_id: str) -> list[dict[str, Any]]:
    """本会话中尚未把验证码图片发给用户的 challenge（status='pending'）。"""
    rows = db.query(
        "SELECT * FROM library_challenge_sessions WHERE session_id = ? AND status = 'pending'"
        " ORDER BY created_at",
        (session_id,),
    )
    return [dict(r) for r in rows]


def awaiting_answer(db: Database, session_id: str) -> Optional[dict[str, Any]]:
    """本会话中正在等待用户输入答案的 challenge（最近一个）。"""
    row = db.query_one(
        "SELECT * FROM library_challenge_sessions WHERE session_id = ? AND status = 'awaiting_answer'"
        " ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    )
    return dict(row) if row else None


def set_challenge_status(db: Database, challenge_row_id: str, status: str) -> None:
    db.execute(
        "UPDATE library_challenge_sessions SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), challenge_row_id),
    )


# ---- booking plans --------------------------------------------------------
def create_plan(db: Database, *, person_id: str, user_key: str, fields: dict[str, Any]) -> str:
    pid = new_id("lpl_")
    now = now_iso()
    db.execute(
        "INSERT INTO library_booking_plans (id, person_id, user_key, session_id, channel,"
        " account_id, title, date_start, date_end, weekdays, time_slots, preferred_locations,"
        " fallback_locations, fallback_time_slots, strategy, auto_cancel_if_cannot_signin,"
        " auto_end_if_away_timeout, require_confirmation_each_booking, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, person_id, user_key, fields.get("session_id"), fields.get("channel"),
         fields.get("account_id"), fields.get("title"), fields["date_start"], fields["date_end"],
         _dumps(fields.get("weekdays")), _dumps(fields["time_slots"]),
         _dumps(fields.get("preferred_locations")), _dumps(fields.get("fallback_locations")),
         _dumps(fields.get("fallback_time_slots")), fields["strategy"],
         1 if fields.get("auto_cancel_if_cannot_signin") else 0,
         1 if fields.get("auto_end_if_away_timeout") else 0,
         1 if fields.get("require_confirmation_each_booking") else 0,
         fields.get("status", "draft"), now, now),
    )
    return pid


def get_plan(db: Database, plan_id: str) -> Optional[dict[str, Any]]:
    row = db.query_one("SELECT * FROM library_booking_plans WHERE id = ?", (plan_id,))
    return dict(row) if row else None


def list_plans(db: Database, person_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM library_booking_plans WHERE person_id = ? ORDER BY created_at DESC",
        (person_id,),
    )
    return [dict(r) for r in rows]


def active_plans(db: Database) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM library_booking_plans WHERE status = 'active'")
    return [dict(r) for r in rows]


def set_plan_status(db: Database, plan_id: str, status: str) -> None:
    db.execute(
        "UPDATE library_booking_plans SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), plan_id),
    )


# ---- booking jobs ---------------------------------------------------------
def create_job(db: Database, *, plan_id: Optional[str], person_id: str, user_key: str,
               target_date: str, start_time: str, end_time: str, location_id: Optional[str],
               area_filter: Optional[str], strategy: str, run_after: str,
               status: str = "pending") -> str:
    jid = new_id("ljb_")
    now = now_iso()
    db.execute(
        "INSERT INTO library_booking_jobs (id, plan_id, person_id, user_key, target_date,"
        " start_time, end_time, location_id, area_filter, strategy, run_after, status, attempts,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (jid, plan_id, person_id, user_key, target_date, start_time, end_time, location_id,
         area_filter, strategy, run_after, status, now, now),
    )
    return jid


def job_exists(db: Database, *, plan_id: str, target_date: str, start_time: str) -> bool:
    row = db.query_one(
        "SELECT id FROM library_booking_jobs WHERE plan_id = ? AND target_date = ? AND start_time = ?",
        (plan_id, target_date, start_time),
    )
    return row is not None


def due_jobs(db: Database, now: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM library_booking_jobs WHERE status = 'pending' AND run_after <= ?"
        " ORDER BY run_after LIMIT 50",
        (now,),
    )
    return [dict(r) for r in rows]


def update_job(db: Database, job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    if "mcp_result" in fields and not isinstance(fields["mcp_result"], (str, type(None))):
        fields["mcp_result"] = _dumps(fields["mcp_result"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE library_booking_jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


# ---- reservation watches --------------------------------------------------
def create_watch(db: Database, *, person_id: str, user_key: str, fields: dict[str, Any]) -> str:
    wid = new_id("lwa_")
    now = now_iso()
    db.execute(
        "INSERT INTO library_reservation_watches (id, person_id, user_key, plan_id, job_id,"
        " session_id, channel, account_id, external_reservation_id, seat_no, location_path, date,"
        " start_time, end_time, start_at, end_at, sign_in_deadline, away_deadline, status,"
        " raw_status, raw_payload, auto_cancel_if_cannot_signin, auto_end_if_away_timeout,"
        " last_checked_at, next_check_at, next_reminder_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (wid, person_id, user_key, fields.get("plan_id"), fields.get("job_id"),
         fields.get("session_id"), fields.get("channel"), fields.get("account_id"),
         fields.get("external_reservation_id"), fields.get("seat_no"), fields.get("location_path"),
         fields.get("date"), fields.get("start_time"), fields.get("end_time"),
         fields.get("start_at"), fields.get("end_at"), fields.get("sign_in_deadline"),
         fields.get("away_deadline"), fields.get("status", "reserved"), fields.get("raw_status"),
         _dumps(fields.get("raw_payload")),
         1 if fields.get("auto_cancel_if_cannot_signin") else 0,
         1 if fields.get("auto_end_if_away_timeout") else 0,
         fields.get("last_checked_at"), fields.get("next_check_at"), fields.get("next_reminder_at"),
         now, now),
    )
    return wid


def active_watches(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM library_reservation_watches"
        " WHERE status NOT IN ('ended', 'cancelled', 'none')",
    )
    return [dict(r) for r in rows]


def due_watches(db: Database, now: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM library_reservation_watches"
        " WHERE status NOT IN ('ended', 'cancelled', 'none')"
        " AND (next_check_at IS NULL OR next_check_at <= ?)",
        (now,),
    )
    return [dict(r) for r in rows]


def update_watch(db: Database, watch_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    if "raw_payload" in fields and not isinstance(fields["raw_payload"], (str, type(None))):
        fields["raw_payload"] = _dumps(fields["raw_payload"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE library_reservation_watches SET {sets} WHERE id = ?", (*fields.values(), watch_id)
    )


# ---- notifications --------------------------------------------------------
def create_notification(
    db: Database, *, person_id: str, user_key: str, kind: str, message: str, due_at: str,
    session_id: Optional[str] = None, channel: Optional[str] = None,
    account_id: Optional[str] = None, watch_id: Optional[str] = None,
) -> str:
    nid = new_id("lnt_")
    now = now_iso()
    db.execute(
        "INSERT INTO library_notifications (id, person_id, user_key, session_id, channel,"
        " account_id, watch_id, kind, message, due_at, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (nid, person_id, user_key, session_id, channel, account_id, watch_id, kind, message,
         due_at, now, now),
    )
    return nid


def due_notifications(db: Database, now: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM library_notifications WHERE status = 'pending' AND due_at <= ?"
        " ORDER BY due_at LIMIT 50",
        (now,),
    )
    return [dict(r) for r in rows]


def set_notification_status(db: Database, notification_id: str, status: str) -> None:
    db.execute(
        "UPDATE library_notifications SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), notification_id),
    )


def cancel_notifications_for_watch(db: Database, watch_id: str, kinds: Iterable[str]) -> None:
    placeholders = ",".join("?" for _ in kinds)
    if not placeholders:
        return
    db.execute(
        f"UPDATE library_notifications SET status = 'cancelled', updated_at = ?"
        f" WHERE watch_id = ? AND status = 'pending' AND kind IN ({placeholders})",
        (now_iso(), watch_id, *kinds),
    )
