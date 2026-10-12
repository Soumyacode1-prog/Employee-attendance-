# DECISIONS.md

1. **Indexes.** The unique `employees.emp_code` index enforces identity. The unique `attendance_logs.emp_code + date` index prevents duplicate daily records. Date/employee supports listing; employee/punch-in supports finding the latest open punch. Department/join-date indexes support analytics. I rejected indexes on every derived metric because they would add write cost without helping the required filters.

2. **Punch-in race.** Both requests calculate the same attendance date and attempt an insert. MongoDB allows only one insert because of the unique employee/date index. That request returns 201. The other receives `DuplicateKeyError`, which the API converts to 409. There is no check-then-insert race.

3. **Ties.** The leaderboard uses `$rank`, so tied totals share a rank and the next rank is skipped. Filtering happens after ranking, meaning all employees tied at the cutoff rank are returned. Rows are then ordered by total late minutes and `emp_code`.

4. **Headcount.** Department summary starts with employees whose `joined_on` is within the reporting period, then looks up attendance logs. The aggregation preserves employees with an empty log array before grouping, so employees with no logs still contribute one to headcount.

5. **At 100x the data.** I would precompute daily attendance facts or rollups and move heavy analytics to a read-optimized collection, while retaining the raw audit records as the source of truth. I would also review compound indexes using explain statistics and introduce bounded query timeouts.
