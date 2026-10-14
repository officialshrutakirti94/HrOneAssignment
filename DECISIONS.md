# Decisions

1. **Indexes.** I added a unique index on `employees.emp_code` so employee codes stay unique, and one on `attendance_logs.emp_code` plus `date` for the one-record-per-day rule and employee-month lookups. The date/employee index supports attendance date filtering and sorting. Employee department/join-date and join-date indexes support department summaries and daily headcount. I considered a separate index on `status`, but skipped it because status has few values and the date-based queries are more useful.

2. **Punch-in race.** Both requests can check the employee and prepare a record, but only one insert can win the unique attendance index. MongoDB rejects the other insert, and the API returns 409 for that request.

3. **Ties.** The leaderboard uses MongoDB's competition rank on total late minutes. Everyone tied at or above the cutoff rank is returned, even if that makes the response longer than `limit`; employee code makes the display order predictable.

4. **Headcount.** The department summary starts with employees, not attendance logs, and looks up each employee's logs. That way an employee with no logs still contributes to headcount, as long as they joined by the end of the month.

5. **100x more data.** I would measure the slowest aggregations first, then consider maintaining daily summary documents so dashboards do not repeatedly scan raw attendance history.
