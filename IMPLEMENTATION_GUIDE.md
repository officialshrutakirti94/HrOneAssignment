# HROne Software Engineer Assignment --- Implementation Guide

> **Purpose:** Use this file as the working roadmap for completing the
> Employee Attendance & Analytics API assignment. It consolidates the
> requirements and constraints stated in `PROBLEM_STATEMENT.docx`.
>
> **Important:** The candidate kit also includes `openapi.yaml`,
> `DATA_MODEL.md`, starter `app/main.py`, seed data, and templates. This
> guide cannot replace those files where exact request/response schemas,
> field types, query parameters, and starter-code defects are concerned.
> Follow `openapi.yaml` and `DATA_MODEL.md` for those exact details.

------------------------------------------------------------------------

## 1. Assignment overview

Build the backend for an employee attendance system using:

-   Python 3.11+
-   FastAPI
-   PyMongo or Motor
-   MongoDB 6.0+
-   MongoDB Atlas free tier or local Docker for development

The application supports employee creation/listing, attendance
punch-in/punch-out, manual attendance corrections, analytics, and
query-plan inspection.

### Work areas and evaluation weights

  ------------------------------------------------------------------------
  Area                                        Weight Requirement
  --------------------- ---------------------------- ---------------------
  Part A --- Review                              10% Find and fix defects
                                                     in starter
                                                     `app/main.py`;
                                                     document findings in
                                                     `REVIEW.md`.

  CRUD, rules, and                               30% Correct status codes,
  concurrency                                        business rules
                                                     R1--R5, pagination,
                                                     and race conditions.

  Analytics correctness                          30% Correct aggregation
                                                     results, including
                                                     ties, employees with
                                                     no logs, weekends,
                                                     joiners, and
                                                     overnight shifts.

  MongoDB usage and                              15% Appropriate indexes,
  speed                                              explain plans, and
                                                     performance on about
                                                     100,000 attendance
                                                     records.

  Decisions and code                             15% `DECISIONS.md`,
  quality                                            readable code, input
                                                     validation, and sound
                                                     design choices.
  ------------------------------------------------------------------------

The assignment estimates approximately 4--5 hours, but difficult edge
cases and aggregation pipelines may take longer. Correctness matters
more than merely returning plausible-looking output.

------------------------------------------------------------------------

## 2. Candidate-kit files to inspect

Before implementing endpoints, inspect the supplied files:

-   `PROBLEM_STATEMENT.docx` --- overall requirements and business
    rules.
-   `openapi.yaml` --- authoritative API contract: paths, parameters,
    status codes, response fields, and rules R1--R10.
-   `app/main.py` --- starter application for Part A; it runs, but
    contains more than ten defects.
-   `DATA_MODEL.md` --- collections, field types, seed-data rules, and
    sample documents.
-   `sample_data/` and `sample_seed.py` --- sample documents and seed
    script.
-   `REVIEW.md` and `DECISIONS.md` --- templates to complete.

**Do not guess API field names or response structures.** Check
`openapi.yaml` and `DATA_MODEL.md` before coding each endpoint.

------------------------------------------------------------------------

## 3. Required repository and submission contract

The public Git repository must include:

``` text
your-repository/
├── app/
│   └── main.py
├── requirements.txt
├── REVIEW.md
├── DECISIONS.md
└── README.md
```

The assignment requires all application code to be in **one file:
`app/main.py`**.

### Runtime contract

-   The app must start from the repository root with:

    ``` bash
    uvicorn app.main:app --port 8000
    ```

-   `GET /health` must return HTTP 200 when MongoDB responds and HTTP
    503 when it does not.

-   The app must be ready within 20 seconds.

-   Read `MONGO_URI` and `MONGO_DB` from environment variables.

-   Real environment variables must take priority over values from a
    local `.env`.

-   Never hard-code connection details.

-   Create all required indexes automatically at startup, idempotently.

-   The grader starts the app against a database that already contains
    the hidden dataset.

-   No network access other than MongoDB is allowed. Do not call
    external services.

-   The application must handle concurrent requests correctly.

### Do not commit

-   `.env`
-   Secrets or credentials
-   Dockerfile
-   Virtual environments
-   Data dumps

Submit a **public Git repository** and its link by the deadline and in
the way specified in the hiring email.

------------------------------------------------------------------------

## 4. Data model and naming conventions

MongoDB contains two collections:

-   `employees`
-   `attendance_logs`

