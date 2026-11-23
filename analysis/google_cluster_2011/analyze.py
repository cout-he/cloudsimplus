"""Reconstruct verified Google Cluster Trace 2011 v2.1 workload statistics."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import math
from pathlib import Path
import re

import duckdb

from task_use_partition import AGGREGATE, CLIPPED, build_task_use


BASE = Path(__file__).resolve().parent
SECOND = 1_000_000
TRACE_FIRST = 600 * SECOND
MAXINT = 2**63 - 1
UINT_MAX = 2**64 - 1
PART = re.compile(r"part-(\d{5})-of-(\d{5})\.csv\.gz")
COHORTS = ("observed_tasks", "batch_proxy", "submit_immediate_proxy")
SETTINGS = {
    "task_events": "t:U missing_info:I job_id:L task_index:L machine_id:L event_type:I "
                   "user:S scheduling_class:I priority:I cpu_request:D mem_request:D disk_request:D different_machine:B",
    "task_usage": "start_t:L end_t:L job_id:L task_index:L machine_id:L cpu_mean:D mem_mean:D "
                  "mem_assigned:D mem_unmapped_cache:D mem_total_cache:D mem_max:D disk_io_mean:D "
                  "disk_space:D cpu_max:D disk_io_max:D cpi:D mai:D sample_portion:D aggregation_type:B cpu_sample:D",
    "job_events": "t:U missing_info:I job_id:L event_type:I user:S scheduling_class:I job_name:S logical_job_name:S",
    "machine_events": "t:U machine_id:L event_type:I platform:S cpu_capacity:D mem_capacity:D",
    "machine_attributes": "t:U machine_id:L attribute_name:S attribute_value:S deleted:B",
    "task_constraints": "t:U job_id:L task_index:L operator:I attribute_name:S attribute_value:S",
}
PROJECT = {
    "task_events": ("t", "missing_info", "job_id", "task_index", "machine_id", "event_type",
                    "scheduling_class", "priority", "cpu_request", "mem_request", "different_machine"),
    "task_usage": ("start_t", "end_t", "job_id", "task_index", "machine_id", "cpu_mean", "mem_mean",
                   "mem_max", "cpu_max", "sample_portion", "aggregation_type"),
    "job_events": ("t", "missing_info", "job_id", "event_type", "scheduling_class", "logical_job_name"),
    "machine_events": ("t", "machine_id", "event_type", "platform", "cpu_capacity", "mem_capacity"),
    "machine_attributes": ("t", "machine_id", "attribute_name", "deleted"),
    "task_constraints": ("t", "job_id", "task_index", "operator"),
}
TYPES = {"L": "BIGINT", "U": "UBIGINT", "I": "INTEGER", "S": "VARCHAR", "D": "DOUBLE", "B": "BOOLEAN"}
DURATIONS = (0, .01, .1, 1, 5, 10, 30, 60, 120, 300, 900, 1800, 3600, 7200, 21600, 86400, 259200)
RESOURCES = (0, .00001, .0001, .0005, .001, .002, .005, .01, .02, .05, .1, .2, .4, .6, .8, 1)


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def write_csv(path, header, rows):
    """Make a completed result visible only after all rows are written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    pending.replace(path)


def verified_snapshot(args):
    contents = args.manifest.read_text(encoding="utf-8-sig")
    records = list(csv.DictReader(io.StringIO(contents)))
    selected = {}
    for table in SETTINGS:
        by_index = {}
        expected = 0
        verified = 0
        for row in records:
            if row["table"] != table:
                continue
            match = PART.fullmatch(Path(row["object"]).name)
            if not match:
                continue
            index, expected = map(int, match.groups())
            path = args.raw / row["object"]
            if (row["status"] == "verified_sha256" and path.is_file()
                    and path.stat().st_size == int(row["expected_bytes"])):
                by_index[index] = (path.resolve(), row["sha256_actual"])
                verified += 1
        limit = {
            "task_events": args.max_event_shards,
            "task_usage": args.max_usage_shards,
            "job_events": args.max_job_shards,
            "task_constraints": args.max_constraint_shards,
        }.get(table)
        paths = []
        for index in range(min(expected, limit) if limit else expected):
            if index not in by_index:
                break
            paths.append(by_index[index])
        selected[table] = (paths, expected, verified)
    return selected, contents


