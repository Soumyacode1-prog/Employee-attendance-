"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    """Create the indexes needed by uniqueness, listing and analytics queries."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_on_department"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)], name="status_date"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("timestamp must be an epoch-millisecond integer")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)


def as_aware_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_aware_utc(value).timestamp() * 1000)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def parse_shift(value: str) -> int:
    if len(value) != 5 or value[2] != ":":
        raise ValueError("time must be HH:MM")
    h, m = value.split(":")
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError("time must be HH:MM")
    return int(h) * 60 + int(m)


def ist_local(value: datetime) -> datetime:
    return as_aware_utc(value).astimezone(IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ist_local(punch_in)
    end_minutes = parse_shift(shift_end)
    start_minutes = parse_shift(shift_start)
    result = local.date()
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        result -= timedelta(days=1)
    return result.isoformat()


def shift_instant(attendance_day: str, time_value: str, overnight: bool = False) -> datetime:
    day = parse_date(attendance_day)
    result = datetime(day.year, day.month, day.day, tzinfo=IST)
    result += timedelta(minutes=parse_shift(time_value))
    if overnight:
        result += timedelta(days=1)
    return result.astimezone(timezone.utc)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = ist_local(punch_in)
    start = local.replace(
        hour=parse_shift(shift_start) // 60,
        minute=parse_shift(shift_start) % 60,
        second=0,
        microsecond=0,
    )
    minutes = int((local - start).total_seconds() // 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str
) -> int:
    overnight = parse_shift(shift_end) <= parse_shift(shift_start)
    end = shift_instant(date_str, shift_end, overnight)
    minutes = int((as_aware_utc(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived_values(
    employee: dict[str, Any], date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime], status: str
) -> dict[str, Any]:
    if status in {"ABSENT", "LEAVE"}:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"]) if punch_in else 0
    if punch_in and punch_out:
        hours = compute_work_hours(punch_in, punch_out)
        return {
            "work_hours": hours,
            "late_minutes": late,
            "overtime_minutes": compute_overtime(
                punch_out, employee["shift_end"], date_str, employee["shift_start"]
            ),
            "half_day": hours < 4.50,
        }
    return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        changes[key] = {"from": epoch_ms(change.get("from")) if key in {"punch_in", "punch_out"} else change.get("from"),
                        "to": epoch_ms(change.get("to")) if key in {"punch_in", "punch_out"} else change.get("to")}
    return {"at": epoch_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: doc[key] for key in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")} | {"created_at": epoch_ms(doc.get("created_at"))}


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")), "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(x) for x in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_values(self):
        parse_date(self.joined_on)
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(StrictModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[int] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [serialize_employee(x) for x in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    values = derived_values(emp, d, ts, None, body.status)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        **values,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at)
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if open_record is None:
        raise HTTPException(404, "no open punch-in found")
    punch_in_value = as_aware_utc(open_record["punch_in"])
    if ts <= punch_in_value or ts > punch_in_value + timedelta(hours=24):
        raise HTTPException(422, "invalid punch-out time")
    values = derived_values(emp, open_record["date"], punch_in_value, ts, open_record["status"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": open_record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    try:
        parse_date(date_str)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if employee is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")

    supplied = body.model_dump(exclude_unset=True)
    new_status = supplied.get("status", record["status"])
    old_in = as_aware_utc(record.get("punch_in"))
    old_out = as_aware_utc(record.get("punch_out"))

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_in, new_out = None, None
    else:
        if "punch_in" in supplied:
            new_in = parse_epoch_ms(supplied["punch_in"])
        else:
            new_in = old_in
        if "punch_out" in supplied:
            new_out = parse_epoch_ms(supplied["punch_out"])
        else:
            new_out = old_out
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(new_in, employee["shift_start"], employee["shift_end"]) != date_str:
            raise HTTPException(422, "punch_in must belong to the record date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new_derived = derived_values(employee, date_str, new_in, new_out, new_status)
    old_values = {
        "status": record["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False),
    }
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **new_derived}
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old_values[key] != new_values[key]:
            changes[key] = {"from": old_values[key], "to": new_values[key]}
    if not changes:
        raise HTTPException(422, "regularization changes nothing")

    update = {key: new_values[key] for key in new_values}
    update["history"] = {"$each": [{"at": datetime.now(timezone.utc).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}]}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": len(record.get("history", []))}},
        {"$set": {key: value for key, value in new_values.items()}, "$push": {"history": update["history"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return serialize_attendance(updated)


def month_bounds(month: str) -> tuple[str, str]:
    """Return the inclusive first and last calendar dates for YYYY-MM."""
    try:
        first = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc
    if first.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be YYYY-MM")
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end = month_bounds(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")

    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$project": {
                    "_id": 0,
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "work_hours": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "punch_in": 1,
                    "punch_out": 1,
                    "date": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "joined_on": 1,
            "days": {"$range": [
                {"$toInt": {"$dateToString": {"date": {"$dateFromString": {"dateString": start}}, "format": "%d"}}},
                {"$add": [{"$dayOfMonth": {"$dateFromString": {"dateString": end}}}, 1]},
            ]},
            "logs": 1,
        }},
        {"$project": {
            "emp_code": 1,
            "joined_on": 1,
            "logs": 1,
            "working_days": {"$size": {"$filter": {
                "input": {"$map": {
                    "input": {
                        "$range": [
                            0,
                            {"$dateDiff": {
                                "startDate": {"$dateFromString": {"dateString": start}},
                                "endDate": {"$dateFromString": {"dateString": end}},
                                "unit": "day",
                            }},
                        ]
                    },
                    "as": "offset",
                    "in": {"$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": start}},
                        "unit": "day",
                        "amount": "$$offset",
                    }},
                }},
                "as": "day",
                "cond": {"$and": [
                    {"$gte": [{"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}}, "$joined_on"]},
                    {"$lte": [{"$dayOfWeek": "$$day"}, 6]},
                ]},
            }}},
        }},
        {"$project": {
            "emp_code": 1,
            "working_days": 1,
            "logs": 1,
            "present_days": {"$sum": {"$map": {
                "input": "$logs", "as": "this", "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 6]},
                    ]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]},
                    0,
                ]},
            }}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "x", "cond": {"$eq": ["$$x.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "x", "cond": {"$gt": ["$$x.late_minutes", 0]}}}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"},
        }},
    ]
    # MongoDB returns the stored derived values; only the final API rounding is
    # done here because the contract requires decimal half-up rounding.
    result = next(db.employees.aggregate(pipeline), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    working_days = result.get("working_days", 0)
    present_days = Decimal(str(result.get("present_days", 0)))
    attendance_pct = None if working_days == 0 else float(
        (present_days / Decimal(working_days) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    _, end = month_bounds(month)
    start, _ = month_bounds(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_filter["department"] = department

    pipeline = [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$project": {
                    "_id": 0,
                    "status": 1,
                    "date": 1,
                    "work_hours": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [
                        {"$ne": ["$logs.date", None]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                        False,
                    ]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                "$logs.work_hours",
                0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                1,
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        count = row.get("work_hours_count", 0)
        average = None
        if count:
            average = float((
                Decimal(str(row.get("work_hours_sum", 0))) / Decimal(count)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": row["_id"],
            "headcount": row.get("headcount", 0),
            "present_days": float(row.get("present_days", 0)),
            "avg_work_hours": average,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])
    items = [{
        "emp_code": row["_id"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": row["total_late_minutes"],
        "rank": row["rank"],
    } for row in db.attendance_logs.aggregate(pipeline)]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    try:
        start_day = parse_date(from_)
        end_day = parse_date(to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if end_day < start_day:
        raise HTTPException(422, "to must not be before from")
    if (end_day - start_day).days > 91:
        raise HTTPException(422, "trend range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    start_dt = datetime(start_day.year, start_day.month, start_day.day)
    end_dt = datetime(end_day.year, end_day.month, end_day.day)
    start_s, end_s = start_day.isoformat(), end_day.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "partitionByFields": [], "range": {
            "step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)],
        }}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$day"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]},
                {"$lte": ["$joined_on", {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}}]},
            ]}}}, {"$count": "n"}],
            "as": "headcount_rows",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_rows.n", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                {"$lte": [{"$dayOfWeek": "$day"}, 6]},
            ]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    result = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        def round_rate(value):
            return None if value is None else float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        result.append({
            "date": row["day"].date().isoformat(),
            "is_working_day": bool(row["is_working_day"]),
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": round_rate(rate),
            "moving_avg_7d": round_rate(moving),
        })
    return {"department": department, "items": result}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unknown explain endpoint")
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        command = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        explained = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}], "cursor": {}}
        collection = "attendance_logs"
    elif endpoint in {"late_leaderboard", "department_trend"}:
        if endpoint == "late_leaderboard" and not month:
            raise HTTPException(422, "month is required")
        if endpoint == "department_trend" and (not department or not from_ or not to):
            raise HTTPException(422, "department, from and to are required")
        if endpoint == "late_leaderboard":
            start, end = month_bounds(month)
            match = {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}
        else:
            try:
                parse_date(from_); parse_date(to)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            match = {"date": {"$gte": from_, "$lte": to}}
        command = {"aggregate": "attendance_logs", "pipeline": [{"$match": match}], "cursor": {}}
        collection = "attendance_logs"
    else:
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        query = {"joined_on": {"$lte": end}}
        if department:
            query["department"] = department
        command = {"aggregate": "employees", "pipeline": [{"$match": query}], "cursor": {}}
        collection = "employees"
    explained = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": explained}

