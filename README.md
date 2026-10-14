# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for managing employee records, attendance, punch-in/punch-out operations, attendance corrections, and workforce analytics.

## Features

- **Employee Management:** Create and retrieve employee records.
- **Attendance Tracking:** Record employee punch-ins and punch-outs.
- **Attendance Corrections:** Update attendance records with a correction history.
- **Business Rules:** Implement attendance calculations and validation according to the provided API specification.
- **Analytics:** Monthly employee summaries, department summaries, late-arrival leaderboards, and department attendance trends.
- **Pagination and Filtering:** Retrieve attendance records using supported query parameters.
- **MongoDB Aggregation:** Perform analytics using MongoDB aggregation pipelines.
- **API Documentation:** Explore endpoints through automatically generated Swagger UI documentation.
- **Database Indexes:** Create indexes to support common queries and enforce applicable uniqueness constraints.

## Tech Stack

- **Language:** Python
- **Backend Framework:** FastAPI
- **Database:** MongoDB 6.0+
- **Database Driver:** PyMongo
- **Data Validation:** Pydantic
- **API Documentation:** OpenAPI / Swagger UI

## Project Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── DATA_MODEL.md
├── DECISIONS.md
├── openapi.yaml
├── PROBLEM_STATEMENT.docx
├── README.md
├── requirements.txt
├── REVIEW.md
└── sample_seed.py
```

## Prerequisites

- Python 3.10 or a compatible version supported by the dependencies.
- MongoDB 6.0 or newer.
- Git.
- A MongoDB connection URI and database name.

MongoDB Atlas or a local MongoDB instance can be used.

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/anirudh-beep/SDT-hiring-assignment.git
cd SDT-hiring-assignment
```

### 2. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root using `.env.example` as a reference, if provided.

Configure the following variables:

```env
MONGO_URI=mongodb://localhost:27017/
MONGO_DB=attendance_db
```

For MongoDB Atlas, replace the local URI with your Atlas connection string.

**Security:** Keep `.env` private. Never commit database credentials, passwords, or other secrets to GitHub.

### 5. Load sample data

If you are using a fresh database and want to load the provided sample records, run:

```bash
python sample_seed.py
```

Use the supplied seed script and data model to understand how the sample documents are structured.

### 6. Start the API server

From the project root, run:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For development with automatic reload:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

## API Documentation

Once the server starts, access:

| Resource | URL |
|---|---|
| Swagger UI | http://127.0.0.1:8000/docs |
| ReDoc | http://127.0.0.1:8000/redoc |
| OpenAPI JSON | http://127.0.0.1:8000/openapi.json |

Swagger UI allows you to inspect endpoint schemas, supply request parameters, and execute API requests.

## API Endpoints

The implementation is organized around the following endpoint groups.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Check application and database health |
| POST | `/employees` | Create an employee |
| GET | `/employees` | Retrieve employee records |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | Retrieve attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Retrieve monthly employee analytics |
| GET | `/analytics/departments/summary` | Retrieve department summaries |
| GET | `/analytics/leaderboard/late` | Retrieve the late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Retrieve department attendance trends |
| GET | `/admin/explain/{endpoint}` | Inspect query execution plans for supported endpoints |

Refer to `openapi.yaml` and `/docs` for the authoritative request schemas, response formats, parameters, and business rules.

## Business Rules and Data Model

The implementation is intended to follow the supplied API contract, including:

- Attendance dates and shift handling in Indian Standard Time (IST).
- Punch-in and punch-out validation.
- Late-arrival grace periods and overtime thresholds.
- Work-hour calculations and half-day classification.
- Attendance status validation.
- Historical records of manual attendance corrections.
- Monthly and department-level analytics.
- Pagination, filtering, and database query optimization.

See `openapi.yaml` for the R1–R10 business rules and `DATA_MODEL.md` for the MongoDB document structure.

## Testing

Before considering the application ready for use, verify:

1. The application starts successfully.
2. The database connection and health endpoint work.
3. Employee creation and retrieval behave as expected.
4. Punch-in and punch-out validation work correctly.
5. Attendance corrections preserve the required history.
6. Analytics return correct results for normal and edge-case data.
7. Duplicate requests and concurrent attendance operations are handled correctly.

Run the API against a working MongoDB instance and test the relevant endpoints through Swagger UI or an automated test suite.

## Assignment Documents

- `PROBLEM_STATEMENT.docx` — Assignment requirements and evaluation criteria.
- `openapi.yaml` — API contract and business rules.
- `DATA_MODEL.md` — MongoDB data model and sample document details.
- `REVIEW.md` — Code review notes.
- `DECISIONS.md` — Design decisions and their rationale.

## Author

**Anirudh Reddy Mosra**

GitHub: [anirudh-beep](https://github.com/anirudh-beep)
