#!/usr/bin/env python3
"""Copies a folder to the Cloudflare R2 bucket behind the edge mirror (edge/), with the S3 tools the runner has.

    sync_r2.py PREFIX FOLDER      e.g.  sync_r2.py apt-1 work/debs      (needs the aws CLI)

Environment: R2_ACCOUNT_ID, R2_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (the R2 API token's keys). When R2_BUCKET
is empty nothing is done and the run goes on: the mirror is optional and the Worker falls back to GitHub.

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


def sync(prefix, folder, bucket, endpoint):
    env = {"endpoint": endpoint}
    local = sorted(name for name in os.listdir(folder) if os.path.isfile(os.path.join(folder, name)))
    target = f"s3://{bucket}/{prefix}/"
    excludes = [arg for name in INDEX_FILES for arg in ("--exclude", name)]
    aws(env, "s3", "sync", folder, target, *excludes, "--only-show-errors", "--no-progress")
    for name in INDEX_FILES:
        if name in local:
            aws(env, "s3", "cp", os.path.join(folder, name), target + name, "--only-show-errors")
    gone = stale(remote_names(env, bucket, prefix), local)
    for name in gone:
        aws(env, "s3", "rm", target + name, "--only-show-errors")
    print(f"R2 {prefix}: {len(local)} files copied, {len(gone)} removed", flush=True)


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
    sync(argv[1], argv[2], bucket, f"https://{account}.r2.cloudflarestorage.com")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
