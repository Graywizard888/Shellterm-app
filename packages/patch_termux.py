#!/usr/bin/env python3
"""Turns stock Termux packages into Shellterm packages by renaming the app inside them.

Termux binaries have their install path baked in (/data/data/com.termux/files/usr). Shellterm's app ID is exactly as
long as `com.termux`, so the name can be swapped byte for byte: no offsets move, nothing is rebuilt and ELF files
stay valid. What does change is every checksum that covers a patched file, and those are rewritten here too.

Commands:
  repo       download the packages listed in bootstrap-packages.txt (and everything they depend on), patch them
  bootstrap  download Termux's bootstrap zip, patch it and point apt at our repository
  verify     check patched output: no old name left anywhere and every checksum matches

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import io
import lzma
import re
import shutil
import sys
import tarfile
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


def rewrite_tar(name: str, data: bytes, md5s: dict[str, str] | None = None,
                collect_md5s: bool = False) -> tuple[bytes, dict[str, str]]:
    """Patches every member's name, link target and content. Returns the new .tar.xz and (path -> md5) of its files."""
    source = _open_tar(name, data)
    sums: dict[str, str] = {}
    buffer = io.BytesIO()
    # GNU format on purpose: tarfile reports PAX for every archive it reads, and PAX writes an extended header ('x')
    # for names or link targets over 100 bytes (ruby, python, perl...). dpkg rejects those with "unsupported PAX tar
    # header type 'x'"; GNU long-name records ('L', 'K') are fine for it.
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.GNU_FORMAT) as out:
        for member in source:
            info = tarfile.TarInfo(patch_text(member.name))
            info.mode, info.uid, info.gid = member.mode, member.uid, member.gid
            info.uname, info.gname, info.mtime, info.type = member.uname, member.gname, member.mtime, member.type
            if member.issym() or member.islnk():
                info.linkname = patch_text(member.linkname)
                out.addfile(info)
            elif member.isfile():
                content = patch_file(member.name, source.extractfile(member).read())
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


def deb_file_name(entry: Entry) -> str:
    # Debian leaves the epoch out of file names; a colon would also be awkward in URLs and archives.
    version = entry.version.split(":", 1)[-1]
    return f"{entry.name}_{version}_{ARCH if not entry.filename.endswith('_all.deb') else 'all'}.deb"


def cmd_repo(args: argparse.Namespace) -> None:
    index = parse_index(fetch(f"{TERMUX_REPO}/dists/stable/main/binary-{ARCH}/Packages").decode())
    wanted = read_package_list(Path(args.packages))
    if args.bootstrap:
        wanted += bootstrap_package_names(Path(args.bootstrap))
    plan = resolve(index, sorted(set(wanted)))
    total = sum(e.size for e in plan)
    print(f"{len(plan)} packages, {total / 1e6:.0f} MB to download")
    if args.plan_only:
        return

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

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


def patch_bootstrap(src: Path, dst: Path, repo_url: str) -> None:
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        contents = {i.filename: patch_file(i.filename, zin.read(i)) for i in infos if not i.is_dir()}

    # Our repository replaces Termux's mirror list (the repository is not signed yet, hence trusted=yes).
    contents[SOURCES_LIST] = (f"# Shellterm package repository\ndeb [trusted=yes] {repo_url} stable main\n").encode()

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
    src = Path(args.out + ".stock")
    if args.stock:
        src = Path(args.stock)
    else:
        src.write_bytes(fetch(BOOTSTRAP_URL))
    patch_bootstrap(src, Path(args.out), args.repo_url)
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


def verify_deb(name: str, blob: bytes) -> list[str]:
    problems = []
    members = dict(read_ar(blob))
    data_name = next(n for n in members if n.startswith("data.tar"))
    control_name = next(n for n in members if n.startswith("control.tar"))
    sums: dict[str, str] = {}
    listed: bytes | None = None
    for tar_name in (data_name, control_name):
        pax = pax_records(_decompress_tar(tar_name, members[tar_name]))
        if pax:
            problems.append(f"{name}: {tar_name} has {pax} PAX extended headers, dpkg cannot unpack them")
        tar = _open_tar(tar_name, members[tar_name])
        for member in tar:
            if OLD.decode() in member.name or OLD.decode() in (member.linkname or ""):
                problems.append(f"{name}: old name in path {member.name}")
            if not member.isfile():
                continue
            content = tar.extractfile(member).read()
            if _has_old(member.name, content):
                problems.append(f"{name}: old name inside {member.name}")
            if tar_name == data_name:
                sums[member.name.removeprefix("./")] = md5_hex(content)
            elif member.name.removeprefix("./") == "md5sums":
                listed = content
    if listed:
        for line in listed.decode().splitlines():
            digest, _, path = line.partition("  ")
            if sums.get(path) != digest:
                problems.append(f"{name}: checksum of {path} does not match")
    return problems


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
    repo.set_defaults(run=cmd_repo)

    boot = sub.add_parser("bootstrap", help="download and patch the bootstrap zip")
    boot.add_argument("--out", required=True)
    boot.add_argument("--repo-url", required=True)
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
