# DECISIONS.md

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
- `employees: {"emp_code": 1}` (unique): Enforces unique employee business codes and speeds up single-employee lookups and aggregation `$lookup` joins.
- `employees: {"department": 1, "joined_on": 1}` and `{"joined_on": 1}`: Accelerates department summary filtering and trend headcount lookups (`joined_on <= date`).
- `attendance_logs: {"emp_code": 1, "date": 1}` (unique): Guarantees at most one log per employee per day, makes punch-in concurrent inserts race-safe, and backs PATCH natural key lookups.
- `attendance_logs: {"date": 1, "emp_code": 1}`: Supports `GET /attendance` compound sorting (`date` desc, `emp_code` asc) and provides an index-covered scan for department trend.
- `attendance_logs: {"emp_code": 1, "punch_in": 1}`: Backs `POST /attendance/punch-out` locating the latest open punch-in.
- `attendance_logs: {"date": 1, "late_minutes": 1}`: Backs late leaderboard filtering by date and `late_minutes > 0`.
- *Considered and rejected*: `{"history.at": 1}`, because history is an embedded audit trail read with the parent document and never queried independently.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests fetch the employee, compute the IST attendance date, and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` atomically accepts the first write and rejects the second with a duplicate key error in the storage engine. PyMongo raises `DuplicateKeyError`, which our route catches and translates to `HTTPException(409, "already punched in for this date")`. Exactly one request returns 201; the competing request returns 409 without corrupted state.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The pipeline uses `$setWindowFields` with the `$rank` operator (standard competition ranking: 1, 2, 2, 4) and filters `rank <= limit` *after* ranking. Both tied employees receive the same rank at the cutoff and satisfy `rank <= limit`. The response returns all tied employees (exceeding `limit` items), ordered by `total_late_minutes` descending and tie-broken by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The aggregation pipeline initiates from the `employees` collection (filtering on `joined_on <= month_end`), rather than `attendance_logs`. It performs a `$lookup` to join attendance records. Employees with zero records produce an empty `logs: []` array rather than being dropped. Grouping by department counts every employee as 1 toward `headcount: {"$sum": 1}`, while log sums evaluate cleanly to 0.

5. **One thing you would change** if this had to serve 100x the data.
At 100x scale (~10 million records), on-the-fly cross-collection aggregations will strain memory and IO. I would introduce incremental daily rollups in a separate `daily_department_summary` collection, updated at punch-out or via a lightweight end-of-day job. The trend and summary endpoints would query these pre-aggregated daily summaries directly in O(1) time.
