"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from decimal import ROUND_HALF_UP, Decimal
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def as_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_epoch_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(as_aware_utc(dt).timestamp() * 1000)


def as_utc_dt_from_epoch(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).replace(microsecond=0)


def validate_pagination(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_aware_utc(punch_out) - as_aware_utc(punch_in)).total_seconds())
    return float(
        (Decimal(seconds) / Decimal(3600)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_shift_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: date,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    end_date = attendance_date
    if (end_hour, end_minute) <= (start_hour, start_minute):
        end_date += timedelta(days=1)
    shift_end_ist = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    )
    overtime_seconds = (
        as_aware_utc(punch_out) - shift_end_ist.astimezone(timezone.utc)
    ).total_seconds()
    if overtime_seconds < 30 * 60:
        return 0
    return int(overtime_seconds // 60)


def serialize_history(history: list[dict]) -> list[dict]:
    result = []
    for entry in history:
        changes = {}
        for field, values in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": to_epoch_ms(values.get("from"))
                if field in {"punch_in", "punch_out"} and values.get("from") is not None
                else values.get("from"),
                "to": to_epoch_ms(values.get("to"))
                if field in {"punch_in", "punch_out"} and values.get("to") is not None
                else values.get("to"),
            }
        result.append(
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return result


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must match EMP0001-style format")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("email is invalid")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift time must be HH:MM in 24-hour format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_pair(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: int | None = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: int | None = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class AttendancePatchIn(BaseModel):
    punch_in: int | None = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: int | None = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    punch_in_ist = punch_in.astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    attendance_date = punch_in_ist.date()
    is_overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute) < (end_hour, end_minute):
        attendance_date -= timedelta(days=1)

    return attendance_date


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: date) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    shift_start_ist = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    )
    elapsed_seconds = (punch_in.astimezone(timezone.utc) - shift_start_ist.astimezone(timezone.utc)).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return int(elapsed_seconds // 60)


def parse_month(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(422, "month must be in YYYY-MM format") from exc
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def parse_iso_date(value: str, parameter_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{parameter_name} must be in YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{parameter_name} must be in YYYY-MM-DD format")
    return parsed


def count_weekdays(start: date, end: date) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, remaining_days = divmod(days, 7)
    weekdays = full_weeks * 5
    weekdays += sum(
        (start.weekday() + offset) % 7 < 5
        for offset in range(remaining_days)
    )
    return weekdays


def attendance_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    parsed_from = parse_iso_date(date_from, "date_from") if date_from else None
    parsed_to = parse_iso_date(date_to, "date_to") if date_to else None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")
    if parsed_from or parsed_to:
        query["date"] = {}
        if parsed_from:
            query["date"]["$gte"] = parsed_from.isoformat()
        if parsed_to:
            query["date"]["$lte"] = parsed_to.isoformat()
    if status:
        query["status"] = status
    return query


def month_date_range(month: str) -> tuple[date, date]:
    start, end = parse_month(month)
    return start, end


def monthly_attendance_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = month_date_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
            }
        },
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None = None) -> list[dict]:
    start, end = month_date_range(month)
    employee_match: dict = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$employee_code"]},
                            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "date": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "_weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$logs.date"}
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {"emp_code": "$emp_code", "department": "$department"},
                "headcount": {"$max": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: str | None = None) -> list[dict]:
    start, end = month_date_range(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str,
    start: date,
    end: date,
) -> list[dict]:
    start_datetime = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_datetime = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "date": {"$first": {"$literal": start_datetime}},
                "present_count": {"$sum": 0},
                "late_count": {"$sum": 0},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "let": {"code": "$emp_code", "record_date": "$date"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$code"]},
                                                {"$eq": ["$department", department]},
                                                {"$lte": ["$joined_on", "$$record_date"]},
                                            ]
                                        }
                                    }
                                }
                            ],
                            "as": "employee",
                        }
                    },
                    {"$match": {"employee.0": {"$exists": True}}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                        0,
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
                            "_id": 0,
                            "date": {"$dateFromString": {"dateString": "$_id"}},
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_datetime, end_datetime],
                },
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "_date_key": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "_weekday": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"date_key": "$_date_key"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$date_key"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$_headcount.headcount", 0]}, 0]
                },
                "is_working_day": {
                    "$and": [{"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$_moving_avg", None]}, None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$_moving_avg", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Startup / indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not available")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        doc = body.model_dump()
        doc["created_at"] = datetime.now(timezone.utc)
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    validate_pagination(page, page_size)
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for item in items:
        if "created_at" in item and isinstance(item["created_at"], datetime):
            item["created_at"] = to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts_utc = datetime.now(timezone.utc).replace(microsecond=0) if body.punched_at is None else as_utc_dt_from_epoch(body.punched_at)
    attendance_date = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    date_key = attendance_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_key,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_utc, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc["punch_in"]),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": doc["overtime_minutes"],
        "half_day": doc["half_day"],
        "history": [],
    }
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp=db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is None:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        punch_out_dt = as_utc_dt_from_epoch(body.punched_at).replace(microsecond=0)

    record_data=db.attendance_logs.find_one(
        {
            "emp_code":body.emp_code,
            "punch_in":{"$lte":punch_out_dt}
        },
        sort=[("punch_in",-1)]
    )
    if record_data is None:
        raise HTTPException(404,"no punchin found")
    if record_data.get("punch_out") is not None:
        raise HTTPException(409,"Record already punched out")
    punch_in_dt = as_aware_utc(record_data["punch_in"])
    elapsed=punch_out_dt-punch_in_dt
    if elapsed.total_seconds()<=0 or elapsed>timedelta(hours=24):
        raise HTTPException(422,"punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = work_hours < 4.50
    shift_date = date.fromisoformat(record_data["date"])
    overtime_minutes = compute_shift_overtime(
        punch_out_dt,
        emp["shift_start"],
        emp["shift_end"],
        shift_date,
    )

    update_result = db.attendance_logs.update_one(
        {
            "_id": record_data["_id"],
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if update_result.modified_count!=1:
        raise HTTPException(409,"Record already punched out")
    return{
        "emp_code": record_data["emp_code"],
        "date": record_data["date"],
        "status": record_data["status"],
        "punch_in": to_epoch_ms(record_data["punch_in"]),
        "punch_out": to_epoch_ms(punch_out_dt),
        "work_hours": work_hours,
        "late_minutes": record_data["late_minutes"],
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": serialize_history(record_data.get("history", [])),
    }

@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatchIn):
    try:
        record_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(422, "date must be in YYYY-MM-DD format") from exc
    if record_date.isoformat() != date:
        raise HTTPException(422, "date must be in YYYY-MM-DD format")

    supplied_fields = body.model_fields_set
    if "punch_in" in supplied_fields and body.punch_in is None:
        raise HTTPException(422, "punch_in cannot be null when supplied")
    if "punch_out" in supplied_fields and body.punch_out is None:
        raise HTTPException(422, "punch_out cannot be null when supplied")
    if "status" in supplied_fields and body.status is None:
        raise HTTPException(422, "status cannot be null when supplied")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_state = {
        "status": record.get("status"),
        "punch_in": as_aware_utc(old_punch_in) if old_punch_in is not None else None,
        "punch_out": as_aware_utc(old_punch_out) if old_punch_out is not None else None,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    final_state = old_state.copy()
    for field in ("punch_in", "punch_out"):
        if final_state[field] is not None:
            final_state[field] = final_state[field].replace(microsecond=0)
    if "status" in supplied_fields:
        final_state["status"] = body.status
    if "punch_in" in supplied_fields:
        final_state["punch_in"] = as_utc_dt_from_epoch(body.punch_in)
    if "punch_out" in supplied_fields:
        final_state["punch_out"] = as_utc_dt_from_epoch(body.punch_out)

    final_status = final_state["status"]
    is_non_working_status = final_status in {"ABSENT", "LEAVE"}
    if is_non_working_status:
        if "punch_in" in supplied_fields or "punch_out" in supplied_fields:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE status")
        final_state["punch_in"] = None
        final_state["punch_out"] = None
    elif final_state["punch_in"] is None:
        raise HTTPException(422, "a presence status requires punch_in")

    final_punch_in = final_state["punch_in"]
    final_punch_out = final_state["punch_out"]
    if final_punch_in is not None:
        derived_date = get_attendance_date(
            final_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        )
        if derived_date != record_date:
            raise HTTPException(422, "punch_in must belong to the attendance date in the path")

    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        elapsed = final_punch_out - final_punch_in
        if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if is_non_working_status:
        final_state.update(
            {
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            }
        )
    else:
        final_state["late_minutes"] = compute_late_minutes(
            final_punch_in,
            emp["shift_start"],
            record_date,
        )
        if final_punch_out is None:
            final_state.update(
                {
                    "work_hours": None,
                    "overtime_minutes": 0,
                    "half_day": False,
                }
            )
        else:
            work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_state.update(
                {
                    "work_hours": work_hours,
                    "overtime_minutes": compute_shift_overtime(
                        final_punch_out,
                        emp["shift_start"],
                        emp["shift_end"],
                        record_date,
                    ),
                    "half_day": work_hours < 4.50,
                }
            )

    changes = {}
    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        old_value = old_state[field]
        new_value = final_state[field]
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "the request does not change the attendance record")

    changed_at = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": changed_at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_filter = {
        "_id": record["_id"],
        "emp_code": emp_code,
        "date": date,
    }
    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    update_result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": final_state,
            "$push": {"history": history_entry},
        },
    )
    if update_result.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently; retry the correction")

    serialized_history = []
    for entry in [*record.get("history", []), history_entry]:
        serialized_changes = {}
        for field, values in entry.get("changes", {}).items():
            serialized_changes[field] = {
                "from": to_epoch_ms(values.get("from"))
                if field in {"punch_in", "punch_out"} and values.get("from") is not None
                else values.get("from"),
                "to": to_epoch_ms(values.get("to"))
                if field in {"punch_in", "punch_out"} and values.get("to") is not None
                else values.get("to"),
            }
        serialized_history.append(
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": serialized_changes,
            }
        )

    return {
        "emp_code": emp_code,
        "date": date,
        "status": final_state["status"],
        "punch_in": to_epoch_ms(final_state["punch_in"]),
        "punch_out": to_epoch_ms(final_state["punch_out"]),
        "work_hours": final_state["work_hours"],
        "late_minutes": final_state["late_minutes"],
        "overtime_minutes": final_state["overtime_minutes"],
        "half_day": final_state["half_day"],
        "history": serialized_history,
    }

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = 1,
    page_size: int = 20,
):
    validate_pagination(page, page_size)
    q = attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    records = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []
    for item in records:
        record = {
            "emp_code": item["emp_code"],
            "date": item["date"],
            "status": item["status"],
            "punch_in": to_epoch_ms(item["punch_in"]) if item.get("punch_in") is not None else None,
            "punch_out": to_epoch_ms(item["punch_out"]) if item.get("punch_out") is not None else None,
            "work_hours": item.get("work_hours"),
            "late_minutes": item.get("late_minutes", 0),
            "overtime_minutes": item.get("overtime_minutes", 0),
            "half_day": item.get("half_day", False),
            "history": [],
        }
        if "history" in item:
            history = []
            for entry in item["history"]:
                history_entry = {
                    "at": to_epoch_ms(entry["at"]) if entry.get("at") is not None else None,
                    "by": entry.get("by"),
                    "reason": entry.get("reason"),
                    "changes": {},
                }
                for key, value in (entry.get("changes") or {}).items():
                    history_entry["changes"][key] = {
                        "from": to_epoch_ms(value.get("from")) if key in {"punch_in", "punch_out"} and value.get("from") is not None else value.get("from"),
                        "to": to_epoch_ms(value.get("to")) if key in {"punch_in", "punch_out"} and value.get("to") is not None else value.get("to"),
                    }
                history.append(history_entry)
            record["history"] = history
        items.append(record)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    month_start, month_end = month_date_range(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")

    aggregation = list(
        db.attendance_logs.aggregate(
            monthly_attendance_pipeline(emp_code, month),
            allowDiskUse=True,
        )
    )
    stats = aggregation[0] if aggregation else {}
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = count_weekdays(max(month_start, joined_on), month_end)
    present_days = stats.get("present_days", 0)
    attendance_pct = None
    if working_days:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal(100) / Decimal(working_days)).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    parse_month(month)
    items = list(
        db.employees.aggregate(
            department_summary_pipeline(month, department),
            allowDiskUse=True,
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
):
    parse_month(month)
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(month, limit, department),
            allowDiskUse=True,
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    start = parse_iso_date(from_date, "from")
    end = parse_iso_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to cannot be earlier than from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    items = list(
        db.employees.aggregate(
            department_trend_pipeline(department, start, end),
            allowDiskUse=True,
        )
    )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = 1,
    page_size: int = 20,
    from_date: str | None = Query(
        default=None,
        alias="from",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    to_date: str | None = Query(
        default=None,
        alias="to",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
):
    if endpoint == "attendance_list":
        validate_pagination(page, page_size)
        query = attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = monthly_attendance_pipeline(emp_code, month)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
        collection_name = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = department_summary_pipeline(month, department)
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = leaderboard_pipeline(month, limit, department)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
        collection_name = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = parse_iso_date(from_date, "from")
        end = parse_iso_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "date range must be ordered and cannot exceed 92 days")
        pipeline = department_trend_pipeline(department, start, end)
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
        collection_name = "employees"

    explain = db.command(
        "explain",
        command,
        verbosity="executionStats",
    )
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain,
    }