def staged_tables(db, selected, work, stage_workers):
    cache = work / "projected"
    cache.mkdir(parents=True, exist_ok=True)
    for table, (paths, _, _) in selected.items():
        fields = {name: TYPES[code] for name, code in
                  (pair.split(":") for pair in SETTINGS[table].split())}
        output_types = fields | {"t": "BIGINT"}
        casts = []
        for name in PROJECT[table]:
            expression = name
            if name == "t":
                # The released tail uses UINT64_MAX for after-trace events.
                expression = f"CASE WHEN t = {UINT_MAX}::UBIGINT THEN {MAXINT} ELSE t::BIGINT END"
            elif fields[name] == "DOUBLE":
                expression = f"CASE WHEN isfinite({name}) AND {name} >= 0 THEN {name} END"
            casts.append(f"{expression} AS {name}")
        column_types = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in fields.items()) + "}"
        cached_paths = []
        uncached = []
        sequence = ", ordinality" if table in ("task_events", "machine_events") else ""
        for index, (path, checksum) in enumerate(paths):
            input_key = hashlib.sha256(
                f"2|{checksum}|{table}|{index}|{column_types}|{','.join(casts)}".encode()
            ).hexdigest()[:20]
            target = cache / f"{table}-{index:05d}-{input_key}.parquet"
            cached_paths.append(sql_literal(target.as_posix()))
            if not target.exists():
                uncached.append((index, path, target))

        def project(item, connection):
            index, path, target = item
            pending = target.with_suffix(".pending")
            print(f"Projecting {table} part {index + 1}/{len(paths)}", flush=True)
            # Preserve physical order within each event shard for exact timestamp ties.
            csv_read = (
                f"read_csv({sql_literal(path.as_posix())}, columns = {column_types}, "
                "header = false, auto_detect = false, compression = 'gzip', delim = ',', "
                "parallel = false, quote = '', escape = '')"
            )
            if sequence:
                csv_read += " WITH ORDINALITY"
            connection.execute(f"""
                COPY (SELECT {', '.join(casts)}, {index}::INTEGER AS part{sequence}
                      FROM {csv_read})
                TO {sql_literal(pending.as_posix())}
                (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """)
            pending.replace(target)

        def project_in_worker(item):
            with db.cursor() as connection:
                project(item, connection)

        if stage_workers > 1 and len(uncached) > 1:
            with ThreadPoolExecutor(max_workers=stage_workers) as executor:
                for _ in executor.map(project_in_worker, uncached):
                    pass
        else:
            for item in uncached:
                project(item, db)
        if cached_paths:
            source = "SELECT * FROM read_parquet([" + ", ".join(cached_paths) + "])"
        else:
            empty = [f"NULL::{output_types[name]} AS {name}" for name in PROJECT[table]]
            if table in ("task_events", "machine_events"):
                empty.append("NULL::BIGINT AS ordinality")
            source = f"SELECT {', '.join(empty)}, NULL::INTEGER AS part WHERE false"
        db.execute(f"CREATE TEMP VIEW {table} AS {source}")


def coverage(db, args, selected):
    start = args.start * SECOND
    requested_end = args.end * SECOND if args.end else MAXINT
    event_last, = db.execute(
        "SELECT max(t) FROM task_events WHERE t >= ? AND t < ?", [TRACE_FIRST, MAXINT]
    ).fetchone()
    usage_min, usage_last, usage_last_end = db.execute(
        "SELECT min(start_t), max(start_t), max(end_t) FROM task_usage WHERE end_t > start_t"
    ).fetchone()
    event_end = max(start, min(requested_end, event_last or start))
    usage_start = max(start, usage_min or start)
    usage_full = selected["task_usage"][1] == len(selected["task_usage"][0]) and usage_last is not None
    usage_boundary = usage_last_end if usage_full else usage_last
    usage_end = max(usage_start, min(requested_end, usage_boundary or usage_start))
    for name, value in (("analysis_start", start), ("event_end", event_end),
                        ("usage_start", usage_start), ("usage_end", usage_end)):
        db.execute(f"CREATE TEMP MACRO {name}() AS {value}::BIGINT")
    return start, event_end, usage_start, usage_end


class Statistics:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def add(self, section, cohort, metric, statistic, value, unit, n=None, notes=""):
        self.rows.append((section, cohort, metric, statistic, value, unit, n, notes))

    def counts(self, section, cohort, query, unit="count", notes=""):
        result = self.db.execute(query)
        for column, value in zip(result.description, result.fetchone()):
            self.add(section, cohort, column[0], "count", value, unit, None, notes)

    def distribution(self, section, cohort, metric, source, unit, notes="", exact=False):
        quantile = "quantile_cont" if exact else "approx_quantile"
        probability_type = "DOUBLE" if exact else "FLOAT"
        total, mean, quantiles, maximum = self.db.execute(f"""
            SELECT count(x), avg(x), {quantile}(x, [0.5, 0.75, 0.9, 0.95, 0.99]::{probability_type}[]), max(x)
            FROM ({source}) measurements
            WHERE x IS NOT NULL
        """).fetchone()
        values = (mean, *(quantiles or [None] * 5), maximum)
        for label, value in zip(("mean", "p50", "p75", "p90", "p95", "p99", "max"), values):
            self.add(section, cohort, metric, label, value, unit, total, notes)

    def categorical(self, section, cohort, metric, source):
        for value, count, total in self.db.execute(f"""
            SELECT x, count(*), sum(count(*)) OVER () FROM ({source}) data GROUP BY x ORDER BY x
        """).fetchall():
            name = f"{metric}_{'missing' if value is None else value}"
            self.add(section, cohort, name, "count", count, "count", total)
            self.add(section, cohort, name, "share", count / total, "fraction", total)

    def fraction(self, section, cohort, metric, numerator, denominator, notes=""):
        self.add(section, cohort, metric, "share", numerator / denominator if denominator else None,
                 "fraction", denominator, notes)