The grader seeds these collections directly. Your application must read
the data exactly as described in `DATA_MODEL.md`.

### Employee identity

-   `emp_code` is the employee's business identifier everywhere in the
    API.
-   It is unique and does not change.
-   It is not MongoDB's internal `_id`.
-   `_id` is internal and must never appear in requests or responses.
-   An attendance record is identified by the pair `(emp_code, date)`;
    attendance records do not have an API-facing ID.

### Time and date representations

-   API instants such as `punched_at`, `punch_in`, `punch_out`,
    `created_at`, and history timestamps are **integer epoch
    milliseconds**.
-   MongoDB stores instants as BSON datetimes in UTC.
-   Convert between BSON datetimes and API epoch milliseconds at the
    boundary.
-   Values represented as seconds, floats, or strings are rejected with
    HTTP 422.
-   PyMongo returns naive datetimes by default; explicitly decide how to
    handle them.
-   Attendance dates use `YYYY-MM-DD` in IST.
-   Months use `YYYY-MM`.
-   Shift times use `HH:MM` in 24-hour IST format.

### Derived fields

The API computes these fields; clients do not send them:

-   `work_hours`
-   `late_minutes`
-   `overtime_minutes`
-   `half_day`

These fields are stored in MongoDB. Analytics must read the stored
derived values instead of recomputing them.

### Missing data

-   A seeded record may omit `history`; treat it as an empty list.
-   A seeded record may omit `half_day`; treat it as `false`.
-   A weekday with no attendance record is **not** the same as a record
    whose status is `ABSENT`.

### Attendance status values

  -----------------------------------------------------------------------
  Status            Meaning           Punch-in          Counts as
                                                        present?
  ----------------- ----------------- ----------------- -----------------
  `PRESENT`         Worked on site    Required          Yes

  `WFH`             Worked from home  Required          Yes

  `ON_DUTY`         Official duty     Required          Yes
                    away from the                       
                    office                              

  `ABSENT`          Did not work; no  Both punch times  No
                    approved leave    null              

  `LEAVE`           Approved leave    Both punch times  No
                                      null              
  -----------------------------------------------------------------------

`ABSENT` and `LEAVE` records may come from seeded data or a manual
correction.

### History / audit trail

`history` is for **manual corrections made through PATCH only**.

Each correction appends an entry recording:

-   When the correction happened
-   Who made it
-   Why it was made
-   The before and after value of every field that actually changed

Rules:

-   Punch-in and punch-out do not write to history.
-   History entries are never edited or removed.
-   A record that has never been corrected has an empty history.
-   Concurrent regularizations must not silently lose one another's
    history entries.

------------------------------------------------------------------------

## 5. Business rules R1--R10

Treat these as exact requirements. Write tests for boundary values, not
only ordinary cases.

### R1 --- Time zone, attendance date, and overnight shifts

-   Attendance dates and shift times are in IST (UTC+05:30).
-   The record's attendance date is the IST calendar date of `punch_in`.
-   For overnight shifts where `shift_end <= shift_start` (for example,
    `22:00–06:00`), the shift ends on the next calendar day.
-   For an overnight shift, a punch-in earlier than `shift_end` belongs
    to the shift that started on the previous day.
-   Truncate instants to whole seconds **before any calculation**.
-   Store and return the truncated instant.

### R2 --- Late arrival

-   Grace period: 10 minutes.
-   Late arrival applies only when punch-in is **strictly more than
    10:00 after** `shift_start`.
-   When late, `late_minutes` is the whole-minute floor measured from
    the original `shift_start`, not from the end of the grace period.

Example for a 09:30 shift:

  Punch-in     `late_minutes`
  ---------- ----------------
  09:40:00                  0
  09:40:01                 10
  10:15:59                 45

### R3 --- Overtime

-   Calculate whole minutes, floored, from `shift_end` to `punch_out`.
-   Overtime is counted only when that difference is at least 30
    minutes.
-   Otherwise, `overtime_minutes = 0`.

Test the 29-, 30-, and 31-minute boundaries.

### R4 --- Work hours

-   Calculate `(punch_out - punch_in)` in seconds divided by 3600.
-   Round to two decimal places using **half-up** rounding.
-   `work_hours` is `null` until punch-out.

### R5 --- Half day

-   After punch-out, `half_day = true` when the rounded
    `work_hours < 4.50`.
-   A half day contributes **0.5 of a present day**.

### R6 --- Presence

-   `PRESENT`, `WFH`, and `ON_DUTY` count as present.
-   `ABSENT` and `LEAVE` do not.

