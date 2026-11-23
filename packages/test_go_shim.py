"""Tests for go_shim.py. Run: python3 -m unittest discover -s packages -v

The stub is also run for real when the `unicorn` CPU emulator is installed (pip install unicorn): it starts a patched program
on a start-up stack as the kernel builds it for `linker64 <program> <args>` and checks what the Go runtime would find there.
"""
import struct
import unittest

import go_shim as gs

try:
    import unicorn
    from unicorn import arm64_const as uc_arm64
except ImportError:                                         # pragma: no cover
    unicorn = None

BASE = 0x10000
ORIGINAL_ENTRY = BASE + 0x400
LOAD_SIZE = 0x1000              # executable segment, file offset 0
NEXT_LOAD = 0x4000              # the next segment starts here in the file; the zeros in between are the padding


def make_go_program(needed: bool = False, interp: bytes = gs.INTERP, buildinfo: bool = True, filler: bytes = b"") -> bytes:
    """A minimal arm64 PIE with the parts patch_go_entry looks at; the code at the entry point is `ret` (0xd65f03c0)."""
    image = bytearray(NEXT_LOAD + 0x1000)
    phnum = 4
    image[:16] = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", image, 16, gs.ET_DYN, gs.EM_AARCH64, 1, ORIGINAL_ENTRY, 64, 0, 0, 64, 56, phnum, 64, 0, 0)
    interp_at, dyn_at = 0x200, 0x300
    headers = [
        (gs.PT_INTERP, 4, interp_at, BASE + interp_at, len(interp), len(interp)),
        (gs.PT_LOAD, 5, 0, BASE, LOAD_SIZE, LOAD_SIZE),
        (gs.PT_LOAD, 4, NEXT_LOAD, BASE + NEXT_LOAD, 0x100, 0x100),
        (gs.PT_DYNAMIC, 6, dyn_at, BASE + dyn_at, 32, 32),
    ]
    for i, (kind, flags, offset, vaddr, filesz, memsz) in enumerate(headers):
        struct.pack_into("<IIQQQQQQ", image, 64 + i * 56, kind, flags, offset, vaddr, vaddr, filesz, memsz, 0x1000)
    image[interp_at:interp_at + len(interp)] = interp
    struct.pack_into("<qq", image, dyn_at, gs.DT_NEEDED if needed else 1000, 1)       # 1000: some unrelated tag
    struct.pack_into("<qq", image, dyn_at + 16, gs.DT_NULL, 0)
    image[0x400:0x404] = struct.pack("<I", 0xd65f03c0)
    if buildinfo:
        image[0x500:0x500 + len(gs.GO_BUILDINFO)] = gs.GO_BUILDINFO
    image[0x800:0x800 + len(filler)] = filler
    return bytes(image)


def entry_of(data: bytes) -> int:
    return struct.unpack_from("<Q", data, 24)[0]


class PatchTests(unittest.TestCase):
    def test_go_program_gets_the_shim_and_keeps_its_size(self):
        original = make_go_program()
        patched = gs.patch_go_entry(original)
        self.assertNotEqual(original, patched)
        self.assertEqual(len(original), len(patched))
        self.assertTrue(gs.is_patched(patched))
        self.assertNotEqual(entry_of(patched), ORIGINAL_ENTRY)
        # the code segment now covers the stub; every other byte outside it is untouched
        _, _, _, _, filesz, memsz, _ = gs._program_headers(patched)[1]
        self.assertEqual((filesz, memsz), (entry_of(patched) - BASE + gs.SHIM_SIZE,) * 2)
        changed = [i for i in range(len(original)) if original[i] != patched[i]]
        allowed = set(range(24, 32)) | set(range(64 + 56 + 32, 64 + 56 + 48)) | set(range(LOAD_SIZE, NEXT_LOAD))
        self.assertTrue(set(changed) <= allowed, "bytes changed outside the entry point, segment size and padding")

    def test_patching_twice_changes_nothing(self):
        once = gs.patch_go_entry(make_go_program())
        self.assertEqual(once, gs.patch_go_entry(once))

    def test_programs_that_are_not_go_without_libc_stay_as_they_are(self):
        for case, program in {
            "linked to libc": make_go_program(needed=True),
            "no Go build info": make_go_program(buildinfo=False),
            "other interpreter": make_go_program(interp=b"/lib/ld-musl-aarch64.so.1\x00"),
            "not an ELF file": b"#!/bin/sh\necho hi\n" * 100,
            "empty": b"",
            "truncated": make_go_program()[:50],
        }.items():
            with self.subTest(case):
                self.assertEqual(program, gs.patch_go_entry(program))

    def test_no_room_means_no_change(self):
        # the padding is not zero (something lives there), so the stub must not be written over it
        crowded = make_go_program(filler=b"")
        crowded = crowded[:LOAD_SIZE] + b"\x01" * 64 + crowded[LOAD_SIZE + 64:]
        self.assertEqual(crowded, gs.patch_go_entry(crowded))

    def test_other_architectures_stay_as_they_are(self):
        program = bytearray(make_go_program())
        struct.pack_into("<H", program, 18, 62)                 # x86-64
        self.assertEqual(bytes(program), gs.patch_go_entry(bytes(program)))

    def test_patch_file_applies_the_shim(self):
        import patch_termux as pt
        program = make_go_program()
        self.assertTrue(gs.is_patched(pt.patch_file("data/data/com.gwterm/files/usr/bin/pup", program)))


