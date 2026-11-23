"""Bound the per-task resource aggregation with disjoint task-key partitions."""
import hashlib
from pathlib import Path
from tempfile import mkdtemp


CLIPPED = """
    SELECT job_id, task_index, cpu_mean, mem_mean, cpu_max, mem_max,
        least(end_t, usage_end()) - greatest(start_t, usage_start()) AS span
    FROM task_usage_measures
"""
AGGREGATE = """
    SELECT job_id, task_index, count(*) AS usage_samples,
        sum(cpu_mean * span) / nullif(sum(span) FILTER (WHERE cpu_mean IS NOT NULL), 0) AS cpu_usage_mean,
        sum(mem_mean * span) / nullif(sum(span) FILTER (WHERE mem_mean IS NOT NULL), 0) AS mem_usage_mean,
        max(cpu_max) AS cpu_usage_max, max(mem_max) AS mem_usage_max
    FROM {relation}
    GROUP BY job_id, task_index
"""


def quote_path(path):
    return "'" + path.as_posix().replace("'", "''") + "'"


def build_task_use(db, work: Path, cache_key: str, partitions: int):
    fingerprint = hashlib.sha256(
        f"1|{cache_key}|{partitions}|{CLIPPED}|{AGGREGATE}".encode()
    ).hexdigest()[:24]
    root = work / f"task_use_buckets_{fingerprint}"
    inputs = root / "input"
    outputs = root / "output"
    outputs.mkdir(parents=True, exist_ok=True)
    if not inputs.exists():
        pending = Path(mkdtemp(prefix="pending-", dir=root))
        print(f"Partitioning usage intervals into {partitions} task-key buckets", flush=True)
        db.execute(f"""
            COPY (
                SELECT *, hash(job_id, task_index) % {partitions} AS bucket
                FROM ({CLIPPED})
            ) TO {quote_path(pending)}
            (FORMAT PARQUET, PARTITION_BY (bucket), COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """)
        pending.replace(inputs)

    aggregates = []
    for bucket in sorted(inputs.glob("bucket=*")):
        target = outputs / f"{bucket.name}.parquet"
        if not target.exists():
            print(f"Aggregating usage {bucket.name}", flush=True)
            pending = target.with_suffix(".pending")
            relation = f"read_parquet({quote_path(bucket / '*.parquet')})"
            query = AGGREGATE.format(relation=relation)
            db.execute(f"""
                COPY ({query}) TO {quote_path(pending)}
                (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
            pending.replace(target)
        aggregates.append(quote_path(target))
    if aggregates:
        query = "SELECT * FROM read_parquet([" + ", ".join(aggregates) + "])"
    else:
        query = AGGREGATE.format(relation=f"({CLIPPED})")
    db.execute(f"CREATE TEMP VIEW task_use AS {query}")
