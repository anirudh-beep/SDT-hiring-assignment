"""HROne Employee Attendance & Analytics API, contract v2.0."""
from __future__ import annotations

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ------------------------------ Time and rules ------------------------------
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def parse_epoch_ms(value: Any, *, optional: bool = False) -> Optional[datetime]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch-millisecond value")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError(f"must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise ValueError("epoch milliseconds are out of range")


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_utc(value).timestamp() * 1000)


def round_half_up(value: Any, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_day(value: str) -> date:
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD")
    if d.isoformat() != value:
        raise HTTPException(422, "Date must use YYYY-MM-DD")
    return d


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must use YYYY-MM")
    y, m = map(int, value.split("-"))
    start = date(y, m, 1)
    return start, date(y, m, calendar.monthrange(y, m)[1])


def parse_clock(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "Shift times must use HH:MM")
    h, m = map(int, value.split(":"))
    return time(h, m)


def shift_bounds(day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    d = parse_day(day)
    st, en = parse_clock(shift_start), parse_clock(shift_end)
    start_local = datetime.combine(d, st, IST)
    end_date = d + timedelta(days=1) if en <= st else d
    end_local = datetime.combine(end_date, en, IST)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_utc(punch_in).astimezone(IST)
    st, en = parse_clock(shift_start), parse_clock(shift_end)
    # An overnight-shift punch-in before the end time belongs to the prior shift day.
    if en <= st and local.time().replace(tzinfo=None) < en:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late(punch_in: Optional[datetime], day: str, emp: dict) -> int:
    if punch_in is None:
        return 0
    start, _ = shift_bounds(day, emp["shift_start"], emp["shift_end"])
    seconds = int((truncate_utc(punch_in) - start).total_seconds())
    # Strictly more than 10 minutes; once late, count whole minutes from shift start.
    return seconds // 60 if seconds > 600 else 0


def compute_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((truncate_utc(punch_out) - truncate_utc(punch_in)).total_seconds())
    if seconds <= 0:
        raise HTTPException(422, "punch_out must be after punch_in")
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: Optional[datetime], day: str, emp: dict) -> int:
    if punch_out is None:
        return 0
    _, end = shift_bounds(day, emp["shift_start"], emp["shift_end"])
    seconds = int((truncate_utc(punch_out) - end).total_seconds())
    mins = seconds // 60
    return int(mins) if mins >= 30 else 0


def derived(day: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict) -> dict:
    if status not in PRESENCE:
        if punch_in is not None or punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE must have null punch times")
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        raise HTTPException(422, "Presence statuses require punch_in")
    if attendance_day(punch_in, emp["shift_start"], emp["shift_end"]) != day:
        raise HTTPException(422, "punch_in must belong to the attendance date")
    if punch_out is not None:
        delta = (truncate_utc(punch_out) - truncate_utc(punch_in)).total_seconds()
        if delta <= 0 or delta > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = compute_hours(punch_in, punch_out)
    return {"work_hours": hours, "late_minutes": compute_late(punch_in, day, emp),
            "overtime_minutes": compute_overtime(punch_out, day, emp),
            "half_day": bool(hours is not None and hours < 4.50)}


def public_employee(doc: dict) -> dict:
    return {k: (to_ms(v) if k == "created_at" else v) for k, v in doc.items()
            if k in {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}}


def public_attendance(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    out["history"] = []
    for entry in doc.get("history", []) or []:
        changes = {}
        for field, diff in (entry.get("changes") or {}).items():
            changes[field] = {side: (to_ms(value) if field in {"punch_in", "punch_out"} else value)
                              for side, value in diff.items()}
        out["history"].append({"at": to_ms(entry.get("at")), "by": entry.get("by"),
                               "reason": entry.get("reason"), "changes": changes})
    out["punch_in"] = to_ms(doc.get("punch_in"))
    out["punch_out"] = to_ms(doc.get("punch_out"))
    out["late_minutes"] = int(doc.get("late_minutes", 0) or 0)
    out["overtime_minutes"] = int(doc.get("overtime_minutes", 0) or 0)
    out["half_day"] = bool(doc.get("half_day", False))
    return out


def require_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp


def page_args(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    return (page - 1) * page_size, page_size

# ------------------------------- Pydantic ----------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("shift_start", "shift_end")
    @classmethod
    def clock_format(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v): raise ValueError("must use HH:MM")
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None: parse_epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def presence_status(cls, v):
        if v not in PRESENCE: raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutRequest(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None: parse_epoch_ms(v)
        return v


class RegularizeRequest(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is not None and v not in STATUSES: raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def valid_epoch(cls, v):
        if v is not None: parse_epoch_ms(v)
        return v

    @model_validator(mode="after")
    def reject_explicit_null_instants(self):
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in must be epoch milliseconds when supplied")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out must be epoch milliseconds when supplied")
        return self

# -------------------------------- Indexes ----------------------------------
def ensure_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_emp_date_status")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")


@app.on_event("startup")
def startup():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        # Import/startup remains possible during outages; /health reports unavailable.
        pass

# -------------------------------- Health -----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB connection failed")

# ------------------------------- Employees ---------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip, size = page_args(page, page_size)
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    cursor = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(size)
    return {"items": [public_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

# -------------------------------- Punch-in ---------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = require_employee(body.emp_code)
    instant = parse_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    instant = truncate_utc(instant)
    day = attendance_day(instant, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": instant, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late(instant, day, emp), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance record already exists for this employee and date")
    return public_attendance(doc)

# -------------------------------- Punch-out --------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = require_employee(body.emp_code)
    instant = truncate_utc(parse_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc())
    # Candidate selection is indexed; conditional atomic update ensures only one concurrent request wins.
    candidate = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant}, "punch_out": None,
                                     "status": {"$in": list(PRESENCE)}}, sort=[("punch_in", DESCENDING)])
    if candidate is None:
        already = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant}, "status": {"$in": list(PRESENCE)}, "punch_out": {"$ne": None}}, sort=[("punch_in", DESCENDING)])
        if already:
            raise HTTPException(409, "This attendance record is already punched out")
        raise HTTPException(404, "No open punch-in found")
    pin = truncate_utc(candidate["punch_in"])
    delta = (instant - pin).total_seconds()
    if delta <= 0 or delta > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and no more than 24 hours later")
    vals = derived(candidate["date"], candidate["status"], pin, instant, emp)
    updated = attendance.find_one_and_update(
        {"emp_code": body.emp_code, "date": candidate["date"], "punch_in": pin, "punch_out": None},
        {"$set": {"punch_out": instant, **vals}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "This attendance record was punched out by another request")
    return public_attendance(updated)

# ------------------------------ Attendance list ----------------------------
@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip, size = page_args(page, page_size)
    if date_from is not None: parse_day(date_from)
    if date_to is not None: parse_day(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in STATUSES: raise HTTPException(422, "Invalid status")
    filt: dict[str, Any] = {}
    if emp_code is not None: filt["emp_code"] = emp_code
    if status is not None: filt["status"] = status
    date_filter = {}
    if date_from: date_filter["$gte"] = date_from
    if date_to: date_filter["$lte"] = date_to
    if date_filter: filt["date"] = date_filter
    total = attendance.count_documents(filt)
    cur = attendance.find(filt, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(size)
    return {"items": [public_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}

# ----------------------------- Manual correction ---------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    parse_day(date)
    emp = require_employee(emp_code)
    current = attendance.find_one({"emp_code": emp_code, "date": date})
    if current is None: raise HTTPException(404, "Attendance record not found")
    supplied = body.model_fields_set - {"reason", "regularized_by"}
    if not supplied: raise HTTPException(422, "No attendance fields supplied")
    new_status = body.status if "status" in supplied else current["status"]
    pin = parse_epoch_ms(body.punch_in) if "punch_in" in supplied and body.punch_in is not None else (None if "punch_in" in supplied else current.get("punch_in"))
    pout = parse_epoch_ms(body.punch_out) if "punch_out" in supplied and body.punch_out is not None else (None if "punch_out" in supplied else current.get("punch_out"))
    if new_status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None):
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        pin = pout = None
    vals = derived(date, new_status, pin, pout, emp)
    before = {"status": current.get("status"), "punch_in": current.get("punch_in"), "punch_out": current.get("punch_out"),
              "work_hours": current.get("work_hours"), "late_minutes": int(current.get("late_minutes", 0) or 0),
              "overtime_minutes": int(current.get("overtime_minutes", 0) or 0), "half_day": bool(current.get("half_day", False))}
    after = {"status": new_status, "punch_in": pin, "punch_out": pout, **vals}
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if not changes: raise HTTPException(422, "Request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic compare-and-set protects history from concurrent lost updates.
    filt = {"emp_code": emp_code, "date": date, "history": current.get("history", [])}
    for key in ("status", "punch_in", "punch_out"):
        filt[key] = current.get(key)
    updated = attendance.find_one_and_update(filt, {"$set": after, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "Record changed concurrently; reload and retry")
    return public_attendance(updated)

# ------------------------------- Aggregation -------------------------------
def month_query(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def presence_day_expr():
    return {"$cond": [
        {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$eq": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
        0,
        {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = require_employee(emp_code)
    start_s, end_s, start_d, end_d = month_query(month)
    joined = parse_day(emp["joined_on"])
    first_workday = max(start_d, joined)
    if first_workday > end_d:
        working_days = 0
    else:
        days = (end_d - first_workday).days + 1
        full_weeks, rem = divmod(days, 7)
        working_days = full_weeks * 5 + sum(1 for i in range(rem) if (first_workday.weekday() + i) % 7 < 5)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                  "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]},
                  "_half": {"$ifNull": ["$half_day", False]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": ["$_weekday", [2, 3, 4, 5, 6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}}
    ]
    result = list(attendance.aggregate(pipeline))
    stats = result[0] if result else {}
    present_days = round_half_up(stats.get("present_days", 0), 2)
    pct = None if working_days == 0 else round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 4)
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": present_days, "leave_days": int(stats.get("leave_days", 0)),
            "late_count": int(stats.get("late_count", 0)), "total_late_minutes": int(stats.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)), "attendance_pct": pct}

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start_s, end_s, start_d, end_d = month_query(month)
    # Start from employees to include employees with zero attendance records (R9).
    emp_match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None: emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
            "present_days": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$let": {"vars": {"valid": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$valid"}, 0]}, {"$avg": "$$valid"}, None]}}}}},
        {"$sort": {"department": 1}}
    ]
    # Date filter ensures no department with headcount 0 for the month is returned.
    rows = list(employees.aggregate(pipeline))
    items = []
    for row in rows:
        if row["headcount"] <= 0: continue
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = None if row.get("avg_work_hours") is None else round_half_up(row["avg_work_hours"], 2)
        for k in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"): row[k] = int(row.get(k, 0) or 0)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_query(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    items = list(attendance.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_day(from_date), parse_day(to_date)
    if end < start: raise HTTPException(422, "to must be on or after from")
    count = (end - start).days + 1
    if count > 92: raise HTTPException(422, "Date range must not exceed 92 days")
    if employees.count_documents({"department": department}) == 0: raise HTTPException(404, "Unknown department")
    # Generate the calendar in MongoDB using $range; do not loop over days in Python.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {"$project": {"_id": 0, "days": {"$range": [0, count]}}},
        {"$unwind": "$days"},
        {"$project": {"date": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$days"}}, "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "a"}},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$a.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$a.late_count", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
        }},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]
    items = list(employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        for key in ("attendance_rate", "moving_avg_7d"):
            if item.get(key) is not None: item[key] = round_half_up(item[key], 4)
        item["present_count"] = round_half_up(item["present_count"], 2)
    return {"department": department, "items": items}

# ---------------------------- Explain endpoint -----------------------------
def explain_response(endpoint_name: str, collection_name: str, command: dict) -> dict:
    """Return the response envelope required by openapi.yaml."""
    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint_name, "collection": collection_name, "explain": result}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str,
                     emp_code: Optional[str] = None,
                     month: Optional[str] = None,
                     department: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     status: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     limit: int = Query(10, ge=1, le=50),
                     from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to")):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "Unsupported endpoint")
    try:
        if endpoint == "attendance_list":
            if date_from: parse_day(date_from)
            if date_to: parse_day(date_to)
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must be <= date_to")
            filt: dict[str, Any] = {}
            if emp_code is not None: filt["emp_code"] = emp_code
            if status is not None:
                if status not in STATUSES: raise HTTPException(422, "Invalid status")
                filt["status"] = status
            dr = {}
            if date_from: dr["$gte"] = date_from
            if date_to: dr["$lte"] = date_to
            if dr: filt["date"] = dr
            skip, size = page_args(page, page_size)
            command = {"find": "attendance_logs", "filter": filt,
                       "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": size}
            return explain_response(endpoint, "attendance_logs", command)

        if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
            if not month: raise HTTPException(422, "month is required")
            start_s, end_s, _, _ = month_query(month)

        if endpoint == "employee_monthly":
            if not emp_code: raise HTTPException(422, "emp_code is required")
            pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
                {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                          "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]},
                          "_half": {"$ifNull": ["$half_day", False]}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": ["$_weekday", [2, 3, 4, 5, 6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}}
            ]
            return explain_response(endpoint, "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})

        if endpoint == "department_summary":
            emp_match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
            if department is not None: emp_match["department"] = department
            pipeline = [
                {"$match": emp_match},
                {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},
                {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
                {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
                    "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
                {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
                    "present_days": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
                    "avg_work_hours": {"$let": {"vars": {"valid": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$valid"}, 0]}, {"$avg": "$$valid"}, None]}}}}},
                {"$sort": {"department": 1}}
            ]
            return explain_response(endpoint, "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}})

        if endpoint == "late_leaderboard":
            pipeline = [
                {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"}
            ]
            if department is not None: pipeline.append({"$match": {"employee.department": department}})
            pipeline += [
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                {"$match": {"rank": {"$lte": limit}}},
                {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
                {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
            ]
            return explain_response(endpoint, "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})

        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = parse_day(from_date), parse_day(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")
        day_count = (end - start).days + 1
        pipeline = [
            {"$match": {"department": department}}, {"$group": {"_id": None}},
            {"$project": {"_id": 0, "days": {"$range": [0, day_count]}}}, {"$unwind": "$days"},
            {"$project": {"date": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$days"}}, "format": "%Y-%m-%d"}}}},
            {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}}, {"$count": "n"}], "as": "hc"}},
            {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
                {"$unwind": "$e"}, {"$match": {"e.department": department}},
                {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "a"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "present_count": {"$ifNull": [{"$arrayElemAt": ["$a.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$a.late_count", 0]}, 0]}, "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}}},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
            {"$sort": {"date": 1}}
        ]
        return explain_response(endpoint, "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}})
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}")