def task_statistics(stats, tables, windows, partition_options=None):
    db = stats.db
    start, event_end, usage_start, usage_end = windows
    db.execute("""
        CREATE TEMP VIEW task_usage_measures AS
        SELECT * FROM task_usage
        WHERE start_t < usage_end() AND end_t > usage_start() AND end_t > start_t
            AND start_t >= 0 AND end_t - start_t <= 300000000
    """)
    if partition_options:
        build_task_use(db, *partition_options)
    else:
        query = AGGREGATE.format(relation=f"({CLIPPED})")
        db.execute(f"CREATE TEMP TABLE task_use AS {query}")
    for table, (paths, expected, verified) in tables.items():
        stats.add("coverage", table, "expected_shards", "count", expected, "shards")
        stats.add("coverage", table, "verified_shards_in_snapshot", "count", verified, "shards")
        stats.add("coverage", table, "analyzed_prefix_shards", "count", len(paths), "shards",
                  notes="Longest gap-free verified prefix from part 00000, subject to command limits.")
    for table, low, high in (
        ("task_events", start, event_end), ("task_usage", usage_start, usage_end),
        ("joint", max(start, usage_start), max(max(start, usage_start), min(event_end, usage_end)))
    ):
        stats.add("coverage", table, "window_start", "value", low // SECOND, "trace_seconds")
        stats.add("coverage", table, "window_end_exclusive", "value", high / SECOND, "trace_seconds")
        stats.add("coverage", table, "window_duration", "value", (high - low) / SECOND, "seconds")

    if tables["task_events"][0]:
        stats.counts("inventory", "observed_tasks", """
            SELECT count(*) AS tasks, count(DISTINCT job_id) AS jobs_in_task_events,
                count(*) FILTER (WHERE complete_attempts > 0) AS complete_lifecycle_tasks,
                count(*) FILTER (WHERE duration_s IS NOT NULL) AS complete_successful_tasks,
                count(*) FILTER (WHERE submits >= 2) AS tasks_with_resubmission,
                count(*) FILTER (WHERE first_submit = 0) AS tasks_left_censored,
                count(*) FILTER (WHERE first_submit IS NULL) AS tasks_without_submit,
                count(*) FILTER (WHERE last_state IN (0, 1)) AS tasks_right_censored,
                count(*) FILTER (WHERE inferred_rows > 0) AS tasks_with_inferred_event
            FROM tasks
        """, notes="Unique job ID and task index over the analyzed event shards, including time-zero state.")
        stats.counts("quality", "observed_tasks", """
            SELECT count(*) AS event_records,
                count(*) FILTER (WHERE missing_info IS NOT NULL) AS inferred_event_records,
                count(*) FILTER (WHERE t IS NULL OR job_id IS NULL OR task_index IS NULL) AS event_records_without_key,
                count(*) FILTER (WHERE NOT coalesce(event_type BETWEEN 0 AND 8, false)) AS unknown_event_types,
                count(*) FILTER (WHERE t = 9223372036854775807) AS after_trace_sentinel_events
            FROM task_events
        """)
        total, missing_cpu, missing_mem, inferred, events, no_class, no_priority = db.execute("""
            SELECT count(*), count(*) FILTER (WHERE cpu_request IS NULL),
                count(*) FILTER (WHERE mem_request IS NULL), sum(inferred_rows), sum(event_rows),
                count(*) FILTER (WHERE scheduling_class IS NULL), count(*) FILTER (WHERE priority IS NULL)
            FROM tasks
        """).fetchone()
        stats.fraction("quality", "observed_tasks", "missing_cpu_request", missing_cpu, total)
        stats.fraction("quality", "observed_tasks", "missing_mem_request", missing_mem, total)
        stats.fraction("quality", "observed_tasks", "missing_scheduling_class", no_class, total)
        stats.fraction("quality", "observed_tasks", "missing_priority", no_priority, total)
        stats.fraction("quality", "observed_tasks", "inferred_event_records", inferred, events)
        for code, name, column in ((2, "evict", "evictions"), (3, "fail", "failures"),
                                   (4, "finish", "finishes"), (5, "kill", "kills"), (6, "lost", "losses")):
            ev, ever, last = db.execute(
                f"SELECT sum({column}), count(*) FILTER (WHERE {column} > 0), "
                f"count(*) FILTER (WHERE last_state = {code}) FROM tasks"
            ).fetchone()
            stats.add("outcomes", "observed_tasks", name, "event_rows", ev, "events", total)
            stats.add("outcomes", "observed_tasks", name, "tasks_ever", ever, "tasks", total)
            stats.add("outcomes", "observed_tasks", name, "tasks_last_state", last, "tasks", total)
        stats.counts("eligibility", "batch_proxy", """
            SELECT count(*) AS prescreen_candidates,
                count(*) FILTER (WHERE success_start >= usage_start() AND success_end <= usage_end())
                    AS candidates_within_usage_window
            FROM batch_candidates
        """)
        count, = db.execute("SELECT count(*) FROM batch_proxy").fetchone()
        stats.add("eligibility", "batch_proxy", "eligible_tasks", "count", count if usage_end > usage_start else None,
                  "tasks", notes="Observed-singleton jobs; one natural successful fixed-request attempt; "
                  "class 0–1; no inferred/retry/constraint event; >=90% same-machine CPU and RAM sample coverage.")
    if tables["job_events"][0]:
        stats.counts("inventory", "job_events", """
            SELECT count(*) AS job_event_records, count(DISTINCT job_id) AS jobs_in_job_events,
                count(*) FILTER (WHERE missing_info IS NOT NULL) AS inferred_job_event_records
            FROM job_events
        """)
    if tables["task_constraints"][0]:
        stats.counts("constraints", "observed_tasks", """
            SELECT count(*) AS placement_constraint_records,
                count(DISTINCT (job_id, task_index)) AS tasks_with_observed_placement_constraints
            FROM task_constraints
        """)
    if tables["task_usage"][0]:
        stats.counts("inventory", "task_usage", """
            SELECT count(*) AS usage_records,
                count(*) FILTER (WHERE end_t IS NULL OR start_t IS NULL OR end_t <= start_t) AS unusable_intervals,
                count(*) FILTER (WHERE end_t - start_t > 300000000) AS intervals_longer_than_300s,
                count(*) FILTER (WHERE cpu_mean IS NULL) AS missing_cpu_mean_records,
                count(*) FILTER (WHERE mem_mean IS NULL) AS missing_mem_mean_records,
                count(*) FILTER (WHERE cpu_max IS NULL) AS missing_cpu_max_records,
                count(*) FILTER (WHERE mem_max IS NULL) AS missing_mem_max_records,
                count(*) FILTER (WHERE aggregation_type) AS approximate_peak_records
            FROM task_usage
        """)
        stats.distribution("usage_quality", "observed_tasks", "sample_portion",
                           "SELECT sample_portion AS x FROM task_usage_measures", "fraction")

    metrics = [
        ("durations", "duration_s", "seconds"),
        ("requests", "cpu_request", "normalized_cpu"),
        ("requests", "mem_request", "normalized_memory"),
        ("usage", "cpu_usage_mean", "normalized_cpu"),
        ("usage", "cpu_usage_max", "normalized_cpu"),
        ("usage", "mem_usage_mean", "normalized_memory"),
        ("usage", "mem_usage_max", "normalized_memory"),
    ]
    sources = {
        "observed_tasks": {
            "durations": "tasks",
            "requests": "tasks",
            "usage": "task_use",
        },
        "batch_proxy": {
            "durations": "batch_proxy",
            "requests": "batch_proxy",
            "usage": """(SELECT cpu_integral / cpu_covered_s AS cpu_usage_mean,
                            mem_integral / mem_covered_s AS mem_usage_mean,
                            cpu_peak AS cpu_usage_max, mem_peak AS mem_usage_max FROM batch_proxy)""",
        },
    }
    distributions = []
    for section, metric, unit in metrics:
        queries = {}
        for cohort, source in sources.items():
            query = f"SELECT {metric} AS x FROM {source[section]} WHERE {metric} IS NOT NULL"
            queries[cohort] = query
            stats.distribution(section, cohort, metric, query, unit, "Approximate quantiles (DuckDB t-digest).")
        edges = DURATIONS if section == "durations" else RESOURCES
        if section == "durations":
            distributions.extend(histograms(db, metric, queries, edges))
            for cohort, query in queries.items():
                total, short, long = db.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE x < 60), "
                    f"count(*) FILTER (WHERE x >= 3600) FROM ({query}) durations"
                ).fetchone()
                stats.fraction("durations", cohort, "duration_lt_60s", short, total)
                stats.fraction("durations", cohort, "duration_ge_3600s", long, total)
    for resource, unit in (("cpu", "normalized_cpu"), ("mem", "normalized_memory")):
        resource_metrics = [item for item in metrics if item[1].startswith(resource)]
        all_queries = [
            f"SELECT {metric} AS x FROM {source[section]} WHERE {metric} IS NOT NULL"
            for section, metric, _ in resource_metrics for source in sources.values()
        ]
        bounds = (*RESOURCES, 2, 5, 10, 20) if resource == "cpu" else RESOURCES
        shared_edges = extend_edges(db, bounds, all_queries)
        for section, metric, _ in resource_metrics:
            queries = {cohort: f"SELECT {metric} AS x FROM {source[section]} WHERE {metric} IS NOT NULL"
                       for cohort, source in sources.items()}
            distributions.extend(histograms(db, metric, queries, shared_edges))

    for cohort, request_table in (("observed_tasks", "tasks"), ("batch_proxy", "batch_proxy")):
        for field in ("scheduling_class", "priority"):
            stats.categorical("mix", cohort, field, f"SELECT {field} AS x FROM {request_table}")
        joined = (f"{request_table} JOIN task_use USING (job_id, task_index)"
                  if cohort == "observed_tasks" else request_table)
        for resource, unit in (("cpu", "normalized_cpu"), ("mem", "normalized_memory")):
            actual = f"{resource}_usage_mean" if cohort == "observed_tasks" else (
                f"{resource}_integral / {resource}_covered_s"
            )
            request = f"{resource}_request"
            notes = ("Paired task keys; request at successful start or first known request; "
                     "observed-task usage can include several attempts.")
            stats.distribution("paired", cohort, f"{resource}_actual_minus_request",
                               f"SELECT {actual} - {request} AS x FROM {joined}", unit, notes)
            stats.distribution("paired", cohort, f"{resource}_actual_over_request",
                               f"SELECT ({actual}) / {request} AS x FROM {joined} WHERE {request} > 0",
                               "ratio", notes)
        correlation, n, low_cpu_high_mem = db.execute(f"""
            SELECT corr(cpu_request, mem_request), count(*),
                count(*) FILTER (WHERE cpu_request <= 0.02 AND mem_request >= 0.1)
            FROM {request_table}
            WHERE cpu_request IS NOT NULL AND mem_request IS NOT NULL
        """).fetchone()
        stats.add("joint", cohort, "cpu_mem_request", "pearson_r",
                  correlation if correlation is not None and math.isfinite(correlation) else None, "ratio", n)
        stats.fraction("joint", cohort, "cpu_le_0.02_mem_ge_0.1", low_cpu_high_mem, n,
                       "Descriptive thresholds in Google-normalized units.")
        cpu_expr = "cpu_usage_mean" if cohort == "observed_tasks" else "cpu_integral / cpu_covered_s"
        mem_expr = "mem_usage_mean" if cohort == "observed_tasks" else "mem_integral / mem_covered_s"
        actual_table = "task_use" if cohort == "observed_tasks" else "batch_proxy"
        correlation, n = db.execute(f"""
            SELECT corr({cpu_expr}, {mem_expr}), count(*)
            FROM {actual_table} WHERE ({cpu_expr}) IS NOT NULL AND ({mem_expr}) IS NOT NULL
        """).fetchone()
        stats.add("joint", cohort, "cpu_mem_actual", "pearson_r",
                  correlation if correlation is not None and math.isfinite(correlation) else None, "ratio", n)
        for resource in ("cpu", "mem"):
            actual = (f"{resource}_usage_mean" if cohort == "observed_tasks"
                      else f"{resource}_integral / {resource}_covered_s")
            peak = f"{resource}_usage_max" if cohort == "observed_tasks" else f"{resource}_peak"
            stats.distribution("temporal_usage", cohort, f"{resource}_peak_over_mean",
                               f"SELECT {peak} / ({actual}) AS x FROM {actual_table} WHERE ({actual}) > 0",
                               "ratio", "Per-task peak among sampled windows over duration-weighted mean. "
                               "Aggregation type 1 peaks may sum subcontainer maxima; ratio is descriptive.")
        if cohort == "observed_tasks":
            stats.distribution("temporal_usage", cohort, "usage_samples_per_task",
                               "SELECT usage_samples AS x FROM task_use", "records")
        joint_envelope(stats, cohort, request_table)
        if cohort == "batch_proxy":
            stats.distribution("work", cohort, "cpu_integral", "SELECT cpu_integral AS x FROM batch_proxy",
                               "normalized_cpu_seconds",
                               "Integral of matched, de-overlapped observed average CPU rate; calibration is required for MI.")
            for resource in ("cpu", "mem"):
                stats.distribution("usage_quality", cohort, f"{resource}_coverage",
                                   f"SELECT {resource}_covered_s / duration_s AS x FROM batch_proxy",
                                   "fraction")
    for scheduling_class in range(4):
        for field, unit in (("cpu_request", "normalized_cpu"), ("mem_request", "normalized_memory"),
                            ("cpu_usage_mean", "normalized_cpu"), ("mem_usage_mean", "normalized_memory")):
            source = ("tasks LEFT JOIN task_use USING (job_id, task_index)" if "_usage_" in field else "tasks")
            stats.distribution("class_resources", f"scheduling_class_{scheduling_class}", field,
                               f"SELECT {field} AS x FROM {source} WHERE scheduling_class={scheduling_class}", unit)

    joint = []
    for cohort, table in (("observed_tasks", "tasks"), ("batch_proxy", "batch_proxy")):
        rows = db.execute(f"""
            SELECT {bin_index('cpu_request', RESOURCES)} AS cpu,
                {bin_index('mem_request', RESOURCES)} AS mem, count(*) AS n
            FROM {table}
            WHERE cpu_request IS NOT NULL AND mem_request IS NOT NULL
            GROUP BY cpu, mem ORDER BY cpu, mem
        """).fetchall()
        cpu_bins = extend_edges(db, RESOURCES, ["SELECT cpu_request AS x FROM tasks"])
        mem_bins = extend_edges(db, RESOURCES, ["SELECT mem_request AS x FROM tasks"])
        joint.extend((cohort, cpu_bins[cpu], cpu_bins[cpu + 1], mem_bins[mem], mem_bins[mem + 1], n)
                     for cpu, mem, n in rows)
    return distributions, joint


