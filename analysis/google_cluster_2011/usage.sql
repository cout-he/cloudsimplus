-- Clip usage to the matching successful running interval on the same machine.
-- Remove time overlap per resource for the eligibility duration and MI integral.
CREATE TEMP TABLE candidate_samples AS
WITH matching AS (
    SELECT u.job_id, u.task_index, greatest(u.start_t, c.success_start) AS t0,
        least(u.end_t, c.success_end) AS t1,
        u.cpu_mean, u.mem_mean, u.cpu_max, u.mem_max
    FROM task_usage u
    JOIN batch_candidates c ON (u.job_id, u.task_index, u.machine_id) =
        (c.job_id, c.task_index, c.success_machine)
    WHERE c.success_start >= usage_start() AND c.success_end <= usage_end()
        AND u.start_t < c.success_end AND u.end_t > c.success_start AND u.end_t > u.start_t AND u.end_t - u.start_t <= 300000000
), covered AS (
    SELECT *,
        greatest(t0, coalesce(max(t1) FILTER (WHERE cpu_mean IS NOT NULL) OVER previous, t0)) AS cpu_t0,
        greatest(t0, coalesce(max(t1) FILTER (WHERE mem_mean IS NOT NULL) OVER previous, t0)) AS mem_t0
    FROM matching
    WINDOW previous AS (
        PARTITION BY job_id, task_index
        ORDER BY t0, t1, cpu_mean, mem_mean, cpu_max, mem_max
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
)
SELECT * FROM covered;

CREATE TEMP TABLE candidate_usage AS
SELECT job_id, task_index,
    sum(greatest(0, t1 - cpu_t0)) FILTER (WHERE cpu_mean IS NOT NULL) / 1000000.0 AS cpu_covered_s,
    sum(greatest(0, t1 - mem_t0)) FILTER (WHERE mem_mean IS NOT NULL) / 1000000.0 AS mem_covered_s,
    sum(cpu_mean * greatest(0, t1 - cpu_t0)) / 1000000.0 AS cpu_integral,
    sum(mem_mean * greatest(0, t1 - mem_t0)) / 1000000.0 AS mem_integral,
    max(cpu_max) AS cpu_peak, max(mem_max) AS mem_peak
FROM candidate_samples
GROUP BY job_id, task_index;

CREATE TEMP TABLE batch_proxy AS
SELECT c.*, u.cpu_covered_s, u.mem_covered_s, u.cpu_integral, u.mem_integral, u.cpu_peak, u.mem_peak
FROM batch_candidates c
JOIN candidate_usage u USING (job_id, task_index)
WHERE u.cpu_covered_s >= 0.9 * c.duration_s AND u.mem_covered_s >= 0.9 * c.duration_s
    AND u.cpu_integral > 0;

-- For the cell aggregate, resource-use intervals are treated as constant rates.
-- Allocate fractional boundary overlaps. Every input record is a machine-level
-- sample and is not joined to potentially incomplete task event history.
CREATE TEMP TABLE usage_bins AS
WITH usage_in_window AS (
    SELECT 'observed_tasks' AS cohort, greatest(start_t, usage_start()) AS t0,
        least(end_t, usage_end()) AS t1, cpu_mean, mem_mean,
        greatest(start_t, usage_start()) AS cpu_t0, greatest(start_t, usage_start()) AS mem_t0
    FROM task_usage
    WHERE start_t < usage_end() AND end_t > usage_start() AND end_t > start_t AND end_t - start_t <= 300000000
    UNION ALL
    SELECT 'batch_proxy', t0, t1, cpu_mean, mem_mean, cpu_t0, mem_t0
    FROM candidate_samples
    SEMI JOIN batch_proxy USING (job_id, task_index)
), time_slices AS (
    SELECT cohort, bin, cpu_mean, mem_mean,
        greatest(0, least(t1, bin + 300000000) - greatest(cpu_t0, bin)) AS cpu_overlap,
        greatest(0, least(t1, bin + 300000000) - greatest(mem_t0, bin)) AS mem_overlap
    FROM (
        SELECT *, (t0 // 300000000 + offset_bins) * 300000000 AS bin
        FROM usage_in_window
        CROSS JOIN (VALUES (0), (1)) AS offsets(offset_bins)
    ) possible_bins
    WHERE bin < t1
)
SELECT cohort, bin, count(*) AS usage_records,
    sum(cpu_mean * cpu_overlap) / 1000000.0 AS cpu_area,
    sum(mem_mean * mem_overlap) / 1000000.0 AS mem_area,
    sum(cpu_overlap) FILTER (WHERE cpu_mean IS NOT NULL) / 1000000.0 AS cpu_sample_task_s,
    sum(mem_overlap) FILTER (WHERE mem_mean IS NOT NULL) / 1000000.0 AS mem_sample_task_s
FROM time_slices
GROUP BY cohort, bin;