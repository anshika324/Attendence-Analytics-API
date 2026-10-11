"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, with local .env fallback)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes and Lifespan
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup."""
    # Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("status", 1), ("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions (Business Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    """R8: Decimal round half-up."""
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime truncated to seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to shift that started previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    time_str = dt_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start
    if is_overnight and time_str < shift_end:
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in strictly > 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    shift_start_dt = datetime.fromisoformat(f"{attendance_date}T{shift_start}:00").replace(tzinfo=IST)
    if punch_in_dt <= shift_start_dt:
        return 0
    diff_sec = int((punch_in_dt - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = truncate_to_seconds(punch_in_dt)
    p_out = truncate_to_seconds(punch_out_dt)
    diff_sec = (p_out - p_in).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30; otherwise 0.
    For overnight shifts, shift_end is on the next calendar day.
    """
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_date = (date.fromisoformat(attendance_date) + timedelta(days=1)).isoformat()
    else:
        end_date = attendance_date

    shift_end_dt = datetime.fromisoformat(f"{end_date}T{shift_end}:00").replace(tzinfo=IST)
    if punch_out_dt <= shift_end_dt:
        return 0
    diff_min = int((punch_out_dt - shift_end_dt).total_seconds()) // 60
    return diff_min if diff_min >= 30 else 0


def serialize_history(history_list: Optional[list]) -> list:
    """Format history list for API response."""
    if not history_list:
        return []
    res = []
    for h in history_list:
        changes = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if isinstance(f_val, datetime):
                f_val = dt_to_epoch_ms(f_val)
            if isinstance(t_val, datetime):
                t_val = dt_to_epoch_ms(t_val)
            changes[k] = {"from": f_val, "to": t_val}
        res.append({
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })
    return res


def serialize_attendance_record(doc: dict) -> dict:
    """Format attendance document for API response (no internal _id or id)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history")),
    }


def serialize_employee(doc: dict) -> dict:
    """Format employee document for API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Models
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only if MongoDB answers a ping; else 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforced by database unique index: concurrent duplicates yield 409.
    """
    doc = body.model_dump()
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees: sorted by emp_code asc; total reflects filter."""
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [serialize_employee(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in: creates the day's record (R1, R2); race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        p_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = get_attendance_date(p_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(p_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out: closes most recent record whose punch_in <= punched_at.
    Race-safe: two simultaneous punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        p_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    candidates = list(
        db.attendance_logs.find({
            "emp_code": body.emp_code,
            "punch_in": {"$lte": p_out_dt},
        }).sort("punch_in", -1).limit(1)
    )

    if not candidates:
        raise HTTPException(status_code=404, detail="no punch-in found")

    record = candidates[0]
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    p_in_dt = record["punch_in"]
    diff_sec = (p_out_dt - p_in_dt).total_seconds()
    if p_out_dt <= p_in_dt or diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(p_in_dt, p_out_dt)
    half_day = work_hours < 4.50
    ot_min = compute_overtime(p_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": ot_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records: sorted by date desc, then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recompute derived fields and append to history audit trail.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Setting status ABSENT or LEAVE clears both punch times; cannot supply punch_in or punch_out",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, not {date}",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if new_punch_out <= new_punch_in or diff_sec > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = new_work_hours < 4.50
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_start"], emp["shift_end"], date
            )
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    old_vals = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_vals = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes_db = {}
    for k in old_vals:
        if old_vals[k] != new_vals[k]:
            changes_db[k] = {"from": old_vals[k], "to": new_vals[k]}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency: ensure document has not been altered concurrently
    existing_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": existing_history},
        {
            "$set": new_vals,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="concurrent edit conflict")

    return serialize_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> tuple[int, list[str], list[dict]]:
    """Build the aggregation pipeline for an employee's monthly summary."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    weekdays = [
        date(year, mon, d).isoformat()
        for d in range(1, num_days + 1)
        if date(year, mon, d).weekday() < 5
    ]

    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-{num_days:02d}"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": weekdays,
                            "cond": {"$gte": ["$$this", "$joined_on"]},
                        }
                    }
                },
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.date", weekdays]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": 1,
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {"$divide": ["$present_days", "$working_days"]},
                                        100,
                                    ]
                                },
                                2,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]
    return num_days, weekdays, pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    _, _, pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.employees.aggregate(pipeline))
    if not res:
        raise HTTPException(status_code=404, detail="Employee not found")

    row = res[0]
    row.pop("_id", None)
    return row


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> tuple[int, list[dict]]:
    """Build the aggregation pipeline for monthly department summary."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    weekdays = [
        date(year, mon, d).isoformat()
        for d in range(1, num_days + 1)
        if date(year, mon, d).weekday() < 5
    ]

    match_stage = {"joined_on": {"$lte": f"{month}-{num_days:02d}"}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-{num_days:02d}"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.date", weekdays]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]},
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "emp_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wl",
                        "in": "$$wl.work_hours",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "all_work_hours_nested": {"$push": "$emp_work_hours"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours_nested",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$round": [{"$avg": "$flat_work_hours"}, 2]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]
    return num_days, pipeline


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month: cross-collection aggregation."""
    _, pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> tuple[int, list[dict]]:
    """Build the aggregation pipeline for late leaderboard with standard competition ranking."""
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]

    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return num_days, pipeline


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard: standard competition ranking with ties."""
    _, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list[dict]:
    """Build the aggregation pipeline for department daily trend with gap filling ($densify)."""
    from_dt = datetime.fromisoformat(from_str + "T00:00:00Z")
    to_dt = datetime.fromisoformat(to_str + "T00:00:00Z")
    to_dt_plus_1 = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department: gap filling via $densify and 7-day moving avg."""
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain output with executionStats verbosity for an endpoint's main query or pipeline.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        exp = cursor.explain()
        return jsonable_encoder(exp)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        _, _, pipeline = build_employee_monthly_pipeline(emp_code, month)
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return jsonable_encoder(exp)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _, pipeline = build_department_summary_pipeline(month, department)
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return jsonable_encoder(exp)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _, pipeline = build_late_leaderboard_pipeline(month, limit, department)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return jsonable_encoder(exp)

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return jsonable_encoder(exp)