def joint_envelope(stats, cohort, request_table):
    """Count paired requests inside envelopes defined by exact marginal quantiles."""
    result = stats.db.execute(f"""
        WITH requests AS (
            SELECT cpu_request AS cpu, mem_request AS mem FROM {request_table}
            WHERE cpu_request IS NOT NULL AND mem_request IS NOT NULL
        ), cutoffs AS (
            SELECT quantile_cont(cpu, [0.5, 0.9, 0.99]) AS c,
                quantile_cont(mem, [0.5, 0.9, 0.99]) AS m,
                count(*) AS total
            FROM requests
        )
        SELECT c, m, total,
            count(*) FILTER (WHERE cpu <= c[1] AND mem <= m[1]) AS both_p50,
            count(*) FILTER (WHERE cpu <= c[2] AND mem <= m[2]) AS both_p90,
            count(*) FILTER (WHERE cpu <= c[3] AND mem <= m[3]) AS both_p99,
            count(*) FILTER (WHERE cpu <= c[1] AND mem >= m[2]) AS low_cpu_high_mem
        FROM cutoffs LEFT JOIN requests ON true
        GROUP BY c, m, total
    """).fetchone()
    cpu, mem, total, p50, p90, p99, low_high = result
    if not total:
        return
    for metric, cpu_limit, mem_limit, count in (
        ("both_le_marginal_p50", cpu[0], mem[0], p50),
        ("both_le_marginal_p90", cpu[1], mem[1], p90),
        ("both_le_marginal_p99", cpu[2], mem[2], p99),
        ("cpu_le_p50_mem_ge_p90", cpu[0], mem[1], low_high),
    ):
        stats.add("joint_envelope", cohort, metric, "cpu_limit", cpu_limit, "normalized_cpu", total)
        stats.add("joint_envelope", cohort, metric, "mem_limit", mem_limit, "normalized_memory", total)
        stats.add("joint_envelope", cohort, metric, "count", count, "tasks", total)
        stats.fraction("joint_envelope", cohort, metric, count, total,
                       "Exact marginal quantiles and inclusive comparison on complete request pairs.")

