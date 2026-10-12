# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `parse_epoch_ms` and request models | Timestamp handling must reject seconds, floats, strings, and preserve whole-second truncation at the API boundary. | Send a float, a seconds value, or milliseconds with fractional seconds. | Validate integer epoch milliseconds and truncate datetimes to whole seconds before calculations and storage. |
| 2 | `attendance_date` | Overnight shifts must assign a punch before `shift_end` to the previous attendance day. | Punch in at 02:00 for a 22:00–06:00 shift. | Compare the IST local time with the overnight shift end and subtract one calendar day. |
| 3 | `compute_late_minutes` | The grace period is strict: exactly 10 minutes is not late, but 10 minutes and 1 second is late; late minutes are measured from shift start. | Test 09:40:00, 09:40:01, and 10:15:59 for a 09:30 shift. | Apply the strict `> 10` rule and floor elapsed minutes from shift start. |
| 4 | `compute_work_hours` | Work hours require decimal half-up rounding, not binary float or banker rounding. | Use durations ending exactly at a half-centesimal boundary. | Calculate with `Decimal` and `ROUND_HALF_UP`. |
| 5 | `punch-in` | Duplicate requests must be race-safe; a check-then-insert is insufficient. | Send parallel punch-ins for one employee and date. | Use the unique `(emp_code, date)` index and convert `DuplicateKeyError` to 409. |
| 6 | `punch-out` | Two simultaneous punch-outs must not both succeed. | Send parallel punch-outs for one open record. | Update with `punch_out: None` in the atomic filter and return 409 for the loser. |
| 7 | `regularize_attendance` | Corrections must append history and must not silently overwrite a concurrent correction. | Send two PATCH requests against the same record. | Match the previous history length and return 409 when the document changed. |
| 8 | `regularize_attendance` | Derived fields must be recomputed after every correction, including clearing fields for ABSENT and LEAVE. | Change a present record to LEAVE, or change punch times. | Build the final state first, then recompute all derived values from it. |
| 9 | attendance listing | Date ranges are inclusive and an inverted range is a validation error. | Request `date_from` later than `date_to`. | Validate the range before querying and use inclusive `$gte`/`$lte` bounds. |
| 10 | analytics design | Analytics must read stored derived fields and count employees with no logs. | Seed an employee without attendance records and compare department headcount. | Start department aggregation from employees and use stored log values. |
| 11 | analytics pagination and ranking | Leaderboard limit is applied after standard competition ranking; array position is not a rank. | Create a tie at the cutoff. | Use MongoDB `$setWindowFields` with `$rank`, then filter by rank. |
| 12 | trend analytics | A daily trend must include weekends and days with no records. | Query a range containing an empty weekday and a weekend. | Use MongoDB `$densify` and compute the moving average with a window stage. |

The existing unique indexes, atomic update filters, IST conversion helpers, and Decimal rounding approach were retained because they match the contract when used with the endpoint validation described above.

Also note anything you looked at and decided was **not** a defect, and why.
