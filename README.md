# Employee Attendance & Analytics API

A FastAPI backend backed by MongoDB implementing employee attendance tracking, punch-in/punch-out business rules, audit regularization, and aggregation-based analytics.

## Tech Stack
- **Python**: 3.11+
- **FastAPI**: REST API framework with Pydantic v2 schemas
- **MongoDB**: 6.0+ (Tested on MongoDB 7.0) with PyMongo
- **Uvicorn**: ASGI web server

## Setup & Running

1. **Clone the repository and enter the directory**:
   ```bash
   git clone <repo-url>
   cd <repo-folder>
   ```

2. **Create and activate virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables**:
   Configure `MONGO_URI` and `MONGO_DB` via environment variables (or local `.env`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

5. **Start the API server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

All database indexes (`emp_code`, `(emp_code, date)`, compound date and sorting indexes) are automatically and idempotently created on application startup via FastAPI's lifespan handler.

## Running Tests

Run the test suite with pytest:
```bash
PYTHONPATH=. pytest -v
```

## Documentation Files
- `REVIEW.md`: Detailed review and catalog of defects found in the starter code and their fixes.
- `DECISIONS.md`: Explanations of indexing strategy, concurrency guarantees, tie-breaking rules, headcount handling, and scale architecture.
