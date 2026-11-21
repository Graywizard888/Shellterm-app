#!/usr/bin/env python3
"""Copies a folder to the Cloudflare R2 bucket behind the edge mirror (edge/), with the S3 tools the runner has.

    sync_r2.py PREFIX FOLDER      e.g.  sync_r2.py apt-1 work/debs      (needs the aws CLI)

Environment: R2_ACCOUNT_ID, R2_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (the R2 API token's keys). When R2_BUCKET
is empty nothing is done and the run goes on: the mirror is optional and the Worker falls back to GitHub.

Size cap: the bucket never holds more than R2_MAX_BYTES (default 9,500,000,000, a bit under R2's free 10 GB). Before a
folder is copied, what the rest of the bucket holds plus this folder's size must fit; if it does not, nothing of this
folder is copied (R2 keeps its older files, still a consistent set, and the Worker serves GitHub's for the rest), a
warning is shown on the run and the workflow carries on. The packages grow over time; raise the limit with the
R2_MAX_BYTES variable only if the bucket plan allows it.

Order, so apt never sees an index that names files not yet there:
  1. every file except the index files,
  2. the index files (Packages, Packages.gz, Packages.xz, Release),
  3. only then the files that are no longer in the folder are removed.
"""
import json
import os
import subprocess
import sys
import time

from publish_release import INDEX_FILES

TRIES = 4
PAUSE = 20
DEFAULT_MAX_BYTES = 9_500_000_000


def stale(remote, local):
    """Names in the bucket that the folder no longer has."""
    return sorted(set(remote) - set(local))


def aws(env, *args, capture=False):
    """Runs the aws CLI against R2, trying again after a failure (a dropped connection, a 5xx)."""
    command = ["aws", "--endpoint-url", env["endpoint"], *args]
    for attempt in range(1, TRIES + 1):
        try:
            result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE if capture else None)
            return result.stdout if capture else None
        except subprocess.CalledProcessError as error:
            if attempt == TRIES:
                raise
            print(f"aws {args[0]} {args[1]} failed ({error}); trying again in {PAUSE} s", flush=True)
            time.sleep(PAUSE)


def remote_names(env, bucket, prefix):
    out = aws(
        env, "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", f"{prefix}/",
        "--query", "Contents[].Key", "--output", "json", capture=True,
    )
    keys = json.loads(out) or []  # a prefix with nothing in it answers null
    return [key[len(prefix) + 1:] for key in keys]


def remote_sizes(env, bucket):
    """key -> size in bytes of everything in the bucket (the aws CLI pages through the listing by itself)."""
    out = aws(
        env, "s3api", "list-objects-v2", "--bucket", bucket,
        "--query", "Contents[].[Key,Size]", "--output", "json", capture=True,
    )
    return parse_sizes(json.loads(out))


def parse_sizes(listing):
    """[[key, size], ...] -> {key: size}. Pages with nothing in them come back as null, and a listing joined from
    several pages may be nested one level deeper; both are accepted."""
    sizes = {}
    for item in listing or []:
        if item is None:
            continue
        if isinstance(item, list) and item and isinstance(item[0], list):
            sizes.update(parse_sizes(item))
        elif isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            sizes[item[0]] = int(item[1])
    return sizes


def fits(sizes, prefix, new_bytes, max_bytes):
    """Whether the bucket stays within [max_bytes] once [prefix] holds [new_bytes] (its old files are replaced).

    Returns (fits, the bucket's size after the copy)."""
    other = sum(size for key, size in sizes.items() if not key.startswith(f"{prefix}/"))
    return other + new_bytes <= max_bytes, other + new_bytes


def gigabytes(n):
    return f"{n / 1e9:.2f} GB"


def note(text):
    """A warning on the run's page and in its summary; the run itself is not failed (GitHub is already published)."""
    print(f"::warning title=R2 copy skipped::{text}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### R2 copy skipped\n{text}\n")


def sync(prefix, folder, bucket, endpoint, max_bytes=DEFAULT_MAX_BYTES):
    env = {"endpoint": endpoint}
    local = sorted(name for name in os.listdir(folder) if os.path.isfile(os.path.join(folder, name)))
    new_bytes = sum(os.path.getsize(os.path.join(folder, name)) for name in local)
    ok, after = fits(remote_sizes(env, bucket), prefix, new_bytes, max_bytes)
    if not ok:
        note(
            f"{prefix} ({gigabytes(new_bytes)}) would take the bucket to {gigabytes(after)}, over the limit of "
            f"{gigabytes(max_bytes)}. Nothing of {prefix} was copied; R2 keeps its older files and GitHub serves the rest."
        )
        return False
    target = f"s3://{bucket}/{prefix}/"
    excludes = [arg for name in INDEX_FILES for arg in ("--exclude", name)]
    aws(env, "s3", "sync", folder, target, *excludes, "--only-show-errors", "--no-progress")
    for name in INDEX_FILES:
        if name in local:
            aws(env, "s3", "cp", os.path.join(folder, name), target + name, "--only-show-errors")
    gone = stale(remote_names(env, bucket, prefix), local)
    for name in gone:
        aws(env, "s3", "rm", target + name, "--only-show-errors")
    print(f"R2 {prefix}: {len(local)} files copied, {len(gone)} removed; bucket about {gigabytes(after)} of {gigabytes(max_bytes)}", flush=True)
    return True


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    bucket = os.environ.get("R2_BUCKET", "")
    account = os.environ.get("R2_ACCOUNT_ID", "")
    if not bucket or not account:
        print("R2 is not set up (R2_BUCKET / R2_ACCOUNT_ID empty): skipping the copy")
        return 0
    # R2 does not take the checksum headers newer aws CLIs add by default, and its region is "auto".
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    os.environ.setdefault("AWS_DEFAULT_REGION", "auto")
    sync(argv[1], argv[2], bucket, f"https://{account}.r2.cloudflarestorage.com", int(os.environ.get("R2_MAX_BYTES") or DEFAULT_MAX_BYTES))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