### R7 --- Working days and weekends

-   Working days are Monday--Friday.
-   There is no holiday calendar.
-   For employees who join mid-month, working days are counted from
    `joined_on`.
-   `present_days` counts only Monday--Friday records.
-   Weekend attendance records still contribute to late and overtime
    totals.

### R8 --- Number rounding

-   Reported numbers use two decimal places with half-up rounding.
-   Rates use four decimal places with half-up rounding.

### R9 --- Headcount

-   Include employees whose `joined_on` is on or before the end of the
    reporting period.
-   Include eligible employees even if they have no attendance logs.

### R10 --- Pagination

-   Page numbers start at 1.
-   Default `page_size` is 20.
-   Maximum `page_size` is 100.
-   `total` must reflect the filters, not just the current page.

------------------------------------------------------------------------

## 6. Endpoint inventory

The assignment lists these 12 endpoints. The exact request parameters,
schemas, and response fields must come from `openapi.yaml`.

  ------------------------------------------------------------------------------------------------------
  Method            Endpoint                                      Responsibility       Starter or build?
  ----------------- --------------------------------------------- -------------------- -----------------
  `GET`             `/health`                                     200 when MongoDB     Starter ---
                                                                  answers; otherwise   review
                                                                  503                  

  `POST`            `/employees`                                  Create employee; 409 Starter ---
                                                                  for duplicate        review
                                                                  `emp_code`; 422 for  
                                                                  invalid input        

  `GET`             `/employees`                                  Filter by            Starter ---
                                                                  department;          review
                                                                  paginate; sort by    
                                                                  `emp_code`           

  `POST`            `/attendance/punch-in`                        Create the day's     Starter ---
                                                                  record; apply R1/R2; review
                                                                  race-safe            

  `GET`             `/attendance`                                 Filter by employee,  Starter ---
                                                                  dates, and status;   review
                                                                  paginate; scale to   
                                                                  100k records         

  `POST`            `/attendance/punch-out`                       Calculate work       Build
                                                                  hours, overtime, and 
                                                                  half day; support    
                                                                  overnight shifts     

  `PATCH`           `/attendance/{emp_code}/{date}`               Regularize one day's Build
                                                                  record; recompute    
                                                                  derived fields;      
                                                                  append history       

  `GET`             `/analytics/employees/{emp_code}/monthly`     Monthly summary for  Build
                                                                  one employee using   
                                                                  aggregation          

  `GET`             `/analytics/departments/summary`              Department summary   Build
                                                                  using                
                                                                  cross-collection     
                                                                  aggregation          

  `GET`             `/analytics/leaderboard/late`                 Late-comer ranking   Build
                                                                  with ties using      
                                                                  aggregation/window   
                                                                  function             

  `GET`             `/analytics/departments/{department}/trend`   Daily series with    Build
                                                                  gap filling and a    
                                                                  7-day moving average 

  `GET`             `/admin/explain/{endpoint}`                   MongoDB explain      Build
                                                                  output for an        
                                                                  endpoint's main      
                                                                  query                
  ------------------------------------------------------------------------------------------------------

The five starter endpoints must be reviewed and fixed as needed. The
seven endpoints marked **Build** must be implemented.

------------------------------------------------------------------------

## 7. Part A --- Review and fix `app/main.py`

The starter application runs and the happy path works, but it contains
**more than ten defects** across helpers and endpoints. Some are visible
immediately; others appear only with concurrency, scale, or particular
inputs.

### What to do

1.  Read all of `app/main.py`, including helper functions.
2.  Check every relevant behavior against `openapi.yaml`,
    `DATA_MODEL.md`, and R1--R10.
3.  Test normal inputs, invalid inputs, boundary timestamps, missing
    data, and concurrency.
4.  Fix the defects.
5.  Record each finding in `REVIEW.md`.

### For every defect, record

-   **Where:** file/function/endpoint.
-   **Problem:** what is incorrect and why.
-   **How to notice:** a concrete input, test, or symptom that exposes
    it.
-   **Fix:** what you changed.

Also mention important behaviors you examined and decided were correct.
Prefer short, precise findings with concrete failing examples over vague
descriptions.

Do not assume a helper is correct merely because it is provided for
reuse.

------------------------------------------------------------------------

## 8. Part B --- Implement the remaining endpoints

### General requirements

