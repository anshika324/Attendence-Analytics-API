import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
import os
import concurrent.futures

# Set test environment
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "test_attendance_db"

from app.main import app, client, db, init_indexes

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    client.drop_database("test_attendance_db")
    init_indexes()
    yield
    client.drop_database("test_attendance_db")

@pytest.fixture
def clean_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_indexes()
    yield

client_test = TestClient(app)

def test_health():
    res = client_test.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employee_crud(clean_db):
    emp_data = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    # Create employee
    res = client_test.post("/employees", json=emp_data)
    assert res.status_code == 201
    created = res.json()
    assert created["emp_code"] == "EMP0001"
    assert "_id" not in created
    assert isinstance(created["created_at"], int)

    # Duplicate emp_code -> 409
    res_dup = client_test.post("/employees", json=emp_data)
    assert res_dup.status_code == 409

    # Invalid emp_code format -> 422
    invalid_data = dict(emp_data, emp_code="INVALID")
    assert client_test.post("/employees", json=invalid_data).status_code == 422

    # List employees
    list_res = client_test.get("/employees")
    assert list_res.status_code == 200
    body = list_res.json()
    assert body["total"] == 1
    assert body["items"][0]["emp_code"] == "EMP0001"
    assert body["page"] == 1
    assert body["page_size"] == 20

def test_punch_in_and_late_grace(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Test punch-in exactly at 09:40:00 IST -> late_minutes = 0 (grace 10 min)
    dt_at_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_at_grace = int(dt_at_grace.timestamp() * 1000)

    res = client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_at_grace,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    record = res.json()
    assert record["date"] == "2026-07-06"
    assert record["late_minutes"] == 0
    assert record["status"] == "PRESENT"
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["history"] == []
    assert "_id" not in record
    assert "id" not in record

    # Duplicate punch-in for same date -> 409
    dup_res = client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_at_grace,
        "status": "PRESENT"
    })
    assert dup_res.status_code == 409

def test_punch_in_late_09_40_01(clean_db):
    # 09:40:01 gives 10 minutes late per R2
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })
    dt_late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    ms_late = int(dt_late.timestamp() * 1000)

    res = client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_late,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10

def test_punch_out_and_overtime_and_half_day(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in 09:28 IST (03:58 UTC)
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })

    # Punch out 18:35 IST (13:05 UTC) -> 9.12 hours, overtime 5 min (< 30) -> overtime_minutes = 0
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    res = client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 200
    doc = res.json()
    assert doc["work_hours"] == 9.12
    assert doc["overtime_minutes"] == 0
    assert doc["half_day"] is False

    # Second punch out on same record -> 409
    res_dup = client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res_dup.status_code == 409

def test_overnight_shift(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera Nair",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10"
    })

    # Punch in 21:55 IST on 2026-07-06 (16:25 UTC)
    in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    res_in = client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"
    assert res_in.json()["late_minutes"] == 0

    # Punch out 06:40 IST on 2026-07-07 (01:10 UTC next day)
    # Overtime: 40 min past 06:00 (>= 30 min) -> 40 min overtime
    out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    res_out = client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0005",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res_out.status_code == 200
    doc = res_out.json()
    assert doc["date"] == "2026-07-06"
    assert doc["work_hours"] == 8.75
    assert doc["overtime_minutes"] == 40
    assert doc["half_day"] is False

def test_regularize_patch(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })
    out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    # HR regularizes punch_in to 09:28 IST (03:58 UTC)
    new_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    patch_res = client_test.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": int(new_in.timestamp() * 1000),
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert patch_res.status_code == 200
    doc = patch_res.json()
    assert doc["late_minutes"] == 0
    assert doc["work_hours"] == 9.03
    assert len(doc["history"]) == 1
    h = doc["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # Patch that changes nothing -> 422
    res_noop = client_test.patch("/attendance/EMP0001/2026-07-07", json={
        "reason": "redundant correction",
        "regularized_by": "hr.admin"
    })
    assert res_noop.status_code == 422

def test_concurrency_punch_in(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    ms = int(in_dt.timestamp() * 1000)

    def do_punch():
        return client_test.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": ms,
            "status": "PRESENT"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch(), range(5)))

    assert results.count(201) == 1
    assert results.count(409) == 4

