#!/usr/bin/env python3
"""Publishes a folder as the assets of a release, sending only what changed, and waits out API rate limits.

Why: the workflow's own token (GITHUB_TOKEN) may make about 1,000 API requests an hour for the whole repository, and
every uploaded file is a request. Seven lots of 700 to 1,000 files at once cannot fit, so a run died with "API rate
limit exceeded for installation". Three things make a run fit:
  * a personal access token (secret PACKAGES_TOKEN) has its own 5,000 requests an hour, and a second one from another
    GitHub account (secret PACKAGES_TOKEN_2) doubles that: the limit belongs to the account, not to the repository.
    Both are optional (see TokenPool): one that is missing, expired, revoked or without write access is skipped with a
    warning and the run goes on with the others and the workflow's own token, so a lost token never fails the run,
  * files already on the release with the same SHA-256 are not sent again (a weekly run changes few packages, and a
    run that stopped half way continues instead of starting over),
  * when the limit is reached anyway, it keeps waiting (up to MAX_WAIT, longer than one hourly window) and goes on.

    publish_release.py TAG FOLDER [TITLE NOTES]
        needs the gh CLI and GITHUB_REPOSITORY in the environment, and these tokens (all but GH_TOKEN optional):
        PACKAGES_TOKEN, PACKAGES_TOKEN_2, GH_TOKEN (the workflow's own, the last resort), TOKEN_START (a number: which
        token this lot tries first, so lots running together spread over the accounts)

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


def is_auth_error(error):
    """True when GitHub refused the token itself (revoked, expired, deleted, or without access to the repository)."""
    text = f"{error.stderr or ''} {error.output or ''}".lower()
    return "bad credentials" in text or "http 401" in text or "resource not accessible" in text


class TokenPool:
    """The tokens a run may use, best first, and which of them is limited for now.

    A rate limit belongs to the account behind a token, so when one token is limited the next one carries on at once
    instead of the whole run waiting. A token that GitHub refuses is dropped for good (while another is left). The
    token in use is always exported as GH_TOKEN, which the gh CLI reads."""

    def __init__(self, tokens):
        self.tokens = list(tokens)  # (label, value)
        self.free_at = {}  # label -> time it may be used again
        self.current = 0
        self.apply()

    def apply(self):
        os.environ["GH_TOKEN"] = self.tokens[self.current][1]

    @property
    def label(self):
        return self.tokens[self.current][0]

    def limited(self, until):
        """The token in use cannot be used before [until]."""
        self.free_at[self.label] = until

    def pick(self, now):
        """Use the token in use if it is free, else the first free one. False when every token is limited."""
        order = [self.current] + [i for i in range(len(self.tokens)) if i != self.current]
        for i in order:
            if self.free_at.get(self.tokens[i][0], 0) <= now:
                self.current = i
                self.apply()
                return True
        return False

    def earliest(self):
        return min(self.free_at.get(label, 0) for label, _ in self.tokens)

    def drop(self):
        """Forget the token in use. False (and nothing dropped) when it is the only one left."""
        if len(self.tokens) < 2:
            return False
        print(f"::warning::{self.label} was refused by GitHub (expired, revoked or no write access): not used from now on", flush=True)
        del self.tokens[self.current]
        self.current %= len(self.tokens)
        self.apply()
        return True


POOL = None


def usable(label, value, repo):
    """(True, "") when the token may write to [repo], else (False, why). A rate-limited token counts as usable."""
    os.environ["GH_TOKEN"] = value
    try:
        out = gh("api", f"repos/{repo}", "--jq", ".permissions.push", capture=True).strip()
    except subprocess.CalledProcessError as error:
        text = f"{error.stderr or ''}".strip()
        # Only a refusal of the token itself drops it; a limit, a dropped connection or a 502 does not.
        if is_auth_error(error) or "http 404" in text.lower():
            return False, (text.splitlines() or ["refused"])[0]
        return True, ""
    return (True, "") if out == "true" else (False, "no write access to the repository")


def make_pool(repo, environ=os.environ, check=usable):
    """The tokens from the environment: PACKAGES_TOKEN and PACKAGES_TOKEN_2 when they work (the lot's TOKEN_START
    picks which goes first), then GH_TOKEN as the last resort. Empty, duplicate and refused ones are left out."""
    own = (environ.get("GH_TOKEN") or "").strip()  # read first: checking a token below changes GH_TOKEN
    candidates, seen = [], set()
    for label in ("PACKAGES_TOKEN", "PACKAGES_TOKEN_2"):
        value = (environ.get(label) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ok, why = check(label, value, repo)
        if ok:
            candidates.append((label, value))
        else:
            print(f"::warning::{label} is not used ({why}); the run goes on without it", flush=True)
    if candidates:
        try:
            start = int(environ.get("TOKEN_START") or 0) % len(candidates)
        except ValueError:
            start = 0
        candidates = candidates[start:] + candidates[:start]
    if own and own not in seen:
        candidates.append(("the workflow's own token", own))
    if not candidates:
        raise SystemExit("No GitHub token: set GH_TOKEN (the workflow's own) or PACKAGES_TOKEN")
    print("Tokens in order: " + ", ".join(label for label, _ in candidates), flush=True)
    return TokenPool(candidates)


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


def retrying(action, quota, sleep=time.sleep, now=time.time, tries=TRIES, max_wait=MAX_WAIT, pool=None):
    """Runs [action] until it works, sleeping as [quota] (-> remaining, reset) advises.

    A rate-limit error is waited out for up to [max_wait] seconds in all (a quota the API still shows as plentiful
    is no reason to hurry: the wait grows to LONG_PAUSE instead of staying at PAUSE). Any other error is tried
    [tries] times. With a [pool], a limited token hands over to the next free one without waiting, a refused token is
    dropped, and the run sleeps only when every token is limited (until the first of them is free)."""
    attempt, waited = 0, 0
    while True:
        if pool:
            pool.pick(now())
        attempt += 1
        try:
            return action()
        except subprocess.CalledProcessError as error:
            if pool and is_auth_error(error) and pool.drop():
                attempt -= 1
                continue
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
            if pool and limited:
                pool.limited(now() + wait)
                if len(pool.tokens) > 1 and pool.pick(now()):
                    print(f"{pool.tokens[pool.current][0]} takes over (the other is limited for {int(wait)} s)", flush=True)
                    attempt -= 1
                    continue
                wait = min(wait, max(pool.earliest() - now(), 1))
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
        retrying(lambda: ensure_release(repo, tag, title, notes), quota, pool=POOL)
    local = local_files(folder)
    existing = retrying(lambda: release_assets(repo, tag), quota, pool=POOL)
    delete, upload = plan(existing, local)
    print(f"{tag}: {len(local)} files, {len(upload)} to upload, {len(delete)} to remove", flush=True)
    for name in delete:
        retrying(lambda name=name: gh("release", "delete-asset", tag, name, "--yes"), quota, pool=POOL)
    sent = 0
    for chunk in batches(upload):
        paths = [os.path.join(folder, name) for name in chunk]
        retrying(lambda paths=paths: gh("release", "upload", tag, "--clobber", *paths), quota, pool=POOL)
        sent += len(chunk)
        print(f"{tag}: {sent} of {len(upload)} uploaded", flush=True)


def main(argv):
    if len(argv) not in (3, 5):
        print(__doc__)
        return 2
    global POOL
    repo = os.environ["GITHUB_REPOSITORY"]
    POOL = make_pool(repo)
    publish(argv[1], argv[2], repo, *argv[3:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