-   Implement every endpoint marked "You" in the endpoint inventory.
-   Follow `openapi.yaml` exactly.
-   Analytics must be implemented as **MongoDB aggregation pipelines**,
    not by fetching documents and looping in Python.
-   Return the correct HTTP status codes:
    -   `404` --- missing resources, including an unknown employee or no
        record for the requested date.
    -   `409` --- conflicts.
    -   `422` --- validation failures, including bad timestamps.
    -   A `500` response is always a failed test.
-   The implementation must be fast on about 100,000 attendance records.
-   List and analytics endpoints must be backed by indexes.

### 8.1 Punch-out

Implement `POST /attendance/punch-out`.

Checklist:

-   Find the correct employee/date attendance record.
-   Validate the timestamp using the API contract.
-   Apply R1 before calculating durations.
-   Compute `work_hours` using R4 and half-up rounding.
-   Compute `overtime_minutes` using R3.
-   Compute `half_day` using R5.
-   Support overnight shifts.
-   Return the exact contract-defined response and status codes.
-   Ensure simultaneous punch-outs yield exactly one HTTP 200.

### 8.2 Manual regularization / PATCH

Implement `PATCH /attendance/{emp_code}/{date}`.

Checklist:

-   Follow the exact PATCH schema and allowed fields in `openapi.yaml`.
-   Handle missing employee/record as specified by the contract.
-   Recompute derived fields when relevant source fields change.
-   Append a history entry only for fields that actually changed.
-   Include the required actor, timestamp, reason, and before/after
    values.
-   Preserve all history entries during concurrent regularizations.
-   Do not let punch-in or punch-out modify the audit history.

### 8.3 Employee monthly analytics

Implement `GET /analytics/employees/{emp_code}/monthly`.

-   Use a MongoDB aggregation pipeline.
-   Follow the exact month parameter and output contract in
    `openapi.yaml`.
-   Use stored derived values for analytics.
-   Apply R6--R8 wherever relevant.
-   Account for half days, weekends, records without punch-out, and
    missing optional fields.
-   Test empty months and boundary dates.

### 8.4 Department summary --- difficult endpoint

Implement `GET /analytics/departments/summary`.

The challenge is that attendance logs identify employees by `emp_code`,
but department membership is stored on employee documents.

The pipeline must:

-   Include employees who joined on or before the reporting period's
    end.
-   Include eligible employees with no attendance logs.
-   Exclude employees who have not joined by the period end.
-   Calculate attendance metrics using the correct underlying records.
-   Calculate averages over records, **not an average of averages**.

Use a cross-collection aggregation, such as `$lookup`, where
appropriate. Design headcount and log-based metrics carefully so
employees without logs are not accidentally dropped.

Test a department containing: - An eligible employee with no logs - An
employee who joined mid-month - An employee who joins after the period -
Employees with multiple attendance records

### 8.5 Late leaderboard --- difficult endpoint

Implement `GET /analytics/leaderboard/late`.

Required tie behavior: ties share a rank, and the next rank is skipped.
Example: `1, 2, 2, 4`.

-   Use MongoDB's window-ranking capability as appropriate, such as
    `$setWindowFields` with `$rank`.
-   Sort tied rows by `emp_code` for deterministic output.
-   Apply the `limit` **after ranking**.
-   Do not use an array index as the rank.
-   Do not substitute a consecutive ranking method that produces
    `1, 2, 2, 3`.

Test ties at the top, in the middle, and at the limit cutoff.

### 8.6 Department trend --- difficult endpoint

Implement `GET /analytics/departments/{department}/trend`.

-   Return exactly one row per calendar day in the requested range.
-   Include weekends.
-   Include dates with no attendance records.
-   Calculate the required daily values.
-   Calculate the seven-day moving average in MongoDB, not in a Python
    loop.
-   The first rows use a shorter window when fewer preceding dates
    exist.

Investigate MongoDB aggregation facilities such as `$densify` for
date-series gap filling and `$setWindowFields` for moving averages.
Confirm exact output fields and missing-value behavior against
`openapi.yaml`.

Test ranges with missing weekdays, weekends, and fewer than seven days
at the beginning.

------------------------------------------------------------------------

## 9. Concurrency requirements

The grader sends parallel requests. Sequential correctness is not
enough.

  -----------------------------------------------------------------------
  Scenario                            Required behavior
  ----------------------------------- -----------------------------------
  Two simultaneous punch-ins for the  Exactly one returns `201`; the
  same employee and date              others return `409`.

  Two simultaneous punch-outs for the Exactly one returns `200`.
  same record                         

  Concurrent manual regularizations   History entries must not silently
                                      overwrite or lose one another.
  -----------------------------------------------------------------------

