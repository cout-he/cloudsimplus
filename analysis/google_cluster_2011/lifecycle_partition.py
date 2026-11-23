"""Build cross-shard lifecycle data in bounded groups while preserving job history."""

from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory


OUTPUTS = ("tasks", "batch_candidates", "running_segments")
INTERMEDIATES = ("event_history", "task_counts", "attempts")
INPUTS = ("task_events", "task_constraints")
PARTITION_COLUMN = "_lifecycle_bucket"


def literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def drop_temporary_tables(db, names):
    existing = {
        row[0] for row in db.execute("""
            SELECT table_name FROM duckdb_tables()
            WHERE temporary AND schema_name = 'main'
        """).fetchall()
    }
    for name in names:
        if name in existing:
            db.execute(f"DROP TABLE temp.main.{name}")


def build_checkpoint(target, build):
    if target.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # The pending directory is created under the checkpoint parent. Its contents
    # become visible as a completed checkpoint only when the directory is renamed.
    with TemporaryDirectory(prefix=".pending-", dir=target.parent) as pending:
        pending_path = Path(pending).resolve()
        if pending_path.parent != target.parent.resolve():
            raise RuntimeError(f"Unexpected checkpoint directory: {pending_path}")
        payload = pending_path / "data"
        build(payload)
        payload.rename(target)


def stage_input(db, source, target, partitions):
    def write(payload):
        print(f"Partitioning {source} by job into {partitions} buckets", flush=True)
        db.execute(f"""
            COPY (
                SELECT *, (hash(job_id) % {partitions})::INTEGER AS {PARTITION_COLUMN}
                FROM {source} WHERE job_id IS NOT NULL
            ) TO {literal(payload)}
            (FORMAT PARQUET, PARTITION_BY ({PARTITION_COLUMN}), COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """)
        # COPY may emit no files for an empty relation.
        payload.mkdir(exist_ok=True)

    build_checkpoint(target, write)


def bind_bucket(db, source, input_path, bucket, shadow):
    folder = input_path / f"{PARTITION_COLUMN}={bucket}"
    if folder.is_dir() and next(folder.glob("*.parquet"), None) is not None:
        query = f"SELECT * FROM read_parquet({literal(folder / '*.parquet')}, hive_partitioning = false)"
    else:
        query = f"SELECT * FROM {source} WHERE false"
    db.execute(f"CREATE OR REPLACE TEMP VIEW {shadow} AS {query}")


def run_lifecycle_partitions(db, work: Path, sql: str, cache_key: str, partitions: int = 32):
    """Run the same lifecycle SQL per job bucket, publishing three temporary views."""
    if partitions < 1:
        raise ValueError("partitions must be positive")
    start, end = db.execute("SELECT analysis_start(), event_end()").fetchone()
    root = work.resolve() / "lifecycle_partitions"
    base_key = sha256(f"v1|{partitions}|{cache_key}".encode()).hexdigest()[:24]
    result_key = sha256(f"{base_key}|{start}|{end}|{sql}".encode()).hexdigest()[:24]
    inputs = root / "inputs" / base_key
    results = root / "results" / result_key
    names = INPUTS + INTERMEDIATES + OUTPUTS
    shadows = {name: f"_lifecycle_{name}" for name in names}
    shadow_tables = [shadows[name] for name in INTERMEDIATES + OUTPUTS]
    pattern = r"\b(" + "|".join(names) + r")\b"
    bucket_sql = re.sub(pattern, lambda match: shadows[match.group()], sql)

    # A prior unpartitioned attempt may have left large temporary materializations.
    drop_temporary_tables(db, INTERMEDIATES + OUTPUTS)

    open_files, flush_threshold = db.execute("""
        SELECT current_setting('partitioned_write_max_open_files'),
            current_setting('partitioned_write_flush_threshold')
    """).fetchone()
    try:
        db.execute("SET partitioned_write_max_open_files = ?", [max(open_files, partitions * 2)])
        db.execute("SET partitioned_write_flush_threshold = ?", [max(flush_threshold, 524288)])
        for source in INPUTS:
            stage_input(db, source, inputs / source, partitions)
    finally:
        db.execute("SET partitioned_write_max_open_files = ?", [open_files])
        db.execute("SET partitioned_write_flush_threshold = ?", [flush_threshold])

    for bucket in range(partitions):
        def write(payload):
            print(f"Reconstructing lifecycle bucket {bucket + 1}/{partitions}", flush=True)
            for source in INPUTS:
                bind_bucket(db, source, inputs / source, bucket, shadows[source])
            payload.mkdir()
            try:
                db.execute(bucket_sql)
                for name in OUTPUTS:
                    target = literal(payload / f"{name}.parquet")
                    db.execute(f"""
                        COPY {shadows[name]} TO {target}
                        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
                    """)
            finally:
                drop_temporary_tables(db, shadow_tables)
                for source in INPUTS:
                    db.execute(f"DROP VIEW IF EXISTS temp.main.{shadows[source]}")

        build_checkpoint(results / f"{bucket:04d}", write)

    for name in OUTPUTS:
        paths = ", ".join(literal(results / f"{bucket:04d}" / f"{name}.parquet") for bucket in range(partitions))
        db.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * FROM read_parquet([{paths}])")
