"""Sweep the request timeline one trace day at a time."""

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory


MINUTE_US = 60_000_000
DAY_US = 24 * 60 * MINUTE_US
DELTAS = "run_delta, cpu_delta, mem_delta, no_cpu_delta, no_mem_delta"
SUMS = ", ".join(f"sum({name}) AS {name}" for name in DELTAS.split(", "))

TRACE_EDGES = f"""
    SELECT cohort, t, {DELTAS}, t // {DAY_US} AS trace_day
    FROM (
        SELECT cohort, CASE WHEN sign = 1 THEN started ELSE stopped END AS t,
            sign AS run_delta, sign * coalesce(cpu_request, 0) AS cpu_delta,
            sign * coalesce(mem_request, 0) AS mem_delta,
            sign * (cpu_request IS NULL)::INTEGER AS no_cpu_delta,
            sign * (mem_request IS NULL)::INTEGER AS no_mem_delta
        FROM timed_inputs CROSS JOIN (VALUES (1), (-1)) direction(sign)
    )
"""

NAMES = "(VALUES ('observed_tasks'), ('batch_proxy'), ('submit_immediate_proxy')) names(cohort)"


def run_timeline_partitions(
    db, work: Path, sql: str, cache_key: str, windows: tuple[int, int, int, int]
) -> None:
    """Populate the same minute tables as timeline.sql using a disk-backed edge partition."""
    inputs, minute_events, minute_arrivals = (item.query for item in db.extract_statements(sql))
    _, sweep = minute_events.split("), summed AS (", 1)
    # The partitioned COPY is the only consumer so avoid materializing every interval first.
    db.execute(inputs.replace("CREATE TEMP TABLE timed_inputs", "CREATE TEMP VIEW timed_inputs", 1))
    start, end, _, _ = windows
    first_tick = start // MINUTE_US * MINUTE_US
    last_tick = (end + MINUTE_US - 1) // MINUTE_US * MINUTE_US
    first_day = first_tick // DAY_US
    last_day = (max(first_tick + 1, last_tick) - 1) // DAY_US

    db.execute("""
        CREATE TEMP TABLE timeline_seed (
            cohort VARCHAR, run_delta HUGEINT, cpu_delta DOUBLE, mem_delta DOUBLE,
            no_cpu_delta HUGEINT, no_mem_delta HUGEINT
        )
    """)
    prefix = sha256(cache_key.encode()).hexdigest()[:12]
    with TemporaryDirectory(dir=work, prefix=f"timeline-{prefix}-") as temp:
        spool = Path(temp) / "edges"
        spool_path = spool.as_posix().replace("'", "''")
        print("Partitioning request timeline edges by trace day", flush=True)
        db.execute(f"""
            COPY ({TRACE_EDGES}) TO '{spool_path}'
            (FORMAT PARQUET, PARTITION_BY (trace_day), COMPRESSION ZSTD)
        """)

        for day in range(first_day, last_day + 1):
            day_start = max(first_tick, day * DAY_US)
            day_end = min(last_tick, (day + 1) * DAY_US)
            partition = spool / f"trace_day={day}"
            if partition.is_dir():
                pattern = (partition / "*.parquet").as_posix().replace("'", "''")
                edge_source = f"SELECT * FROM read_parquet('{pattern}')"
            else:
                edge_source = f"SELECT * FROM ({TRACE_EDGES}) LIMIT 0"
            db.execute(f"""
                CREATE OR REPLACE TEMP TABLE timeline_day_edges AS
                SELECT cohort, t, {SUMS} FROM ({edge_source}) GROUP BY cohort, t
            """)
            destination = (
                "CREATE TEMP TABLE minute_events AS" if day == first_day
                else "INSERT INTO minute_events"
            )
            # Carries summarize edges strictly before day_start. Edges at midnight
            # are grouped with that carry before the first positive-length segment.
            # day_end is a guard tick so no integration can cross the partition.
            db.execute(f"""
                {destination}
                WITH edges AS (
                    SELECT cohort, t, {DELTAS} FROM timeline_day_edges
                    UNION ALL
                    SELECT cohort, {day_start}, {DELTAS} FROM timeline_seed
                    UNION ALL
                    SELECT cohort, tick, 0, 0, 0, 0, 0
                    FROM range({day_start}, {day_end + MINUTE_US}, {MINUTE_US}) r(tick)
                    CROSS JOIN {NAMES}
                ), summed AS (
                {sweep}
            """)
            db.execute(f"""
                INSERT INTO timeline_seed
                SELECT cohort, {SUMS} FROM timeline_day_edges GROUP BY cohort
            """)

    db.execute("DROP TABLE timeline_day_edges")
    db.execute("DROP TABLE timeline_seed")
    db.execute("DROP VIEW timed_inputs")
    db.execute(minute_arrivals)