def extend_edges(db, edges, queries):
    maximum = max((db.execute(f"SELECT max(x) FROM ({q}) values_in_scope").fetchone()[0] or 0
                   for q in queries), default=0)
    if maximum >= edges[-1]:
        return (*edges, max(1.05 * maximum, 1.1 * edges[-1]))
    return edges


def bin_index(column, edges):
    return ("CASE " + " ".join(f"WHEN {column} < {upper} THEN {index}"
                               for index, upper in enumerate(edges[1:]))
            + f" ELSE {len(edges) - 1} END")


def histograms(db, metric, queries, edges):
    edges = extend_edges(db, edges, queries.values())
    for cohort, query in queries.items():
        bins = db.execute(f"""
            SELECT {bin_index('x', edges)} AS bin, count(*)
            FROM ({query}) data
            GROUP BY bin ORDER BY bin
        """).fetchall()
        if not bins:
            continue
        counts = dict(bins)
        for index, (left, right) in enumerate(zip(edges, edges[1:])):
            yield cohort, metric, left, right, counts.get(index, 0)
SERIES_COLUMNS = (
    "cohort", "bin_start_s", "bin_seconds", "new_tasks", "arrival_rate_per_s",
    "running_tasks_avg", "running_tasks_peak", "cpu_requested_avg", "mem_requested_avg",
    "cpu_actual_avg", "mem_actual_avg", "usage_records", "cpu_sample_task_s", "mem_sample_task_s",
    "cpu_missing_request_task_s", "mem_missing_request_task_s", "cpu_requested_peak", "mem_requested_peak",
)


