"""Start-up shim for Go programs that do not use libc.

Android 10+ will not execute files from an app's data directory, so Shellterm starts every program there as
`/system/bin/linker64 <program> <args>`. A normal program (linked to libc) never notices: bionic's startup code drops the
two leading words again. A Go program built without cgo has no libc. It reads the raw start-up stack itself, sees
argv[0] = /system/bin/linker64 and argv[1] = the program, and so runs with the wrong command line (`pup 'p text{}'` was
read as "pup /system/bin/linker64 ...", printed nothing and exited).

The fix here is a 116-byte arm64 stub placed in the zero padding behind the executable segment, and made the new entry
point of the program. When argv[0] ends in `linker64` it removes that first argument (argc - 1, argv, envp and the auxiliary
vector move down by one word, the stack pointer stays where it was and stays 16-byte aligned), then jumps to the original
entry point. In every other case (the program started directly) it changes nothing. The stub's source is go_shim.S.

Nothing is rebuilt and the file keeps its exact size; only the entry point and the executable segment's length change.
"""
from __future__ import annotations

import struct

# Output of `aarch64-linux-android29-clang -c go_shim.S`, without the final placeholder branch (see go_shim.S).
SHIM_WORDS = (
    0x910003e9, 0xf9400120, 0xf100081f, 0x5400032b,
    0xf9400521, 0x38401422, 0x35ffffe2, 0xd1002421,
    0xf9400022, 0xd28d2d83, 0xf2ad6dc3, 0xf2ce4ca3,
    0xf2e686c3, 0xeb03005f, 0x540001c1, 0xd1000400,
    0xf9000120, 0x91004122, 0x91002123, 0xf8408444,
    0xf8008464, 0xb5ffffc4, 0xf8408444, 0xf8008464,
    0xb5ffffc4, 0xa8c11444, 0xa8811464, 0xb5ffffc4,
)
SHIM_BODY = struct.pack(f"<{len(SHIM_WORDS)}I", *SHIM_WORDS)
SHIM_SIZE = len(SHIM_BODY) + 4          # plus the branch to the original entry point

GO_BUILDINFO = b"\xff Go buildinf:"
INTERP = b"/system/bin/linker64\x00"

EM_AARCH64 = 183
ET_DYN = 3
PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
PF_X = 1
DT_NULL, DT_NEEDED = 0, 1


def _branch(from_addr: int, to_addr: int) -> bytes | None:
    delta = to_addr - from_addr
    if delta % 4 or not -(1 << 27) <= delta < (1 << 27):
        return None
    return struct.pack("<I", 0x14000000 | ((delta >> 2) & 0x3FFFFFF))


def is_patched(data: bytes) -> bool:
    """True when the entry point already is this shim."""
    entry = _entry_offset(data)
    return entry is not None and data[entry:entry + len(SHIM_BODY)] == SHIM_BODY


def _program_headers(data: bytes) -> list[tuple[int, int, int, int, int, int, int]]:
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    if phentsize < 56:
        return []
    headers = []
    for i in range(phnum):
        base = phoff + i * phentsize
        if base + 56 > len(data):
            return []
        # p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz
        p_type, p_flags, p_offset, p_vaddr, _paddr, p_filesz, p_memsz = struct.unpack_from("<IIQQQQQ", data, base)
        headers.append((p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz, base))
    return headers


def _entry_offset(data: bytes) -> int | None:
    """File offset of the entry point, or None when the file is not a program this module understands."""
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    if e_type != ET_DYN or e_machine != EM_AARCH64:
        return None
    entry = struct.unpack_from("<Q", data, 24)[0]
    for p_type, p_flags, p_offset, p_vaddr, p_filesz, _memsz, _base in _program_headers(data):
        if p_type == PT_LOAD and p_flags & PF_X and p_vaddr <= entry < p_vaddr + p_filesz:
            return entry - p_vaddr + p_offset
    return None


def _needs_libraries(data: bytes, headers) -> bool:
    for p_type, _flags, p_offset, _vaddr, p_filesz, _memsz, _base in headers:
        if p_type == PT_DYNAMIC:
            for pos in range(p_offset, min(p_offset + p_filesz, len(data) - 15), 16):
                tag = struct.unpack_from("<q", data, pos)[0]
                if tag == DT_NEEDED:
                    return True
                if tag == DT_NULL:
                    break
    return False


def state(data: bytes) -> str | None:
    """None for a file this module leaves alone, "patched" for a Go program without libc that has the stub and
    "unpatched" for one that still needs it (so the stub could not be added: that program will not work)."""
    if _entry_offset(data) is None or GO_BUILDINFO not in data:
        return None
    headers = _program_headers(data)
    if not any(h[0] == PT_INTERP and data[h[2]:h[2] + h[4]] == INTERP for h in headers) or _needs_libraries(data, headers):
        return None
    return "patched" if is_patched(data) else "unpatched"


def patch_go_entry(data: bytes) -> bytes:
    """Adds the start-up shim to a Go program without libc (a PIE started through linker64). Anything else, and a
    program that already has the shim, is returned unchanged. The result always has the same length as the input."""
    if _entry_offset(data) is None or GO_BUILDINFO not in data:
        return data
    headers = _program_headers(data)
    if not any(h[0] == PT_INTERP and data[h[2]:h[2] + h[4]] == INTERP for h in headers):
        return data
    if _needs_libraries(data, headers):
        return data                      # linked to libc: bionic already hides the linker's words
    entry = struct.unpack_from("<Q", data, 24)[0]
    code = next((h for h in headers if h[0] == PT_LOAD and h[1] & PF_X and h[3] <= entry < h[3] + h[4]), None)
    if code is None or is_patched(data):
        return data
    _type, _flags, offset, vaddr, filesz, memsz, header_base = code
    if memsz != filesz:
        return data
    at = (offset + filesz + 15) & ~15                     # 16-aligned file offset of the stub
    end = at + SHIM_SIZE
    # The stub goes into padding: all zero, and in front of whatever the next segment (or section data) starts with.
    following = [h[2] for h in headers if h[2] > offset and h[0] == PT_LOAD]
    if end > len(data) or any(data[offset + filesz:end]) or (following and end > min(following)):
        return data
    shim_vaddr = vaddr + (at - offset)
    jump = _branch(shim_vaddr + len(SHIM_BODY), entry)
    if jump is None:
        return data
    out = bytearray(data)
    out[at:end] = SHIM_BODY + jump
    new_size = end - offset
    struct.pack_into("<QQ", out, header_base + 32, new_size, new_size)     # p_filesz, p_memsz
    struct.pack_into("<Q", out, 24, shim_vaddr)                            # e_entry
    return bytes(out)
