"""Download and verify the public Google Cluster Trace 2011 v2.1 objects."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from contextlib import contextmanager
import hashlib
import os
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
from urllib.parse import quote

import requests
from urllib3.exceptions import HTTPError

BASE = Path(__file__).resolve().parent
BUCKET = "clusterdata-2011-2"
API = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o"
METADATA = {"MD5SUM", "SHA1SUM", "SHA256SUM", "README", "schema.csv"}
COLUMNS = (
    "object", "table", "generation", "expected_bytes", "local_bytes",
    "sha256_expected", "sha256_actual", "md5_base64", "crc32c_base64",
    "status", "source", "local_path", "error",
)
BLOCK = 1024 * 1024
TIMEOUT = (12, 90)


def write_manifest(path, rows):
    staged = path.with_suffix(".csv.tmp")
    with staged.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    for attempt in range(5):
        try:
            staged.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.15 * (attempt + 1))


def read_catalog(data):
    output = data / "bucket_inventory.json"
    if output.exists():
        return json.loads(output.read_text(encoding="utf-8"))
    token = None
    items = []
    while True:
        params = {
            "fields": "nextPageToken,items(name,size,md5Hash,crc32c,generation,contentEncoding)",
            "maxResults": 1000,
        }
        if token:
            params["pageToken"] = token
        for attempt in range(4):
            try:
                with requests.get(API, params=params, timeout=TIMEOUT) as response:
                    response.raise_for_status()
                    page = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)
        items.extend(page.get("items", ()))
        token = page.get("nextPageToken")
        if not token:
            break
    if not items or len({item["name"] for item in items}) != len(items):
        raise ValueError("Empty or duplicate object listing")
    output.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return items


def object_row(item, raw):
    name = item["name"]
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or chr(92) in name:
        raise ValueError(f"Unexpected object path {name!r}")
    path = raw.joinpath(*relative.parts)
    return {
        "object": name,
        "table": relative.parts[0] if len(relative.parts) > 1 else "metadata",
        "generation": item["generation"],
        "expected_bytes": int(item["size"]),
        "local_bytes": path.stat().st_size if path.exists() else 0,
        "sha256_expected": "",
        "sha256_actual": "",
        "md5_base64": item.get("md5Hash", ""),
        "crc32c_base64": item.get("crc32c", ""),
        "status": "pending_verify" if path.exists() else "not_downloaded",
        "source": f"gs://{BUCKET}/{name}",
        "local_path": path.relative_to(BASE).as_posix() if path.is_relative_to(BASE) else path.as_posix(),
        "error": "",
    }


def checksums(path):
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode("ascii")


def verify(path, row):
    if path.stat().st_size != row["expected_bytes"]:
        raise ValueError(f"Size mismatch ({path.stat().st_size})")
    sha, md5 = checksums(path)
    if row["md5_base64"] and md5 != row["md5_base64"]:
        raise ValueError("GCS MD5 mismatch")
    if row["sha256_expected"] and sha != row["sha256_expected"]:
        raise ValueError("Official SHA256 mismatch")
    if not row["sha256_expected"] and not row["md5_base64"]:
        raise ValueError("No authoritative checksum for this object")
    return sha


def fetch_http(session, row, partial):
    expected = row["expected_bytes"]
    offset = partial.stat().st_size if partial.exists() else 0
    if offset == expected and partial.exists():
        return
    if offset > expected:
        raise ValueError("Partial file exceeds object size")
    url = f"{API}/{quote(row['object'], safe='')}"
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    params = {"alt": "media", "generation": row["generation"]}
    with session.get(url, headers=headers, params=params, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        if offset and response.status_code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match or int(match[1]) != offset or int(match[3]) != expected:
                raise ValueError("Unexpected Content-Range; partial was preserved")
            mode = "ab"
        elif response.status_code == 200:
            mode = "wb"
        else:
            raise ValueError(f"Unexpected response {response.status_code}")
        with partial.open(mode) as stream:
            for chunk in response.raw.stream(BLOCK, decode_content=False):
                stream.write(chunk)


def download(row, raw, gcloud, attempts):
    path = raw.joinpath(*PurePosixPath(row["object"]).parts)
    partial = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            row["sha256_actual"] = verify(path, row)
            row.update(status="verified_sha256" if row["sha256_expected"] else "verified_gcs_md5",
                       local_bytes=path.stat().st_size, error="")
        except (OSError, ValueError) as error:
            row.update(status="failed", error=f"Existing file preserved: {error}")
        return row
    with requests.Session() as session:
        for attempt in range(attempts):
            try:
                if gcloud and not partial.exists():
                    result = subprocess.run(
                        [gcloud, "storage", "cp", f"{row['source']}#{row['generation']}", str(partial)],
                        capture_output=True, text=True, check=False,
                    )
                    if result.returncode:
                        partial_error = result.stderr.strip().splitlines()[-1:] or ["gcloud failed"]
                        raise OSError(partial_error[0])
                else:
                    fetch_http(session, row, partial)
                sha = verify(partial, row)
                partial.replace(path)
                row.update(status="verified_sha256" if row["sha256_expected"] else "verified_gcs_md5",
                           sha256_actual=sha, local_bytes=path.stat().st_size, error="")
                return row
            except (requests.RequestException, HTTPError, OSError, ValueError) as error:
                size = partial.stat().st_size if partial.exists() else 0
                row.update(status="failed", local_bytes=size, error=str(error)[:350])
                if partial.exists() and size >= row["expected_bytes"]:
                    partial.replace(partial.with_name(f"{partial.name}.invalid-{time.time_ns()}"))
                if attempt + 1 < attempts:
                    time.sleep(min(8, 1 + 2 * attempt))
    return row


def read_official_sha(raw):
    hashes = {}
    with (raw / "SHA256SUM").open(encoding="utf-8") as stream:
        for line in stream:
            match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip())
            if match:
                name = match[2].removeprefix("./").removeprefix(f"gs://{BUCKET}/")
                hashes[name] = match[1].lower()
    if not hashes:
        raise ValueError("Official SHA256SUM does not contain parseable hashes")
    return hashes


def queue_order(row):
    match = re.search(r"/part-(\d+)-of-", row["object"])
    table_priority = {
        "machine_events": 0, "machine_attributes": 1, "task_events": 2,
        "task_usage": 3, "job_events": 4, "task_constraints": 5,
    }
    return (int(match[1]) if match else -1, table_priority.get(row["table"], 9), row["object"])


@contextmanager
def download_lock(data):
    with (data / "download.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise SystemExit(f"A download is already running for {data}: {error}") from error
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=BASE / "data")
    parser.add_argument("--manifest", type=Path, default=BASE / "download_manifest.csv")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--sample-shards", type=int, help="Download only this many earliest official shards per table")
    parser.add_argument("--tables", nargs="+", help="Limit downloading to selected table prefixes")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--transport", choices=("auto", "https", "gcloud"), default="auto")
    args = parser.parse_args()
    data = args.data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    with download_lock(data):
        return run(args, data, parser)


def run(args, data, parser):
    raw = data / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    items = read_catalog(data)
    rows = [object_row(item, raw) for item in items]
    by_name = {row["object"]: row for row in rows}
    previous = {}
    if args.manifest.exists():
        with args.manifest.open(newline="", encoding="utf-8") as stream:
            previous = {row["object"]: row for row in csv.DictReader(stream)}
    gcloud = shutil.which("gcloud") if args.transport != "https" else None
    if args.transport == "gcloud" and not gcloud:
        parser.error("gcloud not found")
    head = by_name["SHA256SUM"]
    download(head, raw, gcloud, args.retries)
    if not head["status"].startswith("verified_"):
        raise RuntimeError(f"Unable to verify SHA256SUM: {head['error']}")
    sums = read_official_sha(raw)
    for row in rows:
        name = row["object"]
        row["sha256_expected"] = sums.get(name, "")
        old = previous.get(name, {})
        if (row["status"] == "pending_verify" and old.get("status", "").startswith("verified_")
                and old.get("generation") == row["generation"]
                and old.get("sha256_expected") == row["sha256_expected"]
                and int(old.get("local_bytes") or -1) == row["expected_bytes"] == row["local_bytes"]):
            row.update(status=old["status"], sha256_actual=old["sha256_actual"])
    write_manifest(args.manifest, rows)
    print(f"Official inventory: {len(rows)} objects, {sum(row['expected_bytes'] for row in rows):,} bytes", flush=True)
    if args.inventory_only:
        return 0

    selected = []
    counts = {}
    for row in sorted(rows, key=queue_order):
        table = row["table"]
        if table != "metadata":
            if args.tables and table not in args.tables:
                continue
            counts[table] = counts.get(table, 0) + 1
            if args.sample_shards is not None and counts[table] > args.sample_shards:
                continue
        if not row["status"].startswith("verified_"):
            selected.append(row)
    remaining = sum(row["expected_bytes"] for row in selected)
    free = shutil.disk_usage(data).free
    if remaining > free:
        raise SystemExit(f"Insufficient disk: selected {remaining:,} bytes, free {free:,}")
    print(f"Queue: {len(selected)} objects, {remaining:,} bytes, transport={'gcloud' if gcloud else 'HTTPS'}", flush=True)
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(download, row.copy(), raw, gcloud, args.retries) for row in selected]
        for i, future in enumerate(as_completed(futures), 1):
            row = future.result()
            by_name[row["object"]].update(row)
            if row["status"] == "failed":
                failed += 1
                print(f"FAILED {row['object']}: {row['error']}", flush=True)
            if i == 1 or i % 12 == 0 or i == len(futures):
                write_manifest(args.manifest, rows)
                verified = [candidate for candidate in rows if candidate["status"].startswith("verified_")]
                print(f"Finished {i}/{len(futures)} in this run; {len(verified)} verified, "
                      f"{sum(candidate['local_bytes'] for candidate in verified):,} bytes", flush=True)
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