def test_concurrency_punch_out(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })

    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    ms = int(out_dt.timestamp() * 1000)

    def do_punch_out():
        return client_test.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch_out(), range(5)))

    assert results.count(200) == 1
    assert results.count(409) == 4

def test_analytics_employee_monthly_and_mid_month_joiner(clean_db):
    # Mid-month joiner: joined 2026-07-13
    client_test.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13"
    })

    # Record on 2026-07-14 (Tuesday, weekday)
    in_dt = datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })
    out_dt = datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    res = client_test.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0002"
    assert data["month"] == "2026-07"
    # July 2026 has 23 weekdays total. From July 13 to 31, there are 15 weekdays.
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    # attendance_pct: 1.0 / 15 * 100 = 6.6666... -> 6.67
    assert data["attendance_pct"] == 6.67

def test_late_leaderboard_ties_and_competition_ranking(clean_db):
    # Create 3 employees in Sales
    for i in range(1, 4):
        client_test.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    # EMP0001: 50 min late on 2026-07-06 (punch in at 10:20 IST)
    # EMP0002: 50 min late on 2026-07-06 (punch in at 10:20 IST) -> tied on 50 min
    # EMP0003: 20 min late on 2026-07-06 (punch in at 09:50 IST)
    # Ranks should be: EMP0001: Rank 1, EMP0002: Rank 1 (tied), EMP0003: Rank 3 (skipped rank 2!)
    t1 = datetime(2026, 7, 6, 4, 50, 0, tzinfo=timezone.utc) # 10:20 IST -> 50 min late
    t3 = datetime(2026, 7, 6, 4, 20, 0, tzinfo=timezone.utc) # 09:50 IST -> 20 min late

    client_test.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(t1.timestamp() * 1000), "status": "PRESENT"})
    client_test.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": int(t1.timestamp() * 1000), "status": "PRESENT"})
    client_test.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": int(t3.timestamp() * 1000), "status": "PRESENT"})

    res = client_test.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert res.status_code == 200
    items = res.json()["items"]
    # Limit is 1, but EMP0001 and EMP0002 both have rank 1 <= 1, so both returned!
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["emp_code"] == "EMP0001"
    assert items[1]["emp_code"] == "EMP0002"

    res_all = client_test.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    items_all = res_all.json()["items"]
    assert len(items_all) == 3
    assert items_all[0]["rank"] == 1
    assert items_all[1]["rank"] == 1
    assert items_all[2]["rank"] == 3 # standard competition ranking skips 2!

def test_department_summary_headcount_zero_logs(clean_db):
    # Employee 1 has logs, Employee 2 has NO logs
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })
    client_test.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Only EMP0001 punches in
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    res = client_test.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    eng = items[0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] == 2 # Both employees counted!
    assert eng["present_days"] == 1.0
    assert eng["avg_work_hours"] == 9.12

def test_department_trend_with_gap_filling_and_moving_avg(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # 2026-07-06 (Monday): present
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000), "status": "PRESENT"})
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    client_test.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})

    # Trend from 2026-07-05 to 2026-07-07 (3 days: Sun, Mon, Tue)
    res = client_test.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Sunday 2026-07-05: weekend
    assert items[0]["date"] == "2026-07-05"
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # Monday 2026-07-06: working day with punch
    assert items[1]["date"] == "2026-07-06"
    assert items[1]["is_working_day"] is True
    assert items[1]["present_count"] == 1.0
    assert items[1]["attendance_rate"] == 1.0
    assert items[1]["moving_avg_7d"] == 1.0

    # Tuesday 2026-07-07: working day with NO punch -> rate 0.0
    assert items[2]["date"] == "2026-07-07"
    assert items[2]["is_working_day"] is True
    assert items[2]["present_count"] == 0.0
    assert items[2]["attendance_rate"] == 0.0
    # moving_avg_7d over [1.0, 0.0] = 0.5
    assert items[2]["moving_avg_7d"] == 0.5

def test_explain_endpoints(clean_db):
    client_test.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        params = {}
        if ep == "employee_monthly":
            params = {"emp_code": "EMP0001", "month": "2026-07"}
        elif ep == "department_summary":
            params = {"month": "2026-07", "department": "Engineering"}
        elif ep == "late_leaderboard":
            params = {"month": "2026-07", "limit": 10}
        elif ep == "department_trend":
            params = {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}
        res = client_test.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert "serverInfo" in data or "queryPlanner" in data or "stages" in data
