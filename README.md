# Employee Attendance and Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The service supports employee management, punch-in/out, regularization with audit history, monthly and department analytics, leaderboard ranking, daily trend analysis, and MongoDB explain output.

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017
export MONGO_DB=attendance_db
python sample_seed.py
uvicorn app.main:app --port 8000
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

Before submitting, test `GET /health`, the analytics endpoints, concurrent punch-in and punch-out requests, regularization history, and all five `/admin/explain` variants. Explain output should contain `IXSCAN` and no `COLLSCAN` for the main query.