def timeline_statistics(stats, windows):
    db = stats.db
    start, event_end, usage_start, usage_end = (t // SECOND for t in windows)
    minutes = {(cohort, int(t // SECOND)): values
               for cohort, t, *values in db.execute("""
                   SELECT cohort, bin, running_task_s, running_tasks_peak, cpu_area, mem_area,
                       cpu_missing_request_task_s, mem_missing_request_task_s, cpu_requested_peak, mem_requested_peak
                   FROM minute_events
               """).fetchall()}
    usage = {(cohort, int(t // SECOND)): values
             for cohort, t, *values in db.execute("""
                 SELECT cohort, bin, usage_records, cpu_area, mem_area, cpu_sample_task_s, mem_sample_task_s
                 FROM usage_bins
             """).fetchall()}
    arrivals = {(cohort, int(t // SECOND)): n
                for cohort, t, n in db.execute("SELECT cohort, bin, new_tasks FROM minute_arrivals").fetchall()}
    cohort_available, = db.execute("SELECT count(*) FROM batch_proxy").fetchone()
    rows = []
    for cohort in COHORTS:
        if cohort != "observed_tasks" and not cohort_available:
            continue
        event_low, event_high = start, event_end
        use_low, use_high = usage_start, usage_end
        if cohort != "observed_tasks":
            # Eligibility requires a matched completed attempt in both streams.
            event_low = use_low = max(start, usage_start)
            event_high = use_high = min(event_end, usage_end)
        for width in (60, 300, 900, 3600):
            origin = TRACE_FIRST // SECOND
            first = origin + (min(event_low, use_low) - origin + width - 1) // width * width
            last = origin + (max(event_high, use_high) - origin) // width * width
            for t in range(first, last, width):
                has_events = event_low <= t and t + width <= event_high
                has_usage = cohort != "submit_immediate_proxy" and width >= 300 and (
                    use_low <= t and t + width <= use_high
                )
                if not (has_events or has_usage):
                    continue
                row = [cohort, t, width]
                if has_events:
                    minute_bins = list(range(t, t + width, 60))
                    series = [minutes.get((cohort, minute), (0,) * 8) for minute in minute_bins]
                    run, peak, cpu, mem, no_cpu, no_mem, cpu_peak, mem_peak = zip(*series)
                    new = sum(arrivals.get((cohort, minute), 0) for minute in minute_bins)
                    row.extend((new, new / width, sum(run) / width, max(peak),
                                sum(cpu) / width, sum(mem) / width))
                else:
                    no_cpu, no_mem, cpu_peak, mem_peak = (), (), (), ()
                    row.extend([None] * 6)
                if has_usage:
                    series = []
                    for tick in range(t, t + width, 300):
                        active = any(minutes.get((cohort, minute), (0,))[0] > 0
                                     for minute in range(tick, tick + 300, 60))
                        idle = not active and cohort == "batch_proxy"
                        missing = 0 if idle else None
                        series.append(usage.get((cohort, tick), (0, missing, missing, 0, 0)))
                    records, cpu, mem, cpu_seconds, mem_seconds = zip(*series)
                    row.extend((sum(cpu) / width if None not in cpu else None,
                                sum(mem) / width if None not in mem else None, sum(records),
                                sum(x or 0 for x in cpu_seconds), sum(x or 0 for x in mem_seconds)))
                else:
                    row.extend([None] * 5)
                row.extend((sum(no_cpu), sum(no_mem), max(cpu_peak), max(mem_peak))
                           if has_events else (None, None, None, None))
                rows.append(tuple(row))
    columns = ", ".join(f"{name} {'VARCHAR' if index == 0 else 'DOUBLE'}"
                        for index, name in enumerate(SERIES_COLUMNS))
    db.execute(f"CREATE TEMP TABLE series ({columns})")
    if rows:
        # Bulk CSV import keeps the scripting API out of the raw-data hot path.
        write_csv(stats.series_path, SERIES_COLUMNS, rows)
        db.execute(f"""
            INSERT INTO series SELECT * FROM read_csv({sql_literal(stats.series_path.as_posix())},
                header = true, auto_detect = false,
                columns = {{{", ".join(f"'{name}': '{'VARCHAR' if index == 0 else 'DOUBLE'}'"
                                       for index, name in enumerate(SERIES_COLUMNS))}}})
        """)
    units = {
        "new_tasks": "tasks", "arrival_rate_per_s": "tasks_per_second",
        "running_tasks_avg": "tasks", "running_tasks_peak": "tasks", "cpu_requested_avg": "normalized_cpu",
        "mem_requested_avg": "normalized_memory", "cpu_actual_avg": "normalized_cpu",
        "mem_actual_avg": "normalized_memory", "cpu_requested_peak": "normalized_cpu",
        "mem_requested_peak": "normalized_memory",
    }
    for cohort in COHORTS:
        for width in (60, 300, 900, 3600):
            section = f"timeseries_{width}s"
            where = f"WHERE cohort = {sql_literal(cohort)} AND bin_seconds = {width}"
            for metric, unit in units.items():
                stats.distribution(section, cohort, metric, f"SELECT {metric} AS x FROM series {where}", unit, exact=True)
            zero, bins = db.execute(f"""
                SELECT count(*) FILTER (WHERE new_tasks = 0), count(new_tasks) FROM series {where}
            """).fetchone()
            stats.fraction(section, cohort, "zero_arrival_bins", zero, bins)

    for cohort in ("observed_tasks", "batch_proxy"):
        where = (f"WHERE cohort = {sql_literal(cohort)} AND bin_seconds = 300 "
                 "AND cpu_actual_avg IS NOT NULL AND cpu_requested_avg IS NOT NULL "
                 "AND mem_actual_avg IS NOT NULL AND mem_requested_avg IS NOT NULL")
        for metric, unit in units.items():
            stats.distribution("joint_timeseries_300s", cohort, metric,
                               f"SELECT {metric} AS x FROM series {where}", unit, exact=True)
        for resource in ("cpu", "mem"):
            stats.distribution("joint_timeseries_300s", cohort, f"{resource}_sample_to_running_task_s_ratio",
                               f"SELECT {resource}_sample_task_s / (running_tasks_avg * bin_seconds) AS x "
                               f"FROM series {where} AND running_tasks_avg > 0", "ratio",
                               "Ratio of covered duration sums to event running duration; "
                               "may exceed one due to record/attempt boundaries, distinct source scope or overlaps.",
                               exact=True)
    diurnal = db.execute("""
        WITH per_hour AS (
            SELECT cohort, ((CAST(bin_start_s AS BIGINT) - 600 + 19 * 3600) // 3600) % 24 AS local_hour,
                avg(arrival_rate_per_s) AS arrivals, avg(cpu_requested_avg) AS cpu_req,
                avg(mem_requested_avg) AS mem_req, avg(cpu_actual_avg) AS cpu,
                avg(mem_actual_avg) AS mem, avg(running_tasks_avg) AS concurrent,
                count(arrival_rate_per_s) AS event_bins, count(cpu_actual_avg) AS usage_bins
            FROM series WHERE bin_seconds = 300
            GROUP BY cohort, local_hour
        )
        SELECT * FROM per_hour ORDER BY cohort, local_hour
    """).fetchall()
    for cohort in COHORTS:
        for time_slice, predicate in (("daytime_07_19_EDT", "BETWEEN 7 AND 18"),
                                      ("nighttime_19_07_EDT", "NOT BETWEEN 7 AND 18")):
            for metric in ("arrival_rate_per_s", "cpu_requested_avg", "mem_requested_avg",
                           "cpu_actual_avg", "mem_actual_avg", "running_tasks_avg"):
                mean, count = db.execute(f"""
                    SELECT avg({metric}), count({metric}) FROM series WHERE bin_seconds = 300
                        AND cohort = {sql_literal(cohort)}
                        AND ((CAST(bin_start_s AS BIGINT) - 600 + 19 * 3600) // 3600) % 24 {predicate}
                """).fetchone()
                stats.add(time_slice, cohort, metric, "mean", mean, units[metric], count,
                          "EDT local time; repeated daily cycles require enough days of coverage.")
    return rows, diurnal


def machine_statistics(stats, tables):
    db = stats.db
    if tables["machine_events"][0]:
        stats.counts("machines", "machine_events", """
            SELECT count(*) AS machine_event_records, count(DISTINCT machine_id) AS machines,
                count(*) FILTER (WHERE event_type = 0) AS add_events,
                count(*) FILTER (WHERE event_type = 1) AS remove_events,
                count(*) FILTER (WHERE event_type = 2) AS update_events,
                count(DISTINCT platform) AS platforms,
                count(DISTINCT (cpu_capacity, mem_capacity)) FILTER (
                    WHERE cpu_capacity IS NOT NULL AND mem_capacity IS NOT NULL
                ) AS capacity_pairs,
                count(DISTINCT machine_id) FILTER (WHERE t = 0 AND event_type = 0) AS machines_at_trace_start
            FROM machine_events
        """)
        db.execute("""
            CREATE TEMP TABLE first_machine_capacity AS
            SELECT machine_id,
                arg_min(cpu_capacity, (t, ordinality)) AS cpu_capacity,
                arg_min(mem_capacity, (t, ordinality)) AS mem_capacity,
                arg_min(platform, (t, ordinality)) AS platform
            FROM machine_events GROUP BY machine_id
        """)
        for field, unit in (("cpu_capacity", "normalized_cpu"), ("mem_capacity", "normalized_memory")):
            stats.distribution("machines", "machine_events", field,
                               f"SELECT {field} AS x FROM first_machine_capacity", unit)
            total, count = db.execute(
                f"SELECT sum({field}), count({field}) FROM machine_events WHERE t = 0 AND event_type = 0"
            ).fetchone()
            stats.add("machines", "machine_events", f"{field}_at_trace_start", "sum", total, unit, count)
        for cpu, mem, count in db.execute("""
            SELECT cpu_capacity, mem_capacity, count(*) FROM first_machine_capacity
            GROUP BY cpu_capacity, mem_capacity ORDER BY count(*) DESC, cpu_capacity, mem_capacity
        """).fetchall():
            stats.add("machine_capacity_mix", "machine_events", f"cpu_{cpu}_mem_{mem}", "count", count, "machines")
        stats.categorical("machine_platform_mix", "machine_events", "platform",
                          "SELECT platform AS x FROM first_machine_capacity")
    if tables["machine_attributes"][0]:
        stats.counts("machines", "machine_attributes", """
            SELECT count(*) AS attribute_records, count(DISTINCT machine_id) AS machines_with_attributes,
                count(DISTINCT attribute_name) AS attribute_names,
                count(*) FILTER (WHERE deleted) AS attribute_deletions
            FROM machine_attributes
        """)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=BASE / "download_manifest.csv")
    parser.add_argument("--raw", type=Path, default=BASE / "data" / "raw")
    parser.add_argument("--output", type=Path, default=BASE)
    parser.add_argument("--work", type=Path, default=BASE / "data" / "work")
    parser.add_argument("--start", type=int, default=600, help="inclusive trace second (600 is the official start)")
    parser.add_argument("--end", type=int, help="exclusive trace second")
    parser.add_argument("--max-event-shards", type=int)
    parser.add_argument("--max-usage-shards", type=int)
    parser.add_argument("--max-job-shards", type=int)
    parser.add_argument("--max-constraint-shards", type=int)
    parser.add_argument("--partitions", type=int, default=0,
                        help="use memory-bounded lifecycle, timeline and usage partitions; 32 for the full trace")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--stage-workers", type=int, default=1,
                        help="independent gzip readers during caching; use modest values on shared hosts")
    parser.add_argument("--memory-limit", default="1.5GB")
    parser.add_argument("--stage-only", action="store_true",
                        help="cache projected Parquet for verified inputs without changing published statistics")
    args = parser.parse_args()
    if args.start < 600 or (args.end is not None and args.end <= args.start):
        parser.error("use trace seconds with 600 <= start < end")
    limits = (args.max_event_shards, args.max_usage_shards, args.max_job_shards, args.max_constraint_shards)
    if args.threads <= 0 or args.stage_workers <= 0 or args.partitions < 0 or any(x is not None and x <= 0 for x in limits):
        parser.error("threads, stage workers, and shard limits must be positive")
    snapshot, manifest_contents = verified_snapshot(args)
    args.work.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.work / "statistics.duckdb")) as db:
        db.execute("SET threads = ?", [args.threads])
        db.execute("SET memory_limit = ?", [args.memory_limit])
        db.execute("SET preserve_insertion_order = false")
        db.execute("SET temp_directory = ?", [(args.work / "spill").as_posix()])
        staged_tables(db, snapshot, args.work, args.stage_workers)
        if args.stage_only:
            print(f"Cached {sum(len(paths) for paths, _, _ in snapshot.values())} verified shards", flush=True)
            return
        windows = coverage(db, args, snapshot)
        print(f"Trace windows in seconds: event [{windows[0] / SECOND}, {windows[1] / SECOND}), "
              f"usage [{windows[2] / SECOND}, {windows[3] / SECOND})", flush=True)
        partition_options = None
        if args.partitions:
            checksums = [(table, [sha for _, sha in paths])
                         for table, (paths, _, _) in snapshot.items()]
            source_key = hashlib.sha256(repr((windows, checksums, MAXINT, UINT_MAX)).encode()).hexdigest()
            partition_options = (args.work, source_key, args.partitions)
        for script in ("lifecycle.sql", "usage.sql", "timeline.sql"):
            print(f"Running {script}", flush=True)
            sql = (BASE / script).read_text(encoding="utf-8")
            if partition_options and script == "lifecycle.sql":
                from lifecycle_partition import run_lifecycle_partitions
                run_lifecycle_partitions(db, args.work, sql, source_key, args.partitions)
                continue
            if partition_options and script == "timeline.sql":
                from timeline_partition import run_timeline_partitions
                run_timeline_partitions(db, args.work, sql, source_key, windows)
                continue
            for statement in db.extract_statements(sql):
                target = re.search(r"(?im)^CREATE TEMP TABLE (\w+)", statement.query)
                if target:
                    print(f"  Building {target[1]}", flush=True)
                db.execute(statement.query)
        stats = Statistics(db)
        stats.series_path = args.work / "timeline-staging.csv"
        print("Computing statistics and bounded plotting aggregates", flush=True)
        distributions, joint = task_statistics(stats, snapshot, windows, partition_options)
        series, diurnal = timeline_statistics(stats, windows)
        machine_statistics(stats, snapshot)
        plot_dir = args.output / "plot_data"
        write_csv(plot_dir / "timeseries.csv", SERIES_COLUMNS, series)
        write_csv(plot_dir / "distributions.csv", ("cohort", "metric", "bin_left", "bin_right", "count"),
                  distributions)
        write_csv(plot_dir / "joint_requests.csv", ("cohort", "cpu_left", "cpu_right", "mem_left", "mem_right", "count"),
                  joint)
        write_csv(plot_dir / "diurnal.csv", ("cohort", "local_hour_edt", "arrival_rate_per_s", "cpu_requested_avg",
                                           "mem_requested_avg", "cpu_actual_avg", "mem_actual_avg", "running_tasks_avg",
                                           "event_5min_bins", "usage_5min_bins"), diurnal)
        manifest_pending = args.work / "analysis_manifest.pending"
        manifest_pending.write_text(manifest_contents, encoding="utf-8", newline="")
        manifest_pending.replace(args.work / "analysis_manifest.csv")
        write_csv(args.output / "workload_statistics.csv",
                  ("section", "cohort", "metric", "statistic", "value", "unit", "n", "notes"), stats.rows)
    print(f"Exported {len(stats.rows)} statistics and {len(series)} timeline rows to {args.output}", flush=True)


if __name__ == "__main__":
    main()
