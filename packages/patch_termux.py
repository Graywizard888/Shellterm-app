#!/usr/bin/env python3
"""Turns stock Termux packages into Shellterm packages by renaming the app inside them.

Termux binaries have their install path baked in (/data/data/com.termux/files/usr). Shellterm's app ID is exactly as
long as `com.termux`, so the name can be swapped byte for byte: no offsets move, nothing is rebuilt and ELF files
stay valid. What does change is every checksum that covers a patched file, and those are rewritten here too.

Commands:
  repo       download the packages listed in bootstrap-packages.txt (and everything they depend on), patch them.
             With --all it takes every package of the Termux repository instead, optionally one shard of them
             (--shard 1/4), checking each one after patching (--check) and deleting it again (--discard).
  bootstrap  download Termux's bootstrap zip, patch it and point apt at our repository
  verify     check patched output: no old name left anywhere and every checksum matches

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import bz2
import concurrent.futures
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

OLD = b"com.termux"
NEW = b"com.gwterm"
assert len(OLD) == len(NEW), "the swap only works if both names have the same length"

TERMUX_REPO = "https://packages-cf.termux.dev/apt/termux-main"
BOOTSTRAP_URL = "https://github.com/termux/termux-packages/releases/latest/download/bootstrap-aarch64.zip"
ARCH = "aarch64"
SYMLINKS = "SYMLINKS.txt"
DPKG_INFO = "var/lib/dpkg/info/"
DPKG_STATUS = "var/lib/dpkg/status"
SOURCES_LIST = "etc/apt/sources.list"
DATA_ROOT = "data/data/" + NEW.decode() + "/files/usr/"


def patch_bytes(data: bytes) -> bytes:
    return data.replace(OLD, NEW)


def patch_file(name: str, data: bytes) -> bytes:
    """Patches a file's content. Gzip files (man pages) are opened, patched and packed again, deterministically."""
    if name.endswith(".gz") and data[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(data)
        except (OSError, EOFError):
            return patch_bytes(data)
        if OLD not in raw:
            return data
        return gzip.compress(patch_bytes(raw), compresslevel=9, mtime=0)
    return patch_bytes(data)


def patch_text(text: str) -> str:
    return text.replace(OLD.decode(), NEW.decode())


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ----------------------------------------------------------------------------------------------------------------
# Downloading

def fetch(url: str, attempts: int = 6) -> bytes:
    """GET with backoff; the mirrors time out now and then."""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise RuntimeError(f"Could not fetch {url}: {error}") from error
            time.sleep(attempt * 5)
    raise AssertionError("unreachable")


def fetch_file(url: str, target: Path, sha256: str, attempts: int = 6) -> None:
    """Downloads to a file (not into memory) and checks the SHA-256 from the repository index."""
    for attempt in range(1, attempts + 1):
        try:
            digest = hashlib.sha256()
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != sha256:
                raise ConnectionError("checksum mismatch")
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise RuntimeError(f"Could not fetch {url}: {error}") from error
            time.sleep(attempt * 5)


# ----------------------------------------------------------------------------------------------------------------
# Package index

@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    filename: str
    sha256: str
    size: int
    depends: tuple[str, ...]
    provides: tuple[str, ...]


def parse_index(text: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for block in text.strip().split("\n\n"):
        fields: dict[str, str] = {}
        key = None
        for line in block.split("\n"):
            if line.startswith((" ", "\t")) and key:
                fields[key] += "\n" + line
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if "Package" not in fields:
            continue
        deps = []
        for field in ("Pre-Depends", "Depends"):
            deps += [d for d in fields.get(field, "").split(",") if d.strip()]
        provides = tuple(re.sub(r"\(.*?\)", "", p).strip() for p in fields.get("Provides", "").split(",") if p.strip())
        entries[fields["Package"]] = Entry(
            name=fields["Package"],
            version=fields["Version"],
            filename=fields["Filename"],
            sha256=fields["SHA256"],
            size=int(fields["Size"]),
            depends=tuple(deps),
            provides=provides,
        )
    return entries


def resolve(index: dict[str, Entry], wanted: list[str]) -> list[Entry]:
    """Every wanted package plus what they depend on (the first alternative that exists wins)."""
    provided: dict[str, str] = {}
    for entry in index.values():
        for virtual in entry.provides:
            provided.setdefault(virtual, entry.name)

    chosen: dict[str, Entry] = {}
    stack = list(wanted)
    while stack:
        name = stack.pop()
        if name in chosen:
            continue
        entry = index.get(name) or index.get(provided.get(name, ""))
        if entry is None:
            raise RuntimeError(f"No package named '{name}' in the Termux repository")
        chosen[entry.name] = entry
        for dep in entry.depends:
            for alternative in dep.split("|"):
                alt = re.sub(r"\(.*?\)|:\w+", "", alternative).strip()
                if alt in index or alt in provided:
                    stack.append(alt)
                    break
            else:
                raise RuntimeError(f"{entry.name} needs '{dep.strip()}', which the Termux repository does not have")
    return sorted(chosen.values(), key=lambda e: e.name)


def read_package_list(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


# ----------------------------------------------------------------------------------------------------------------
# .deb files (ar archive holding control.tar.* and data.tar.*)

AR_MAGIC = b"!<arch>\n"


def read_ar(blob: bytes) -> list[tuple[str, bytes]]:
    if not blob.startswith(AR_MAGIC):
        raise ValueError("not an ar archive")
    members, pos = [], len(AR_MAGIC)
    while pos < len(blob):
        header = blob[pos:pos + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        pos += 60
        members.append((name, blob[pos:pos + size]))
        pos += size + (size & 1)
    return members


def write_ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(AR_MAGIC)
    for name, data in members:
        out += f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        out += data
        if len(data) & 1:
            out += b"\n"
    return bytes(out)


def _decompress_tar(name: str, data: bytes) -> bytes:
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".zst"):
        raise RuntimeError(f"{name}: zstd packages are not used by Termux, add support if that changes")
    if not name.endswith(".tar"):
        raise RuntimeError(f"{name}: unknown tar compression")
    return data


def _open_tar(name: str, data: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(_decompress_tar(name, data)), mode="r:")


def pax_records(raw: bytes) -> int:
    """Number of PAX extended header records ('x' or 'g') in an uncompressed tar; dpkg refuses them."""
    count, pos = 0, 0
    while pos + 512 <= len(raw):
        header = raw[pos:pos + 512]
        if header == b"\0" * 512:
            pos += 512
            continue
        if header[156:157] in (b"x", b"g"):
            count += 1
        size_field = header[124:136]
        size = int(size_field.split(b"\0")[0].strip() or b"0", 8) if size_field[0] < 0x80 else 0
        pos += 512 + (size + 511) // 512 * 512
    return count


def rewrite_tar_stream(source: tarfile.TarFile, target, md5s: dict[str, str] | None = None,
                       collect_md5s: bool = False) -> dict[str, str]:
    """Patches every member's name, link target and content while copying `source` into the open file `target`.

    Returns (path -> md5) of the patched files. `source` may be opened in streaming mode ("r|xz"), so a package with
    gigabytes of files never has to be held in memory, only one file at a time."""
    sums: dict[str, str] = {}
    # GNU format on purpose: tarfile reports PAX for every archive it reads, and PAX writes an extended header ('x')
    # for names or link targets over 100 bytes (ruby, python, perl...). dpkg rejects those with "unsupported PAX tar
    # header type 'x'"; GNU long-name records ('L', 'K') are fine for it.
    with tarfile.open(fileobj=target, mode="w|", format=tarfile.GNU_FORMAT) as out:
        for member in source:
            info = tarfile.TarInfo(patch_text(member.name))
            info.mode, info.uid, info.gid = member.mode, member.uid, member.gid
            info.uname, info.gname, info.mtime, info.type = member.uname, member.gname, member.mtime, member.type
            if member.issym() or member.islnk():
                info.linkname = patch_text(member.linkname)
                out.addfile(info)
            elif member.isfile():
                original = source.extractfile(member).read()
                content = patch_file(member.name, original)
                # The rename keeps every length, so a file only changes size when its gzip was repacked (man pages).
                if len(content) != len(original) and not member.name.endswith(".gz"):
                    raise ValueError(f"{member.name}: size changed from {len(original)} to {len(content)} bytes")
                info.size = len(content)
                if collect_md5s:
                    sums[info.name.removeprefix("./")] = md5_hex(content)
                if md5s is not None and info.name.removeprefix("./") == "md5sums":
                    content = rewrite_md5sums(content, md5s)
                    info.size = len(content)
                out.addfile(info, io.BytesIO(content))
            else:
                info.devmajor, info.devminor = member.devmajor, member.devminor
                out.addfile(info)
    return sums


def rewrite_tar(name: str, data: bytes, md5s: dict[str, str] | None = None,
                collect_md5s: bool = False) -> tuple[bytes, dict[str, str]]:
    """Patches every member's name, link target and content. Returns the new .tar.xz and (path -> md5) of its files."""
    source = _open_tar(name, data)
    buffer = io.BytesIO()
    sums = rewrite_tar_stream(source, buffer, md5s, collect_md5s)
    return lzma.compress(buffer.getvalue(), format=lzma.FORMAT_XZ, preset=6), sums


def rewrite_md5sums(content: bytes, new_sums: dict[str, str]) -> bytes:
    """Package checksum list: same file paths (renamed), new content hashes."""
    lines = []
    for line in content.decode().splitlines():
        digest, _, path = line.partition("  ")
        path = patch_text(path)
        lines.append(f"{new_sums.get(path, digest)}  {path}")
    return ("\n".join(lines) + "\n").encode()


def patch_deb(blob: bytes) -> bytes:
    members = read_ar(blob)
    data_name = next(n for n, _ in members if n.startswith("data.tar"))
    data_blob = dict(members)[data_name]
    new_data, sums = rewrite_tar(data_name, data_blob, collect_md5s=True)
    out = []
    for name, payload in members:
        if name == data_name:
            out.append(("data.tar.xz", new_data))
        elif name.startswith("control.tar"):
            new_control, _ = rewrite_tar(name, payload, md5s=sums)
            out.append(("control.tar.xz", new_control))
        else:
            out.append((name, payload))
    return write_ar(out)


# ----------------------------------------------------------------------------------------------------------------
# File-based .deb handling: for the whole repository, where a package (ghc: 226 MB packed, 3 GB unpacked) must not
# be held in memory. Everything goes through temporary files and the tars are read and written as streams.

def read_ar_to_dir(path: Path, work: Path) -> list[tuple[str, Path]]:
    """Copies each member of an ar archive into its own file under `work`."""
    members: list[tuple[str, Path]] = []
    with path.open("rb") as fp:
        if fp.read(len(AR_MAGIC)) != AR_MAGIC:
            raise ValueError("not an ar archive")
        while True:
            header = fp.read(60)
            if not header:
                break
            if len(header) < 60:
                raise ValueError("truncated ar header")
            name = header[:16].decode().strip().rstrip("/")
            size = int(header[48:58].decode().strip())
            member = work / f"member{len(members)}"
            with member.open("wb") as out:
                remaining = size
                while remaining:
                    chunk = fp.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ValueError(f"truncated ar member {name}")
                    out.write(chunk)
                    remaining -= len(chunk)
            if size & 1:
                fp.read(1)
            members.append((name, member))
    return members


def write_ar_from_files(path: Path, members: list[tuple[str, Path]]) -> None:
    with path.open("wb") as out:
        out.write(AR_MAGIC)
        for name, source in members:
            size = source.stat().st_size
            out.write(f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{'100644':<8}{size:<10}`\n".encode())
            with source.open("rb") as fp:
                shutil.copyfileobj(fp, out, 1 << 20)
            if size & 1:
                out.write(b"\n")


def _open_tar_file(name: str, path: Path) -> tarfile.TarFile:
    """Opens a tar for one sequential pass (stream mode), whatever its compression."""
    if name.endswith(".xz"):
        return tarfile.open(path, mode="r|xz")
    if name.endswith(".gz"):
        return tarfile.open(path, mode="r|gz")
    if name.endswith(".zst"):
        raise RuntimeError(f"{name}: zstd packages are not used by Termux, add support if that changes")
    if not name.endswith(".tar"):
        raise RuntimeError(f"{name}: unknown tar compression")
    return tarfile.open(path, mode="r|")


def compress_xz(source: Path, target: Path) -> None:
    """xz at the same preset as the in-memory path. The xz tool is used when there is one (it is much faster)."""
    if shutil.which("xz"):
        with target.open("wb") as out:
            subprocess.run(["xz", "-6", "-T1", "-c", str(source)], stdout=out, check=True)
        return
    with source.open("rb") as fp, lzma.open(target, "wb", format=lzma.FORMAT_XZ, preset=6) as out:
        shutil.copyfileobj(fp, out, 1 << 20)


def patch_deb_file(src: Path, dst: Path, work: Path) -> None:
    members = read_ar_to_dir(src, work)
    data_name = next(n for n, _ in members if n.startswith("data.tar"))
    out: list[tuple[str, Path]] = []
    sums: dict[str, str] = {}
    # The data tar comes first: the checksum list in the control tar needs the new hashes.
    for name, path in members:
        if name == data_name:
            raw = work / "data.patched.tar"
            with _open_tar_file(name, path) as source, raw.open("wb") as target:
                sums = rewrite_tar_stream(source, target, collect_md5s=True)
            packed = work / "data.patched.tar.xz"
            compress_xz(raw, packed)
            raw.unlink()
            path.unlink()
            out.append(("data.tar.xz", packed))
    for name, path in members:
        if name == data_name:
            continue
        if name.startswith("control.tar"):
            raw = work / "control.patched.tar"
            with _open_tar_file(name, path) as source, raw.open("wb") as target:
                rewrite_tar_stream(source, target, md5s=sums)
            packed = work / "control.patched.tar.xz"
            compress_xz(raw, packed)
            raw.unlink()
            out.append(("control.tar.xz", packed))
        else:
            out.append((name, path))
    # Same member order as the stock package: debian-binary, control, data.
    order = {"debian-binary": 0, "control.tar.xz": 1, "data.tar.xz": 2}
    out.sort(key=lambda item: order.get(item[0], 3))
    # Written under a temporary name and renamed, so a package file that exists is always a complete one.
    partial = dst.with_name(dst.name + ".part")
    write_ar_from_files(partial, out)
    partial.replace(dst)


def deb_file_name(entry: Entry) -> str:
    # Debian leaves the epoch out of file names; a colon would also be awkward in URLs and archives. Characters other
    # than letters, digits, dot, dash and underscore (the + in libc++, a ~ in a version) become "_": GitHub renames
    # such characters in release assets, and the file name in the repository index must match the asset exactly.
    version = entry.version.split(":", 1)[-1]
    name = f"{entry.name}_{version}_{ARCH if not entry.filename.endswith('_all.deb') else 'all'}.deb"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def split_into_shards(plan: list[Entry], shard: str) -> list[Entry]:
    """Shard "2/4" is the second of four lots. A package's lot depends only on its name, so it stays in the same
    release from one run to the next (a package that moved would be missing for a while during an update)."""
    number, _, total = shard.partition("/")
    number, total = int(number), int(total)
    if not 1 <= number <= total:
        raise SystemExit(f"--shard {shard}: expected K/N with 1 <= K <= N")
    return sorted((e for e in plan if shard_of(e.name, total) == number), key=lambda e: e.name)


def shard_of(name: str, total: int) -> int:
    """Lot number, 1..total, of a package."""
    return int(hashlib.sha256(name.encode()).hexdigest(), 16) % total + 1


def dpkg_check(deb: Path, entry: Entry, work: Path) -> list[str]:
    """What dpkg itself makes of a patched package: readable control data, same name and version, unpacks cleanly."""
    problems = []
    fields = subprocess.run(["dpkg-deb", "-f", str(deb), "Package", "Version"], capture_output=True, text=True)
    if fields.returncode != 0:
        return [f"{entry.name}: dpkg-deb cannot read the control data: {fields.stderr.strip()[:200]}"]
    if f"Package: {entry.name}" not in fields.stdout or f"Version: {entry.version}" not in fields.stdout:
        problems.append(f"{entry.name}: name or version changed: {fields.stdout.strip()!r}")
    listing = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True)
    if listing.returncode != 0:
        problems.append(f"{entry.name}: dpkg-deb cannot list the files: {listing.stderr.strip()[:200]}")
        return problems
    target = work / "unpacked"
    unpack = subprocess.run(["dpkg-deb", "-x", str(deb), str(target)], capture_output=True, text=True)
    shutil.rmtree(target, ignore_errors=True)
    if unpack.returncode != 0:
        problems.append(f"{entry.name}: dpkg-deb cannot unpack it: {unpack.stderr.strip()[:200]}")
    return problems


def process_package(entry: Entry, out: Path, scratch: Path, check: bool, discard: bool) -> dict:
    """Downloads one package, patches it, and (with check) verifies it. Returns a small report."""
    started = time.time()
    work = Path(tempfile.mkdtemp(prefix=entry.name + ".", dir=scratch))
    report: dict = {"name": entry.name, "version": entry.version, "size": entry.size, "file": deb_file_name(entry),
                    "problems": [], "warnings": []}
    try:
        stock = work / "stock.deb"
        fetch_file(f"{TERMUX_REPO}/{urllib.parse.quote(entry.filename)}", stock, entry.sha256)
        parts = work / "parts"
        parts.mkdir()
        patched = out / deb_file_name(entry)
        patch_deb_file(stock, patched, parts)
        stock.unlink()
        shutil.rmtree(parts, ignore_errors=True)
        report["patched_size"] = patched.stat().st_size
        if check:
            check_work = work / "check"
            check_work.mkdir()
            report["problems"] += verify_deb_file(entry.name, patched, check_work, report["warnings"])
            report["problems"] += dpkg_check(patched, entry, work)
        if discard:
            patched.unlink()
    except Exception as error:  # one broken package must not stop the others; it is reported
        report["problems"].append(f"{entry.name}: {type(error).__name__}: {error}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["seconds"] = round(time.time() - started, 1)
    return report


def cmd_repo(args: argparse.Namespace) -> None:
    index = parse_index(fetch(f"{TERMUX_REPO}/dists/stable/main/binary-{ARCH}/Packages").decode())
    if args.all:
        plan = sorted(index.values(), key=lambda e: e.name)
        if args.only:
            wanted_names = set(args.only.split(","))
            plan = [e for e in plan if e.name in wanted_names]
        if args.shard:
            plan = split_into_shards(plan, args.shard)
    else:
        wanted = read_package_list(Path(args.packages))
        if args.bootstrap:
            wanted += bootstrap_package_names(Path(args.bootstrap))
        plan = resolve(index, sorted(set(wanted)))
    names = [deb_file_name(e) for e in plan]
    clashes = sorted({n for n in names if names.count(n) > 1})
    if clashes:
        raise SystemExit(f"Two packages would get the same file name: {', '.join(clashes)}")
    total = sum(e.size for e in plan)
    print(f"{len(plan)} packages, {total / 1e6:.0f} MB to download")
    if args.plan_only:
        return

    out = Path(args.out)
    done_before: set[str] = set()
    if args.all and args.resume and args.report and Path(args.report).exists():
        # Carry on after an interrupted run: packages the report lists without problems (and whose file is there) are
        # kept and skipped; anything else in the folder, such as a half-written file, is removed.
        file_of = {e.name: deb_file_name(e) for e in plan}
        for line in Path(args.report).read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a half-written last line
            name = record.get("name")
            if name in file_of and not record["problems"] and (out / file_of[name]).is_file():
                done_before.add(name)
        keep = {file_of[name] for name in done_before}
        for stray in out.glob("*"):
            if stray.name not in keep:
                stray.unlink()
        plan = [e for e in plan if e.name not in done_before]
        print(f"Resuming: {len(done_before)} packages already done, {len(plan)} to go")
    else:
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    if not args.all:
        def work(entry: Entry) -> str:
            blob = fetch(f"{TERMUX_REPO}/{urllib.parse.quote(entry.filename)}")
            if hashlib.sha256(blob).hexdigest() != entry.sha256:
                raise RuntimeError(f"Checksum mismatch for {entry.name}")
            (out / deb_file_name(entry)).write_bytes(patch_deb(blob))
            return entry.name

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for done, name in enumerate(pool.map(work, plan), 1):
                if done % 25 == 0 or done == len(plan):
                    print(f"  {done}/{len(plan)} {name}")
        return

    # Every package of the repository: one process per package (patching is CPU work), biggest first so the last
    # ones are small, streamed through temporary files.
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="patch-scratch-"))
    scratch.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    warned = 0
    started = time.time()
    report_file = open(args.report, "a" if done_before else "w") if args.report else None
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(process_package, e, out, scratch, args.check, args.discard)
                   for e in sorted(plan, key=lambda e: -e.size)]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            report = future.result()
            if report_file:
                report_file.write(json.dumps(report) + "\n")
                report_file.flush()
            warned += len(report["warnings"])
            for line in report["problems"] + report["warnings"]:
                print(line, file=sys.stderr)
            if report["problems"]:
                failed.append(report["name"])
            if done % 25 == 0 or done == len(plan):
                print(f"  {done}/{len(plan)} done, {len(failed)} with problems, {warned} warnings, "
                      f"{(time.time() - started) / 60:.1f} min", flush=True)
    if report_file:
        report_file.close()
    shutil.rmtree(scratch, ignore_errors=True)
    if failed:
        sys.exit(f"{len(failed)} packages with problems: {', '.join(sorted(failed))}")
    print(f"All {len(plan)} packages patched" + (" and checked" if args.check else ""))


# ----------------------------------------------------------------------------------------------------------------
# Bootstrap zip

def bootstrap_package_names(zip_path: Path) -> list[str]:
    """Packages the stock bootstrap already has installed, read from its dpkg status file."""
    with zipfile.ZipFile(zip_path) as zf:
        status = zf.read(DPKG_STATUS).decode()
    return re.findall(r"^Package: (.+)$", status, re.MULTILINE)


def fix_conffile_sums(status: str, sums: dict[str, str]) -> str:
    """dpkg remembers each config file's original hash; it must match the patched file or upgrades ask questions."""
    lines = []
    in_conffiles = False
    for line in status.split("\n"):
        if line.startswith("Conffiles:"):
            in_conffiles = True
        elif in_conffiles and line.startswith(" "):
            parts = line.split()  # path, hash, and sometimes "obsolete"
            if len(parts) >= 2:
                parts[1] = sums.get(parts[0].removeprefix("/"), parts[1])
                line = " " + " ".join(parts)
        else:
            in_conffiles = False
        lines.append(line)
    return "\n".join(lines)


def patch_bootstrap(src: Path, dst: Path, repo_url: str, flat_repos: tuple[str, ...] = ()) -> None:
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        contents = {i.filename: patch_file(i.filename, zin.read(i)) for i in infos if not i.is_dir()}

    # Our repository replaces Termux's mirror list (the repository is not signed yet, hence trusted=yes). Flat
    # repositories (the release assets) are written with a "/" suite, which makes apt fetch plain file URLs.
    if flat_repos:
        sources = "".join(f"deb [trusted=yes] {url} /\n" for url in flat_repos)
    else:
        sources = f"deb [trusted=yes] {repo_url} stable main\n"
    contents[SOURCES_LIST] = f"# Shellterm package repository\n{sources}".encode()

    # file path inside the zip (relative to usr/) -> hash of the patched file, in dpkg's path spelling
    sums = {DATA_ROOT + name: md5_hex(data) for name, data in contents.items() if not name.startswith(DPKG_INFO)}
    for name in list(contents):
        if name.startswith(DPKG_INFO) and name.endswith(".md5sums"):
            contents[name] = rewrite_md5sums(contents[name], sums)
    status = contents[DPKG_STATUS].decode()
    contents[DPKG_STATUS] = fix_conffile_sums(status, sums).encode()

    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        for info in infos:
            if info.is_dir():
                zout.writestr(info, b"")
                continue
            copy = zipfile.ZipInfo(info.filename, info.date_time)
            copy.external_attr = info.external_attr
            copy.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(copy, contents[info.filename])
    (dst.parent / (dst.name + ".sha256")).write_text(
        f"{hashlib.sha256(dst.read_bytes()).hexdigest()}  {dst.name}\n")


def cmd_bootstrap(args: argparse.Namespace) -> None:
    if not args.repo_url and not args.flat_repo:
        sys.exit("bootstrap: give --repo-url or at least one --flat-repo")
    src = Path(args.out + ".stock")
    if args.stock:
        src = Path(args.stock)
    else:
        src.write_bytes(fetch(BOOTSTRAP_URL))
    patch_bootstrap(src, Path(args.out), args.repo_url or "", tuple(args.flat_repo))
    print(f"Bootstrap ready: {Path(args.out).stat().st_size / 1e6:.1f} MB")


# ----------------------------------------------------------------------------------------------------------------
# Verification

def _has_old(name: str, data: bytes) -> bool:
    if name.endswith(".gz") and data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            pass
    return OLD in data


def _raw_tar(name: str, path: Path):
    """The tar behind a .deb member as a stream of uncompressed bytes."""
    if name.endswith(".xz"):
        return lzma.open(path, "rb")
    if name.endswith(".gz"):
        return gzip.open(path, "rb")
    if name.endswith(".tar"):
        return path.open("rb")
    raise RuntimeError(f"{name}: unknown tar compression")


def pax_records_stream(fp) -> int:
    """Number of PAX extended header records ('x' or 'g') in an uncompressed tar read as a stream."""
    count = 0
    while True:
        header = fp.read(512)
        if len(header) < 512:
            return count
        if header == b"\0" * 512:
            continue
        if header[156:157] in (b"x", b"g"):
            count += 1
        size_field = header[124:136]
        size = int(size_field.split(b"\0")[0].strip() or b"0", 8) if size_field[0] < 0x80 else 0
        skip = (size + 511) // 512 * 512
        while skip:
            chunk = fp.read(min(skip, 1 << 20))
            if not chunk:
                return count
            skip -= len(chunk)


def compressed_has_old(name: str, data: bytes) -> bool:
    """True when the old name hides inside a compressed file (jar or zip entry, bz2, xz or gzip that is not a man
    page). Those cannot be patched byte for byte: the compression and its checksums would have to be redone."""
    if name.endswith(".gz") and "/man/" in name:
        return False
    try:
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return any(OLD in zf.read(info) for info in zf.infolist() if not info.is_dir() and info.file_size < 1 << 28)
        if data[:2] == b"\x1f\x8b":
            return OLD in gzip.decompress(data)
        if data[:3] == b"BZh":
            return OLD in bz2.decompress(data)
        if data[:6] == b"\xfd7zXZ\x00":
            return OLD in lzma.decompress(data)
    except (OSError, EOFError, ValueError, zipfile.BadZipFile, lzma.LZMAError, RuntimeError):
        return False
    return False


def verify_deb_file(name: str, path: Path, work: Path, warnings: list[str] | None = None) -> list[str]:
    problems = []
    members = read_ar_to_dir(path, work)
    names = [n for n, _ in members]
    data_name = next(n for n in names if n.startswith("data.tar"))
    control_name = next(n for n in names if n.startswith("control.tar"))
    files = dict(members)
    sums: dict[str, str] = {}
    listed: bytes | None = None
    for tar_name in (data_name, control_name):
        with _raw_tar(tar_name, files[tar_name]) as raw:
            pax = pax_records_stream(raw)
        if pax:
            problems.append(f"{name}: {tar_name} has {pax} PAX extended headers, dpkg cannot unpack them")
        with _open_tar_file(tar_name, files[tar_name]) as tar:
            for member in tar:
                if OLD.decode() in member.name or OLD.decode() in (member.linkname or ""):
                    problems.append(f"{name}: old name in path {member.name}")
                if not member.isfile():
                    continue
                content = tar.extractfile(member).read()
                if _has_old(member.name, content):
                    problems.append(f"{name}: old name inside {member.name}")
                elif warnings is not None and compressed_has_old(member.name, content):
                    warnings.append(f"{name}: old name inside the compressed file {member.name}")
                if tar_name == data_name:
                    sums[member.name.removeprefix("./")] = md5_hex(content)
                elif member.name.removeprefix("./") == "md5sums":
                    listed = content
    if listed:
        for line in listed.decode().splitlines():
            digest, _, listed_path = line.partition("  ")
            if sums.get(listed_path) != digest:
                problems.append(f"{name}: checksum of {listed_path} does not match")
    return problems


def verify_deb(name: str, blob: bytes) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        deb = Path(tmp) / "package.deb"
        deb.write_bytes(blob)
        work = Path(tmp) / "work"
        work.mkdir()
        return verify_deb_file(name, deb, work)


def verify_bootstrap(path: Path) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
    for name, data in contents.items():
        if _has_old(name, data):
            problems.append(f"bootstrap: old name inside {name}")
    for name, data in contents.items():
        if name.startswith(DPKG_INFO) and name.endswith(".md5sums"):
            for line in data.decode().splitlines():
                digest, _, file_path = line.partition("  ")
                relative = file_path.removeprefix(DATA_ROOT)
                if relative in contents and md5_hex(contents[relative]) != digest:
                    problems.append(f"bootstrap: checksum of {relative} does not match ({name})")
    if not contents.get(SOURCES_LIST, b"").startswith(b"#"):
        problems.append("bootstrap: etc/apt/sources.list is missing")
    return problems


def cmd_verify(args: argparse.Namespace) -> None:
    problems = []
    if args.bootstrap:
        problems += verify_bootstrap(Path(args.bootstrap))
    if args.debs:
        for deb in sorted(Path(args.debs).glob("*.deb")):
            problems += verify_deb(deb.name, deb.read_bytes())
    for problem in problems[:50]:
        print(problem, file=sys.stderr)
    if problems:
        sys.exit(f"{len(problems)} problems found")
    print("Verified: no trace of the old name, all checksums match")


# ----------------------------------------------------------------------------------------------------------------

def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo", help="download and patch packages")
    repo.add_argument("--packages", default=str(Path(__file__).with_name("bootstrap-packages.txt")))
    repo.add_argument("--bootstrap", help="stock bootstrap zip: its installed packages are added to the repository")
    repo.add_argument("--out", required=True)
    repo.add_argument("--plan-only", action="store_true")
    repo.add_argument("--all", action="store_true", help="every package of the Termux repository, not just the list")
    repo.add_argument("--only", help="with --all: only these packages (comma separated), for tests")
    repo.add_argument("--shard", help="with --all: K/N, the K-th of N lots of about equal size")
    repo.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="with --all: packages patched at once")
    repo.add_argument("--check", action="store_true", help="with --all: verify each package after patching")
    repo.add_argument("--discard", action="store_true", help="with --all: delete each package once checked")
    repo.add_argument("--scratch", help="with --all: folder for temporary files")
    repo.add_argument("--report", help="with --all: write one JSON line per package here")
    repo.add_argument("--resume", action="store_true",
                      help="with --all and --report: skip packages an earlier, interrupted run already finished")
    repo.set_defaults(run=cmd_repo)

    boot = sub.add_parser("bootstrap", help="download and patch the bootstrap zip")
    boot.add_argument("--out", required=True)
    boot.add_argument("--repo-url", help="apt repository (dists/stable/main layout) apt is pointed at")
    boot.add_argument("--flat-repo", action="append", default=[],
                      help="flat apt repository (all files in one folder), repeatable; used instead of --repo-url")
    boot.add_argument("--stock", help="use this stock zip instead of downloading one")
    boot.set_defaults(run=cmd_bootstrap)


    check = sub.add_parser("verify", help="check patched packages and bootstrap")
    check.add_argument("--bootstrap")
    check.add_argument("--debs")
    check.set_defaults(run=cmd_verify)

    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main(sys.argv[1:])
