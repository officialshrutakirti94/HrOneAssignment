# Review of `app/main.py`

This file records the issues I found in the starter code, how I noticed them, and
what I changed to fix them.

| # | Where | What was wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `/health` | The health check could say the API was healthy without checking whether MongoDB was reachable. | I compared the endpoint with the contract and noticed that it needs to return an error when the database is unavailable. | I added a MongoDB `ping`. If the ping fails, I return `503 Service Unavailable`; if it succeeds, I return `{"status": "ok"}`. |
| 2 | `EmployeeIn` / `create_employee()` | Employee input was not being checked against the expected formats, `created_at` was not being set and returned in the API format, and duplicate employee codes could be inserted. | I compared the request and response behavior with the contract and checked what happened with invalid employee details or a repeated `emp_code`. | I added checks for the employee code, email, shift times, and `joined_on` date. I set `created_at` on the server and return it as epoch milliseconds. I also added a unique MongoDB index for `emp_code` and handle a duplicate-key error by returning `409 Conflict`. |
| 3 | `list_employees()` | Pagination skipped the wrong number of records, and the result was not sorted by `emp_code`. The total also needs to use the same department filter as the list. | I checked the first page with `page=1` and saw that the skip amount should be zero. I also compared the filtering, count, and sort behavior with the contract. | I changed the skip calculation to `(page - 1) * page_size`, count employees using the same filter, and sort the results by `emp_code` ascending. I also validate the page and page-size values. |
| 4 | `PunchInIn` / `punch_in()` | A punch-in should work when `punched_at` is omitted, and the timestamp, status, attendance date, and late minutes need to follow the contract's rules. Duplicate punch-ins also need protection when requests happen at the same time. | I considered missing or invalid timestamps, invalid statuses, overnight shifts, punches near midnight, and duplicate requests for the same employee and attendance date. | I made `punched_at` optional and strictly checked its type and allowed range. I default it to the current UTC time when it is omitted, calculate the attendance date using IST and the overnight-shift rule, and calculate late minutes relative to the shift start with the 10-minute grace period. I restricted the status to `PRESENT`, `WFH`, or `ON_DUTY`. I added a unique index on `emp_code` and `date` and return `409 Conflict` when a duplicate punch-in is attempted. |
| 5 | `list_attendance()` | The original implementation did not return the total number of all matching records correctly, and its response could expose MongoDB-specific fields instead of the API response shape. | I checked the query and response behavior with filters and pagination, then compared the returned fields with the attendance contract. | I count all records matching the filters before pagination, sort by date descending and `emp_code` ascending, and apply pagination in MongoDB. I build the response fields explicitly, convert punch timestamps to epoch milliseconds, and do not return MongoDB's `_id`. I also reject a `date_from` value later than `date_to` and validate pagination. |
| 6 | `patch_attendance()` | The PATCH route was only a stub, so attendance records could not be corrected according to the regularization contract. | I compared the route with the OpenAPI rules for optional correction fields, time/status validation, recomputed fields, and the audit history. | I implemented lookup by `emp_code` and attendance date, validation of the final status and punch times, IST attendance-date checks, and recalculation of late minutes, work hours, overtime, and half-day. I append one history entry containing only the fields that actually changed, convert timestamp values in the response to epoch milliseconds, reject no-op corrections with 422, and use an atomic history match so a concurrent correction returns 409 instead of overwriting another correction. |

## Issues I have not finished

The following contract endpoints are still not implemented in `app/main.py`:

- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

I checked the PATCH flow with focused execution checks using a fake database for
punch-time correction, derived-field recomputation, status change to `ABSENT`, and
no-op rejection. I still need to test the full API against MongoDB and the complete
contract, including concurrency and edge cases.