class PackageTests(unittest.TestCase):
    """The packages workflow's own check: patched packages must carry the stub, and a package that lacks it is reported."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def deb(self, program: bytes, app: str = "com.termux") -> bytes:
        import patch_termux as pt
        import test_patch_termux as tp
        elf = tp.ELF_LIKE.replace(b"com.termux", app.encode())
        data = tp.make_tar([(f"./data/data/{app}/files/usr/bin/pup", program), (f"./data/data/{app}/files/usr/bin/tool", elf)])
        control = tp.make_tar([("./control", b"Package: pup\nVersion: 1.0\nArchitecture: aarch64\nDescription: x\n")])
        return pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", data)])

    def verify(self, blob: bytes, strict: bool):
        import patch_termux as pt
        path = self.root / "p.deb"
        path.write_bytes(blob)
        work = self.root / f"w{len(list(self.root.iterdir()))}"
        work.mkdir()
        warnings, programs = [], []
        problems = pt.verify_deb_file("pup", path, work, None if strict else warnings, programs)
        return problems, warnings, programs

    def patched_deb(self, program: bytes) -> bytes:
        import patch_termux as pt
        src, dst, work = self.root / "src.deb", self.root / "dst.deb", self.root / "work"
        src.write_bytes(self.deb(program))
        work.mkdir(exist_ok=True)
        pt.patch_deb_file(src, dst, work)
        return dst.read_bytes()

    def test_a_patched_package_passes_and_names_its_go_program(self):
        problems, warnings, programs = self.verify(self.patched_deb(make_go_program()), strict=False)
        self.assertEqual((problems, warnings), ([], []))
        self.assertEqual(programs, ["pup: pup"])

    def test_a_go_program_without_the_stub_is_reported(self):
        unpatched = self.deb(make_go_program(), app="com.gwterm")       # right name, no stub
        for strict in (False, True):                     # in a run (with warnings) and in the `verify` command
            problems, warnings, programs = self.verify(unpatched, strict=strict)
            self.assertTrue(any("no start-up stub" in p for p in problems), "it fails the run")
            self.assertEqual((warnings, programs), ([], []))

    def test_programs_that_are_not_go_without_libc_are_not_mentioned(self):
        for program in (make_go_program(needed=True), make_go_program(buildinfo=False)):
            problems, warnings, programs = self.verify(self.patched_deb(program), strict=False)
            self.assertEqual((problems, warnings, programs), ([], [], []))


@unittest.skipIf(unicorn is None, "the unicorn CPU emulator is not installed")
class RunTests(unittest.TestCase):
    STACK_TOP = 0x7ff000000000

    def run_stub(self, program: bytes, argv: list[bytes], env: list[bytes]):
        """Runs the program's entry stub on a kernel-style start-up stack; returns (argc, argv, env, auxv, sp, entry reached)."""
        mu = unicorn.Uc(unicorn.UC_ARCH_ARM64, unicorn.UC_MODE_ARM)
        mu.mem_map(BASE, 0x10000)
        mu.mem_write(BASE, program[:NEXT_LOAD])
        stack = self.STACK_TOP - 0x10000
        mu.mem_map(stack, 0x10000)
        strings = b"".join(item + b"\x00" for item in argv + env)
        text_at = self.STACK_TOP - 0x1000
        mu.mem_write(text_at, strings)
        pointers, cursor = [], text_at
        for item in argv + env:
            pointers.append(cursor)
            cursor += len(item) + 1
        auxv = [(6, 4096), (25, text_at - 16), (31, text_at), (0, 0)]            # PAGESZ, RANDOM, EXECFN, NULL
        words = [len(argv), *pointers[:len(argv)], 0, *pointers[len(argv):], 0] + [w for pair in auxv for w in pair]
        sp = (self.STACK_TOP - 0x2000 - len(words) * 8) & ~15
        mu.mem_write(sp, struct.pack(f"<{len(words)}Q", *words))
        before = bytes(mu.mem_read(sp, len(words) * 8 + 64))
        mu.reg_write(uc_arm64.UC_ARM64_REG_SP, sp)
        mu.emu_start(entry_of(program), ORIGINAL_ENTRY, count=2000)
        self.assertEqual(mu.reg_read(uc_arm64.UC_ARM64_REG_PC), ORIGINAL_ENTRY, "the stub must end at the original entry point")
        now_sp = mu.reg_read(uc_arm64.UC_ARM64_REG_SP)

        def word(index):
            return struct.unpack("<Q", bytes(mu.mem_read(now_sp + index * 8, 8)))[0]

        def string(address):
            out = bytearray()
            while (byte := mu.mem_read(address, 1)[0]):
                out.append(byte)
                address += 1
            return bytes(out)

        argc = word(0)
        args = [string(word(1 + i)) for i in range(argc)]
        assert word(1 + argc) == 0, "argv must end with a null pointer"
        index, found = 2 + argc, []
        while word(index):
            found.append(string(word(index)))
            index += 1
        index += 1
        aux = []
        while True:
            kind, value = word(index), word(index + 1)
            aux.append((kind, value))
            index += 2
            if kind == 0:
                break
        return argc, args, found, aux, now_sp, sp, before

    def test_started_through_linker64_the_linker_word_is_dropped(self):
        program = gs.patch_go_entry(make_go_program())
        argc, args, env, aux, now_sp, sp, _ = self.run_stub(
            program, [b"/system/bin/linker64", b"/data/data/com.gwterm/files/usr/bin/pup", b"p text{}"], [b"HOME=/h", b"PATH=/p"])
        self.assertEqual(argc, 2)
        self.assertEqual(args, [b"/data/data/com.gwterm/files/usr/bin/pup", b"p text{}"])
        self.assertEqual(env, [b"HOME=/h", b"PATH=/p"])
        self.assertEqual(aux[0], (6, 4096))
        self.assertEqual(aux[-1], (0, 0))
        self.assertEqual([kind for kind, _ in aux], [6, 25, 31, 0])
        self.assertEqual(now_sp, sp, "the stack pointer must not move (it has to stay 16-byte aligned)")
        self.assertEqual(now_sp % 16, 0)

    def test_only_the_program_and_linker_given(self):
        program = gs.patch_go_entry(make_go_program())
        argc, args, env, _, _, _, _ = self.run_stub(program, [b"/system/bin/linker64", b"/bin/tool"], [])
        self.assertEqual((argc, args, env), (1, [b"/bin/tool"], []))

    def test_started_directly_nothing_changes(self):
        program = gs.patch_go_entry(make_go_program())
        argc, args, env, aux, now_sp, sp, before = self.run_stub(program, [b"/bin/pup", b"p text{}"], [b"HOME=/h"])
        self.assertEqual((argc, args, env), (2, [b"/bin/pup", b"p text{}"], [b"HOME=/h"]))
        self.assertEqual(now_sp, sp)

    def test_a_program_without_arguments_is_left_alone(self):
        program = gs.patch_go_entry(make_go_program())
        argc, args, _, _, _, _, _ = self.run_stub(program, [], [b"A=1"])
        self.assertEqual((argc, args), (0, []))

    def test_short_program_name_is_not_mistaken_for_the_linker(self):
        program = gs.patch_go_entry(make_go_program())
        argc, args, _, _, _, _, _ = self.run_stub(program, [b"pup", b"x"], [b"A=1"])
        self.assertEqual((argc, args), (2, [b"pup", b"x"]))


if __name__ == "__main__":
    unittest.main()
