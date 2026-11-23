-- Event codes follow the Google 2011 v2.1 schema.
CREATE TEMP TABLE event_history AS
SELECT t, missing_info, job_id, task_index, machine_id, event_type, different_machine, part, ordinality,
    last_value(cpu_request IGNORE NULLS) OVER history AS cpu_at_event,
    last_value(mem_request IGNORE NULLS) OVER history AS mem_at_event,
    last_value(scheduling_class IGNORE NULLS) OVER history AS class_at_event,
    last_value(priority IGNORE NULLS) OVER history AS priority_at_event,
    last_value(CASE WHEN event_type <= 6 THEN event_type END IGNORE NULLS)
        OVER history AS state_after,
    lead(t) OVER ordered_events AS next_t
FROM task_events
WHERE t IS NOT NULL AND job_id IS NOT NULL AND task_index IS NOT NULL
    AND event_type BETWEEN 0 AND 8
WINDOW
    ordered_events AS (PARTITION BY job_id, task_index ORDER BY t, part, ordinality),
    history AS (ordered_events ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW);

CREATE TEMP TABLE task_counts AS
SELECT job_id, task_index, count(*) AS event_rows,
    min(t) FILTER (WHERE event_type = 0) AS first_submit,
    arg_min(missing_info IS NOT NULL, (t, part, ordinality))
        FILTER (WHERE event_type = 0) AS first_submit_inferred,
    arg_min(class_at_event, (t, part, ordinality)) FILTER (WHERE event_type IN (0, 1, 7)) AS first_class,
    arg_min(priority_at_event, (t, part, ordinality)) FILTER (WHERE event_type IN (0, 1, 7)) AS first_priority,
    arg_min(cpu_at_event, (t, part, ordinality)) FILTER (WHERE event_type IN (0, 1, 7)) AS first_cpu,
    arg_min(mem_at_event, (t, part, ordinality)) FILTER (WHERE event_type IN (0, 1, 7)) AS first_mem,
    arg_max(event_type, (t, part, ordinality)) FILTER (WHERE event_type <= 6 AND t < event_end()) AS last_state,
    count(*) FILTER (WHERE event_type = 0) AS submits,
    count(*) FILTER (WHERE event_type = 1) AS schedules,
    count(*) FILTER (WHERE event_type = 2) AS evictions,
    count(*) FILTER (WHERE event_type = 3) AS failures,
    count(*) FILTER (WHERE event_type = 4) AS finishes,
    count(*) FILTER (WHERE event_type = 5) AS kills,
    count(*) FILTER (WHERE event_type = 6) AS losses,
    count(*) FILTER (WHERE event_type = 8) AS running_updates,
    count(*) FILTER (WHERE missing_info IS NOT NULL) AS inferred_rows,
    bool_or(different_machine) AS different_machine
FROM event_history
GROUP BY job_id, task_index;

CREATE TEMP TABLE attempts AS
WITH transitions AS (
    SELECT *,
        lag(CASE WHEN event_type = 0 THEN t END) OVER sorted AS submitted,
        lag(missing_info) OVER sorted AS submit_inferred,
        lead(t) OVER sorted AS stopped,
        lead(event_type) OVER sorted AS outcome,
        lead(missing_info) OVER sorted AS stop_inferred
    FROM event_history
    WHERE event_type <= 6
    WINDOW sorted AS (PARTITION BY job_id, task_index ORDER BY t, part, ordinality)
)
SELECT job_id, task_index, machine_id, submitted, t AS started, stopped, outcome,
    cpu_at_event AS cpu_request, mem_at_event AS mem_request,
    class_at_event AS scheduling_class, priority_at_event AS priority,
    submit_inferred IS NOT NULL OR missing_info IS NOT NULL OR stop_inferred IS NOT NULL AS inferred_attempt,
    (stopped - t) / 1000000.0 AS duration_s
FROM transitions
WHERE event_type = 1;

CREATE TEMP TABLE tasks AS
WITH task_details AS (
    SELECT job_id, task_index, count(*) AS complete_attempts,
        arg_max(duration_s, stopped) FILTER (WHERE outcome = 4) AS duration_s,
        arg_max(started, stopped) FILTER (WHERE outcome = 4) AS success_start,
        arg_max(stopped, stopped) FILTER (WHERE outcome = 4) AS success_end,
        arg_max(machine_id, stopped) FILTER (WHERE outcome = 4) AS success_machine,
        arg_max(cpu_request, stopped) FILTER (WHERE outcome = 4) AS cpu_at_start,
        arg_max(mem_request, stopped) FILTER (WHERE outcome = 4) AS mem_at_start,
        arg_max(scheduling_class, stopped) FILTER (WHERE outcome = 4) AS class_at_start
    FROM attempts
    WHERE submitted >= analysis_start() AND stopped < event_end()
        AND submitted <= started AND stopped > started AND outcome BETWEEN 2 AND 6
        AND NOT inferred_attempt AND outcome <> 6
    GROUP BY job_id, task_index
)
SELECT task_counts.*, count(*) OVER (PARTITION BY job_id) AS tasks_in_job,
    coalesce(complete_attempts, 0) AS complete_attempts,
    duration_s, success_start, success_end, success_machine, cpu_at_start, mem_at_start, class_at_start,
    coalesce(cpu_at_start, first_cpu) AS cpu_request,
    coalesce(mem_at_start, first_mem) AS mem_request,
    coalesce(class_at_start, first_class) AS scheduling_class,
    first_priority AS priority
FROM task_counts
LEFT JOIN task_details USING (job_id, task_index);

CREATE TEMP TABLE batch_candidates AS
SELECT job_id, task_index, success_start, success_end, success_machine,
    first_submit, duration_s, cpu_request, mem_request, scheduling_class, priority
FROM tasks
WHERE tasks_in_job = 1 AND submits = 1 AND schedules = 1 AND finishes = 1
    AND evictions = 0 AND failures = 0 AND kills = 0 AND losses = 0
    AND inferred_rows = 0 AND running_updates = 0 AND NOT coalesce(different_machine, false)
    AND duration_s > 0 AND class_at_start IN (0, 1)
    AND cpu_at_start > 0 AND mem_at_start > 0
    AND NOT EXISTS (SELECT 1 FROM task_constraints tc
        WHERE tc.job_id = tasks.job_id AND tc.task_index = tasks.task_index);

CREATE TEMP TABLE running_segments AS
SELECT job_id, task_index, greatest(t, analysis_start()) AS started,
    least(coalesce(next_t, event_end()), event_end()) AS stopped,
    cpu_at_event AS cpu_request, mem_at_event AS mem_request
FROM event_history
WHERE state_after = 1 AND t < event_end()
    AND least(coalesce(next_t, event_end()), event_end()) > greatest(t, analysis_start());