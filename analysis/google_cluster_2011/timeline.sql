-- Sweep state changes and minute boundaries together, without expanding
-- running intervals to every bin for every task.
CREATE TEMP TABLE timed_inputs AS
SELECT 'observed_tasks' AS cohort, started, stopped, cpu_request, mem_request
FROM running_segments
UNION ALL
SELECT 'batch_proxy', success_start, success_end, cpu_request, mem_request
FROM batch_proxy
UNION ALL
SELECT 'submit_immediate_proxy', first_submit, first_submit + success_end - success_start,
    cpu_request, mem_request
FROM batch_proxy;

CREATE TEMP TABLE minute_events AS
WITH edges AS (
    SELECT cohort, started AS t, 1 AS run_delta, coalesce(cpu_request, 0) AS cpu_delta,
        coalesce(mem_request, 0) AS mem_delta, (cpu_request IS NULL)::INTEGER AS no_cpu_delta,
        (mem_request IS NULL)::INTEGER AS no_mem_delta
    FROM timed_inputs
    UNION ALL
    SELECT cohort, stopped, -1, -coalesce(cpu_request, 0), -coalesce(mem_request, 0),
        -(cpu_request IS NULL)::INTEGER, -(mem_request IS NULL)::INTEGER
    FROM timed_inputs
    UNION ALL
    SELECT names.cohort, tick, 0, 0, 0, 0, 0
    FROM range((analysis_start() // 60000000) * 60000000, event_end() + 60000000, 60000000) r(tick)
    CROSS JOIN (VALUES ('observed_tasks'), ('batch_proxy'), ('submit_immediate_proxy')) names(cohort)
), summed AS (
    SELECT cohort, t, sum(run_delta) AS run_delta, sum(cpu_delta) AS cpu_delta,
        sum(mem_delta) AS mem_delta, sum(no_cpu_delta) AS no_cpu_delta,
        sum(no_mem_delta) AS no_mem_delta
    FROM edges
    GROUP BY cohort, t
), sweep AS (
    SELECT cohort, t, lead(t) OVER timeline - t AS delta_t,
        sum(run_delta) OVER timeline AS concurrent,
        sum(cpu_delta) OVER timeline AS cpu,
        sum(mem_delta) OVER timeline AS mem,
        sum(no_cpu_delta) OVER timeline AS no_cpu,
        sum(no_mem_delta) OVER timeline AS no_mem
    FROM summed
    WINDOW timeline AS (PARTITION BY cohort ORDER BY t ROWS UNBOUNDED PRECEDING)
)
SELECT cohort, (t // 60000000) * 60000000 AS bin,
    sum(delta_t * concurrent) / 1000000.0 AS running_task_s,
    max(concurrent) FILTER (WHERE delta_t > 0) AS running_tasks_peak,
    greatest(0, sum(delta_t * cpu) / 1000000.0) AS cpu_area,
    greatest(0, sum(delta_t * mem) / 1000000.0) AS mem_area,
    greatest(0, max(cpu)) AS cpu_requested_peak,
    greatest(0, max(mem)) AS mem_requested_peak,
    sum(delta_t * no_cpu) / 1000000.0 AS cpu_missing_request_task_s,
    sum(delta_t * no_mem) / 1000000.0 AS mem_missing_request_task_s
FROM sweep
WHERE delta_t > 0
GROUP BY cohort, bin;

CREATE TEMP TABLE minute_arrivals AS
SELECT 'observed_tasks' AS cohort, first_submit // 60000000 * 60000000 AS bin,
    count(*) AS new_tasks
FROM tasks
WHERE first_submit >= analysis_start() AND first_submit < event_end() AND NOT first_submit_inferred
GROUP BY bin
UNION ALL
SELECT names.cohort, first_submit // 60000000 * 60000000 AS bin, count(*)
FROM batch_proxy
CROSS JOIN (VALUES ('batch_proxy'), ('submit_immediate_proxy')) names(cohort)
GROUP BY names.cohort, bin;