Implementation guidance:

-   Use a unique compound index on `(emp_code, date)` to enforce one
    attendance record per employee/date.
-   Handle duplicate-key errors and convert conflicts to the
    contract-required status code.
-   Do not rely on a read-then-write check alone for uniqueness.
-   Use atomic conditional updates for state transitions such as
    punch-out.
-   Choose an atomic update or suitable transaction approach for
    concurrent history updates.
-   Test with actual parallel requests, not only sequential unit tests.

------------------------------------------------------------------------

## 10. Indexes and performance

The seeded database starts with only the default `_id` index. Create
needed indexes at application startup and do so idempotently.

### Index design process

1.  List the real filters and sort orders used by each list/analytics
    query.
2.  Identify equality filters, range filters, and sort keys.
3.  Design indexes for those query shapes.
4.  Create indexes automatically at startup.
5.  Use the explain endpoint to inspect actual plans.
6.  Test with approximately 100,000 attendance records.
7.  Adjust indexes based on the plans and the real contract.

Potential index candidates to evaluate against the actual query shapes:

-   Unique employee index on `emp_code`.
-   Employee listing index involving `department` and `emp_code`.
-   Unique attendance index on `(emp_code, date)`.
-   Attendance listing index beginning with fields commonly used for
    date-range or employee filters.
-   Additional indexes only when justified by actual query shapes and
    explain plans.

These are design candidates, not a guaranteed final index specification.
Check `DATA_MODEL.md`, the actual queries, and the execution plans
before deciding.

Avoid hard-coding database connection details. The grader supplies
`MONGO_URI` and `MONGO_DB`.

------------------------------------------------------------------------

## 11. Part C --- Explain endpoint

Implement:

``` text
GET /admin/explain/{endpoint}
```

Allowed `{endpoint}` identifiers:

-   `attendance_list`
-   `employee_monthly`
-   `department_summary`
-   `late_leaderboard`
-   `department_trend`

Requirements:

-   Run MongoDB explain with **`executionStats` verbosity**.
-   Explain the endpoint's main query or pipeline.
-   Build the query/pipeline the same way as the real endpoint,
    including the same filters, sort, and pagination where applicable.
-   Accept the same query parameters as the endpoint being explained.
-   Return the raw explain output.
-   Verify plans against the 100,000-record dataset.
-   The grader checks that plans use an index scan (`IXSCAN`) and
    contain no collection scan (`COLLSCAN`).

Do not assume that creating an index guarantees it will be used. Confirm
the actual execution plan.

------------------------------------------------------------------------

## 12. Complete `DECISIONS.md`

Answer the five questions in the provided template, in your own words.
The target is about **150--200 words total**.

1.  Which indexes did you create, and why?
2.  What exactly happens when two identical punch-ins arrive at the same
    instant?
3.  What does the leaderboard return for a tie at the cutoff?
4.  How does the department summary count employees with no logs?
5.  What would you change for 100 times the data?

Make the answers consistent with the implementation you actually submit.
Be prepared to explain these decisions in a live walkthrough.

------------------------------------------------------------------------

## 13. Testing strategy

The provided sample contains only nine documents. Passing those samples
is not enough; build your own tests for awkward cases.

### Required test-data scenarios

-   Employee with no attendance logs
-   Employees tied on late totals
-   Weekend attendance records
-   Employee joining mid-month
-   Overnight shift
-   Missing weekday record
-   `ABSENT` and `LEAVE` records
-   Record with missing `history`
-   Record with missing `half_day`
-   Record with no punch-out yet
-   Timestamp at a rounding/grace-period boundary
-   Concurrent duplicate punch-ins
-   Concurrent punch-outs
-   Concurrent regularizations
-   Dataset with approximately 100,000 attendance records

### Boundary tests worth writing

-   Punch-in exactly 10 minutes after shift start vs. one second later.
-   Overtime just below 30 minutes vs. exactly 30 minutes.
-   Work duration around the 4.50-hour half-day threshold.
-   Overnight shift around midnight.
-   Month start/end and IST date boundaries.
-   Page 1, later pages, default page size, maximum page size, and
    invalid values.
-   Leaderboard ties at the result cutoff.
-   Trend start/end dates and missing dates.

### Error behavior

Verify contract-defined behavior for:

-   Unknown employee
-   Missing attendance record
-   Duplicate employee code
-   Duplicate employee/date punch-in
-   Invalid timestamp types or values
-   Invalid request body or query parameter
-   Unsupported explain endpoint identifier

The assignment states that a `500` is always a failed test; expected
client errors must be handled deliberately.

------------------------------------------------------------------------

## 14. Suggested implementation order

Follow this order to reduce rework:

-   [ ] **Step 1:** Read `openapi.yaml` and `DATA_MODEL.md`; note all
    exact fields, types, routes, query parameters, and response shapes.
-   [ ] **Step 2:** Read the entire starter `app/main.py`.
-   [ ] **Step 3:** Set up the environment, MongoDB connection,
    configuration, and health endpoint.
-   [ ] **Step 4:** Implement and test shared time/date conversion,
    timestamp validation, truncation, rounding, and shift calculations.
-   [ ] **Step 5:** Create indexes automatically and idempotently at
    startup.
-   [ ] **Step 6:** Review/fix starter endpoints and record concrete
    findings in `REVIEW.md`.
-   [ ] **Step 7:** Implement punch-out and test overnight shifts and
    concurrent requests.
-   [ ] **Step 8:** Implement PATCH and test recalculation and
    concurrent history preservation.
-   [ ] **Step 9:** Implement employee monthly aggregation.
-   [ ] **Step 10:** Implement department summary, including headcount
    without logs.
-   [ ] **Step 11:** Implement leaderboard ranking and tie behavior.
-   [ ] **Step 12:** Implement department trend gap filling and moving
    average in MongoDB.
-   [ ] **Step 13:** Implement the explain endpoint for all five
    supported identifiers.
-   [ ] **Step 14:** Test all routes against the contract, including
    error cases and pagination.
-   [ ] **Step 15:** Run performance/explain checks on a large dataset.
-   [ ] **Step 16:** Complete `DECISIONS.md` and the short submission
    `README.md`.
-   [ ] **Step 17:** Check repository contents, push to a public Git
    repository, and submit the link as instructed.

------------------------------------------------------------------------

## 15. Final submission checklist

### Runtime and configuration

-   [ ] `pip install -r requirements.txt` works from a clean clone.
-   [ ] `uvicorn app.main:app --port 8000` starts from the repository
    root.
-   [ ] `GET /health` behaves correctly and startup is within 20
    seconds.
-   [ ] `MONGO_URI` and `MONGO_DB` are read from environment variables.
-   [ ] No connection details are hard-coded.
-   [ ] All indexes are created automatically at startup.

### API and business rules

-   [ ] Exact paths, fields, parameters, and status codes match
    `openapi.yaml`.
-   [ ] R1--R10 are covered by tests.
-   [ ] Concurrent punch-in, punch-out, and regularization behavior is
    correct.
-   [ ] Pagination and totals are correct.
-   [ ] Analytics are implemented in MongoDB aggregation pipelines.
-   [ ] Awkward cases beyond the nine sample documents are tested.

### Explain, documentation, and repository

-   [ ] Explain endpoint supports all five identifiers.
-   [ ] Explain plans use `IXSCAN` and contain no `COLLSCAN` for the
    required tested queries.
-   [ ] `REVIEW.md` contains precise findings, examples, and fixes.
-   [ ] `DECISIONS.md` answers all five questions in about 150--200
    words total.
-   [ ] `README.md` briefly explains how to run the project and anything
    reviewers should know.
-   [ ] No `.env`, secrets, Dockerfile, virtual environment, or data
    dumps are committed.
-   [ ] The Git repository is public.
-   [ ] The repository link is submitted by the deadline and as
    specified in the hiring email.

------------------------------------------------------------------------

## 16. Final reminders

-   The API contract is exact: wrong field names and status codes can
    fail automated tests.
-   Do not stop after the happy path works.
-   Do not calculate analytics by loading all records into Python.
-   Do not use array position as the leaderboard rank.
-   Do not drop employees without logs from department headcount.
-   Do not fill trend dates in a Python loop.
-   Do not assume indexes are being used---prove it through explain
    output.
-   Do not treat the nine sample documents as sufficient test coverage.
-   Be prepared to explain your code and make a small change during the
    live follow-up session.

**Source basis:** This guide consolidates the requirements stated in
`PROBLEM_STATEMENT.docx`. Exact schemas and any details not specified in
that document must be resolved using the other candidate-kit files,
especially `openapi.yaml`, `DATA_MODEL.md`, and `app/main.py`.
