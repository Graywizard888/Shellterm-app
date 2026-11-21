#!/usr/bin/env python3
"""Publishes a folder as the assets of a release, sending only what changed, and waits out API rate limits.

Why: the workflow's own token (GITHUB_TOKEN) may make about 1,000 API requests an hour for the whole repository, and
every uploaded file is a request. Seven lots of 700 to 1,000 files at once cannot fit, so a run died with "API rate
limit exceeded for installation". Three things make a run fit:
  * a personal access token (secret PACKAGES_TOKEN) has its own 5,000 requests an hour,
  * files already on the release with the same SHA-256 are not sent again (a weekly run changes few packages, and a
    run that stopped half way continues instead of starting over),
  * when the limit is reached anyway, it keeps waiting (up to MAX_WAIT, longer than one hourly window) and goes on.

    publish_release.py TAG FOLDER [TITLE NOTES]   (needs the gh CLI, GH_TOKEN and GITHUB_REPOSITORY in the environment)

With TITLE and NOTES a missing release is created first. Only a real "not found" creates it: a rate-limit error is
waited out like every other call, so a limited token never tries to create a release that already exists.

Packages.xz and Release go last: apt must never see an index that names files not yet uploaded.
"""
import hashlib
import os
import subprocess
import sys
import time

# Sent last, in this order.
INDEX_FILES = ["Packages", "Packages.gz", "Packages.xz", "Release"]
BATCH = 40
TRIES = 10  # for errors that are not a rate limit (a dropped connection, a 502)
MAX_WAIT = 95 * 60  # seconds a rate limit may be waited out in all: more than one hourly window, so a reset is always reached
LOW_QUOTA = 100
PAUSE = 60
LONG_PAUSE = 300


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def local_files(folder):
    """name -> sha256 of every file in the folder."""
    return {
        name: sha256_file(os.path.join(folder, name))
        for name in sorted(os.listdir(folder))
        if os.path.isfile(os.path.join(folder, name))
    }


def plan(existing, local):
    """What to do to make the release hold exactly [local]: (names to delete, names to upload in order).

    [existing] is name -> sha256 as the release lists it ("" when GitHub gave none, which counts as different)."""
    delete = sorted(name for name in existing if name not in local)
    changed = [name for name in sorted(local) if existing.get(name) != local[name]]
    files = [name for name in changed if name not in INDEX_FILES]
    index = [name for name in INDEX_FILES if name in changed]
    return delete, files + index


def batches(names, size=BATCH):
    """Chunks of at most [size] names; the index files are never in a chunk with other files, so they stay last."""
    chunk = []
    for name in names:
        if name in INDEX_FILES:
            continue
        chunk.append(name)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
    index = [name for name in names if name in INDEX_FILES]
    if index:
        yield index


def seconds_to_wait(remaining, reset, now):
    """How long to sleep after a failed request: until the quota resets when it is used up, else a short pause."""
    if remaining < LOW_QUOTA:
        return max(reset - now, 0) + 15
    return PAUSE


def is_rate_limited(error):
    """True when GitHub refused the request because of a rate limit (primary or secondary)."""
    text = f"{error.stderr or ''} {error.output or ''}".lower()
    return "rate limit" in text


def retrying(action, quota, sleep=time.sleep, now=time.time, tries=TRIES, max_wait=MAX_WAIT):
    """Runs [action] until it works, sleeping as [quota] (-> remaining, reset) advises.

    A rate-limit error is waited out for up to [max_wait] seconds in all (a quota the API still shows as plentiful
    is no reason to hurry: the wait grows to LONG_PAUSE instead of staying at PAUSE). Any other error is tried
    [tries] times."""
    attempt, waited = 0, 0
    while True:
        attempt += 1
        try:
            return action()
        except subprocess.CalledProcessError as error:
            limited = is_rate_limited(error)
            if (waited >= max_wait) if limited else (attempt >= tries):
                raise
            try:
                remaining, reset = quota()
            except Exception:  # the quota call itself may be what is limited
                remaining, reset = 0, now() + 60
            wait = seconds_to_wait(remaining, reset, now())
            if limited and wait <= PAUSE:
                wait = min(PAUSE * 2 ** (attempt - 1), LONG_PAUSE)
            wait = min(wait, max(max_wait - waited, PAUSE)) if limited else wait
            print(f"Attempt {attempt} failed ({error}); waiting {int(wait)} s", flush=True)
            sleep(wait)
            waited += wait


def gh(*args, capture=False):
    # stderr is captured so a failure carries GitHub's message (is_rate_limited reads it) and is still shown in the log.
    try:
        result = subprocess.run(
            ["gh", *args], check=True, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as error:
        print((error.stderr or "").strip(), file=sys.stderr, flush=True)
        raise
    return result.stdout if capture else None


def quota():
    out = gh("api", "rate_limit", "--jq", ".resources.core | [.remaining, .reset] | @tsv", capture=True)
    remaining, reset = out.split()
    return int(remaining), int(reset)


def ensure_release(repo, tag, title, notes, run=subprocess.run):
    """Creates the release as a pre-release unless it exists. Raises on any error other than "not found"."""
    found = run(["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--silent"], capture_output=True, text=True)
    if found.returncode == 0:
        return
    if "HTTP 404" not in (found.stderr or ""):
        raise subprocess.CalledProcessError(found.returncode, found.args, stderr=found.stderr)
    gh("release", "create", tag, "--prerelease", "--latest=false", "--title", title, "--notes", notes)


def release_assets(repo, tag):
    """name -> sha256 ("" when unknown) of what the release holds now."""
    release_id = gh("api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".id", capture=True).strip()
    out = gh(
        "api", "--paginate", f"repos/{repo}/releases/{release_id}/assets?per_page=100",
        "--jq", r'.[] | [.name, ((.digest // "") | sub("^sha256:"; ""))] | @tsv', capture=True,
    )
    existing = {}
    for line in out.splitlines():
        name, _, digest = line.partition("\t")
        existing[name] = digest
    return existing


def publish(tag, folder, repo, title=None, notes=None):
    if title and notes:
        retrying(lambda: ensure_release(repo, tag, title, notes), quota)
    local = local_files(folder)
    existing = retrying(lambda: release_assets(repo, tag), quota)
    delete, upload = plan(existing, local)
    print(f"{tag}: {len(local)} files, {len(upload)} to upload, {len(delete)} to remove", flush=True)
    for name in delete:
        retrying(lambda name=name: gh("release", "delete-asset", tag, name, "--yes"), quota)
    sent = 0
    for chunk in batches(upload):
        paths = [os.path.join(folder, name) for name in chunk]
        retrying(lambda paths=paths: gh("release", "upload", tag, "--clobber", *paths), quota)
        sent += len(chunk)
        print(f"{tag}: {sent} of {len(upload)} uploaded", flush=True)


def main(argv):
    if len(argv) not in (3, 5):
        print(__doc__)
        return 2
    publish(argv[1], argv[2], os.environ["GITHUB_REPOSITORY"], *argv[3:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
