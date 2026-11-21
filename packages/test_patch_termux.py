"""Tests for patch_termux.py. Run: python3 -m unittest discover -s packages -v"""
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

import patch_termux as pt
import publish_release as pr
import sync_r2

OLD = "com.termux"
NEW = "com.gwterm"
PREFIX_OLD = f"/data/data/{OLD}/files/usr"
PREFIX_NEW = f"/data/data/{NEW}/files/usr"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_tar(entries: list[tuple[str, bytes | str]], fmt=tarfile.GNU_FORMAT) -> bytes:
    """entries: (name, bytes) for a file, (name, "-> target") for a symlink, (name, "dir") for a directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=fmt) as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 1_700_000_000
            if content == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif isinstance(content, str) and content.startswith("-> "):
                info.type = tarfile.SYMTYPE
                info.linkname = content[3:]
                info.mode = 0o777
                tar.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(content))
    return lzma.compress(buffer.getvalue(), format=lzma.FORMAT_XZ)


def read_tar(data: bytes) -> dict[str, tuple[int, bytes | str]]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data)), mode="r:") as tar:
        for member in tar:
            if member.isfile():
                out[member.name] = (member.mode, tar.extractfile(member).read())
            elif member.issym():
                out[member.name] = (member.mode, "-> " + member.linkname)
            else:
                out[member.name] = (member.mode, "dir")
    return out


ELF_LIKE = b"\x7fELF" + b"\0" * 12 + f"RUNPATH={PREFIX_OLD}/lib".encode() + b"\0" + b"tail-bytes"
MAN_PAGE = f"The tool lives in {PREFIX_OLD}/bin\n".encode()


def sample_deb() -> bytes:
    data = make_tar([
        (f"./data/data/{OLD}/files/usr/bin/tool", ELF_LIKE),
        (f"./data/data/{OLD}/files/usr/bin/tool-link", f"-> {PREFIX_OLD}/bin/tool"),
        (f"./data/data/{OLD}/files/usr/share/man/man1/tool.1.gz", gzip.compress(MAN_PAGE)),
        (f"./data/data/{OLD}/files/usr/etc/tool.conf", f"prefix={PREFIX_OLD}\n".encode()),
        ("./data/data/", "dir"),
    ])
    md5sums = "".join(
        f"{md5(content)}  data/data/{OLD}/files/usr/{path}\n"
        for path, content in [("bin/tool", ELF_LIKE), ("etc/tool.conf", f"prefix={PREFIX_OLD}\n".encode())]
    ).encode()
    control = make_tar([
        ("./control", b"Package: tool\nVersion: 1.0\nArchitecture: aarch64\nDescription: tool for com.termux\n"),
        ("./md5sums", md5sums),
        ("./conffiles", f"{PREFIX_OLD}/etc/tool.conf\n".encode()),
        ("./postinst", f"#!{PREFIX_OLD}/bin/sh\necho done\nLD_PRELOAD='' {PREFIX_OLD}/bin/py3compile -p tool\nunset LD_PRELOAD\n".encode()),
    ])
    return pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", data)])


class PatchBytes(unittest.TestCase):
    def test_names_have_the_same_length(self):
        self.assertEqual(len(pt.OLD), len(pt.NEW))

    def test_replaces_every_occurrence_and_keeps_length(self):
        text = f"{PREFIX_OLD}/bin:{PREFIX_OLD}/lib".encode()
        patched = pt.patch_bytes(text)
        self.assertEqual(len(patched), len(text))
        self.assertNotIn(pt.OLD, patched)
        self.assertEqual(patched.replace(pt.NEW, pt.OLD), text)

    def test_gzip_is_opened_patched_and_deterministic(self):
        patched = pt.patch_file("tool.1.gz", gzip.compress(MAN_PAGE))
        self.assertEqual(gzip.decompress(patched), MAN_PAGE.replace(pt.OLD, pt.NEW))
        self.assertEqual(patched, pt.patch_file("tool.1.gz", gzip.compress(MAN_PAGE)))

    def test_gzip_without_the_name_is_left_alone(self):
        clean = gzip.compress(b"nothing to see")
        self.assertEqual(pt.patch_file("x.gz", clean), clean)

    def test_broken_gzip_does_not_crash(self):
        self.assertEqual(pt.patch_file("x.gz", b"\x1f\x8bnot really gzip"), b"\x1f\x8bnot really gzip")


class KeepPreload(unittest.TestCase):
    def test_empty_assignments_and_unsets_are_turned_into_twins_of_the_same_length(self):
        for before in [b"LD_PRELOAD='' py3compile -p x", b'LD_PRELOAD="" py3compile', b"LD_PRELOAD= py3compile",
                       b"FOO=1 LD_PRELOAD='' run", b"unset LD_PRELOAD", b"x; LD_PRELOAD=; y"]:
            after = pt.keep_preload(before)
            self.assertEqual(len(after), len(before), before)
            self.assertNotIn(b"LD_PRELOAD", after, before)
            self.assertIn(b"NO_PRELOAD", after, before)

    def test_a_preload_that_is_set_or_read_stays(self):
        for same in [b"LD_PRELOAD=/lib/x.so prog", b"echo $LD_PRELOAD", b'export LD_PRELOAD="$HOOK"',
                     b"NOT_LD_PRELOAD='' y", b"nothing here"]:
            self.assertEqual(pt.keep_preload(same), same)


class PatchDeb(unittest.TestCase):
    def setUp(self):
        members = dict(pt.read_ar(pt.patch_deb(sample_deb())))
        self.data = read_tar(members["data.tar.xz"])
        self.control = read_tar(members["control.tar.xz"])
        self.members = members

    def test_file_names_move_to_the_new_app(self):
        self.assertIn(f"./data/data/{NEW}/files/usr/bin/tool", self.data)
        self.assertFalse([n for n in self.data if OLD in n])

    def test_content_is_patched_and_keeps_its_mode(self):
        mode, content = self.data[f"./data/data/{NEW}/files/usr/bin/tool"]
        self.assertEqual(content, ELF_LIKE.replace(pt.OLD, pt.NEW))
        self.assertEqual(len(content), len(ELF_LIKE))
        self.assertEqual(mode, 0o755)

    def test_symlink_target_is_patched(self):
        _, target = self.data[f"./data/data/{NEW}/files/usr/bin/tool-link"]
        self.assertEqual(target, f"-> {PREFIX_NEW}/bin/tool")

    def test_man_page_inside_gzip_is_patched(self):
        _, packed = self.data[f"./data/data/{NEW}/files/usr/share/man/man1/tool.1.gz"]
        self.assertIn(PREFIX_NEW.encode(), gzip.decompress(packed))

    def test_control_scripts_are_patched(self):
        self.assertIn(PREFIX_NEW.encode(), self.control["./postinst"][1])
        self.assertIn(PREFIX_NEW.encode(), self.control["./conffiles"][1])

    def test_scripts_keep_the_exec_hook(self):
        script = self.control["./postinst"][1].decode()
        self.assertNotIn("LD_PRELOAD", script)
        self.assertIn(f"NO_PRELOAD='' {PREFIX_NEW}/bin/py3compile -p tool", script)
        self.assertIn("unset NO_PRELOAD", script)

    def test_md5sums_match_the_patched_files(self):
        listed = dict(line.split("  ", 1)[::-1] for line in self.control["./md5sums"][1].decode().splitlines())
        self.assertEqual(len(listed), 2)
        for path, digest in listed.items():
            _, content = self.data["./" + path]
            self.assertEqual(md5(content), digest, path)

    def test_verifier_accepts_patched_and_rejects_stock(self):
        self.assertEqual(pt.verify_deb("good", pt.patch_deb(sample_deb())), [])
        self.assertTrue(pt.verify_deb("stock", sample_deb()))

    def test_package_is_readable_by_dpkg(self):
        import shutil
        import subprocess
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            deb = Path(tmp, "tool.deb")
            deb.write_bytes(pt.patch_deb(sample_deb()))
            listing = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True).stdout
            self.assertIn(f"data/data/{NEW}/files/usr/bin/tool", listing)
            info = subprocess.run(["dpkg-deb", "-f", str(deb), "Package"], capture_output=True, text=True, check=True)
            self.assertEqual(info.stdout.strip(), "tool")


LONG_DIR = f"./data/data/{OLD}/files/usr/lib/ruby/gems/4.0.0/gems/" + "very-long-gem-name-" * 5
LONG_FILE = LONG_DIR + "/lib/some/deeply/nested/file.rb"
LONG_TARGET = f"{PREFIX_OLD}/lib/ruby/gems/4.0.0/gems/" + "target-" * 20 + "/file.rb"


def long_name_deb() -> bytes:
    data = make_tar([
        (LONG_FILE, b"puts 1\n"),
        (f"./data/data/{OLD}/files/usr/bin/link", f"-> {LONG_TARGET}"),
    ])
    control = make_tar([("./control", b"Package: longnames\nVersion: 1.0\nArchitecture: aarch64\nDescription: x\n")])
    return pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", data)])


def _pax_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(LONG_FILE.replace(OLD, NEW))
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


def tar_record_types(xz: bytes) -> set[str]:
    raw = lzma.decompress(xz)
    return {chr(raw[p + 156]) for p in range(0, len(raw) - 512, 512)
            if raw[p + 257:p + 262] == b"ustar"}


class LongNames(unittest.TestCase):
    """Names over 100 bytes must not turn into PAX extended headers: dpkg refuses them (seen with ruby)."""

    def setUp(self):
        self.deb = pt.patch_deb(long_name_deb())
        self.members = dict(pt.read_ar(self.deb))

    def test_no_pax_headers_are_written(self):
        types = tar_record_types(self.members["data.tar.xz"])
        self.assertFalse(types & {"x", "g"}, types)

    def test_long_names_and_targets_survive_and_are_patched(self):
        data = read_tar(self.members["data.tar.xz"])
        self.assertIn(LONG_FILE.replace(OLD, NEW), data)
        self.assertEqual(data[f"./data/data/{NEW}/files/usr/bin/link"][1], "-> " + LONG_TARGET.replace(OLD, NEW))

    def test_verifier_flags_pax_headers(self):
        pax_data = lzma.compress(_pax_tar(), format=lzma.FORMAT_XZ)
        control = dict(pt.read_ar(self.deb))["control.tar.xz"]
        deb = pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", pax_data)])
        self.assertEqual(pt.verify_deb("good", self.deb), [])
        self.assertTrue(any("PAX" in problem for problem in pt.verify_deb("pax", deb)))

    def test_dpkg_can_unpack_it(self):
        import shutil
        import subprocess
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb not installed")
        with tempfile.TemporaryDirectory() as tmp:
            deb = Path(tmp, "long.deb")
            deb.write_bytes(self.deb)
            target = Path(tmp, "root")
            subprocess.run(["dpkg-deb", "-x", str(deb), str(target)], capture_output=True, text=True, check=True)
            self.assertTrue(Path(target, LONG_FILE.replace(OLD, NEW)).is_file())


STATUS = f"""Package: tool
Status: install ok installed
Version: 1.0
Conffiles:
 {PREFIX_OLD}/etc/tool.conf {md5(f"prefix={PREFIX_OLD}\n".encode())}

Package: other
Status: install ok installed
Version: 2.0
"""


def sample_bootstrap(path: Path) -> None:
    conf = f"prefix={PREFIX_OLD}\n".encode()
    md5sums = (f"{md5(ELF_LIKE)}  data/data/{OLD}/files/usr/bin/tool\n"
               f"{md5(conf)}  data/data/{OLD}/files/usr/etc/tool.conf\n").encode()
    files = {
        "bin/tool": ELF_LIKE,
        "etc/tool.conf": conf,
        "etc/apt/sources.list": b"deb https://packages-cf.termux.dev/apt/termux-main/ stable main\n",
        "var/lib/dpkg/status": STATUS.encode(),
        "var/lib/dpkg/info/tool.md5sums": md5sums,
        "var/lib/dpkg/info/tool.list": f"{PREFIX_OLD}/bin/tool\n".encode(),
        "SYMLINKS.txt": f"{PREFIX_OLD}/bin/tool←./bin/tool-link\n".encode(),
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("bin/"), b"")
        for name, content in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.startswith("bin/") else 0o100644) << 16
            zf.writestr(info, content)


class PatchBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name, "stock.zip")
        self.dst = Path(self.tmp.name, "out", "bootstrap-aarch64.zip")
        sample_bootstrap(self.src)
        pt.patch_bootstrap(self.src, self.dst, "https://example.github.io/Shellterm/apt")
        with zipfile.ZipFile(self.dst) as zf:
            self.files = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
            self.modes = {i.filename: i.external_attr >> 16 for i in zf.infolist()}

    def test_files_and_symlink_list_are_patched(self):
        self.assertEqual(self.files["bin/tool"], ELF_LIKE.replace(pt.OLD, pt.NEW))
        self.assertIn(PREFIX_NEW, self.files["SYMLINKS.txt"].decode())
        self.assertIn(PREFIX_NEW, self.files["var/lib/dpkg/info/tool.list"].decode())

    def test_permissions_survive(self):
        self.assertEqual(self.modes["bin/tool"], 0o100755)

    def test_package_checksums_match_the_new_files(self):
        for line in self.files["var/lib/dpkg/info/tool.md5sums"].decode().splitlines():
            digest, path = line.split("  ")
            self.assertEqual(md5(self.files[path.removeprefix(pt.DATA_ROOT)]), digest, path)

    def test_config_file_hashes_in_the_status_file_match(self):
        status = self.files["var/lib/dpkg/status"].decode()
        conf = self.files["etc/tool.conf"]
        self.assertIn(f" {PREFIX_NEW}/etc/tool.conf {md5(conf)}", status)
        self.assertIn("Package: other", status)

    def test_apt_points_at_our_repository(self):
        sources = self.files["etc/apt/sources.list"].decode()
        self.assertIn("deb [trusted=yes] https://example.github.io/Shellterm/apt stable main", sources)
        self.assertNotIn("termux.dev", sources)

    def test_sha256_file_matches(self):
        recorded = Path(str(self.dst) + ".sha256").read_text().split()
        self.assertEqual(recorded[0], hashlib.sha256(self.dst.read_bytes()).hexdigest())
        self.assertEqual(recorded[1], "bootstrap-aarch64.zip")

    def test_verifier(self):
        self.assertEqual(pt.verify_bootstrap(self.dst), [])
        self.assertTrue(pt.verify_bootstrap(self.src))

    def test_installed_package_names(self):
        self.assertEqual(pt.bootstrap_package_names(self.src), ["tool", "other"])


INDEX = """Package: a
Version: 1:1.0
Filename: pool/main/a/a/a_1:1.0_aarch64.deb
SHA256: 00
Size: 10
Depends: b (>= 1), c | d

Package: b
Version: 2.0
Filename: pool/main/b/b/b_2.0_aarch64.deb
SHA256: 00
Size: 20
Pre-Depends: e

Package: d
Version: 3.0
Filename: pool/main/d/d/d_3.0_all.deb
SHA256: 00
Size: 30
Provides: virtual-d

Package: e
Version: 4.0
Filename: pool/main/e/e/e_4.0_aarch64.deb
SHA256: 00
Size: 40
Depends: virtual-d
"""


class Resolve(unittest.TestCase):
    def setUp(self):
        self.index = pt.parse_index(INDEX)

    def names(self, wanted):
        return [e.name for e in pt.resolve(self.index, wanted)]

    def test_follows_depends_and_first_available_alternative(self):
        self.assertEqual(self.names(["a"]), ["a", "b", "d", "e"])

    def test_virtual_packages_resolve_to_their_provider(self):
        self.assertEqual(self.names(["e"]), ["d", "e"])

    def test_unknown_package_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "No package named 'nope'"):
            self.names(["nope"])

    def test_missing_dependency_is_an_error(self):
        broken = pt.parse_index("Package: x\nVersion: 1\nFilename: p/x.deb\nSHA256: 0\nSize: 1\nDepends: ghost\n")
        with self.assertRaisesRegex(RuntimeError, "ghost"):
            pt.resolve(broken, ["x"])

    def test_file_names_drop_the_epoch_and_keep_all_arch(self):
        self.assertEqual(pt.deb_file_name(self.index["a"]), "a_1.0_aarch64.deb")
        self.assertEqual(pt.deb_file_name(self.index["d"]), "d_3.0_all.deb")

    def test_package_list_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "list.txt")
            path.write_text("# header\n\nnano\n  git  \n")
            self.assertEqual(pt.read_package_list(path), ["nano", "git"])

    def test_real_package_list_only_names_packages_once(self):
        names = pt.read_package_list(Path(__file__).with_name("bootstrap-packages.txt"))
        self.assertEqual(len(names), len(set(names)))


class FileBasedPatching(unittest.TestCase):
    """The streaming path used for the whole repository must give the same result as the in-memory one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def patch_to_file(self, blob: bytes) -> Path:
        src, dst, work = self.root / "in.deb", self.root / "out.deb", self.root / "work"
        work.mkdir(exist_ok=True)
        src.write_bytes(blob)
        pt.patch_deb_file(src, dst, work)
        return dst

    def test_same_files_as_the_in_memory_patcher(self):
        streamed = dict(pt.read_ar(self.patch_to_file(sample_deb()).read_bytes()))
        in_memory = dict(pt.read_ar(pt.patch_deb(sample_deb())))
        self.assertEqual(list(streamed), ["debian-binary", "control.tar.xz", "data.tar.xz"])
        for name in ("control.tar.xz", "data.tar.xz"):
            self.assertEqual(read_tar(streamed[name]), read_tar(in_memory[name]), name)

    def test_long_names_stay_free_of_pax_headers(self):
        deb = dict(pt.read_ar(self.patch_to_file(long_name_deb()).read_bytes()))
        self.assertFalse(tar_record_types(deb["data.tar.xz"]) & {"x", "g"})

    def test_verifier_passes_the_patched_package_and_flags_the_stock_one(self):
        work = self.root / "check"
        work.mkdir()
        self.assertEqual(pt.verify_deb_file("good", self.patch_to_file(sample_deb()), work), [])
        stock = self.root / "stock.deb"
        stock.write_bytes(sample_deb())
        shutil_work = self.root / "check2"
        shutil_work.mkdir()
        self.assertTrue(pt.verify_deb_file("stock", stock, shutil_work))

    def test_a_file_that_changes_size_is_an_error(self):
        from unittest import mock
        with mock.patch.object(pt, "patch_file", return_value=b"short"):
            with self.assertRaisesRegex(ValueError, "size changed"):
                self.patch_to_file(sample_deb())

    def test_old_name_hidden_in_a_jar_is_reported_as_a_warning(self):
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Config.class", PREFIX_OLD.encode() * 4)
        data = make_tar([(f"./data/data/{NEW}/files/usr/lib/app.jar", jar.getvalue())])
        control = make_tar([("./control", b"Package: jarry\nVersion: 1\nArchitecture: all\nDescription: x\n")])
        deb = self.root / "jarry.deb"
        deb.write_bytes(pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", data)]))
        work = self.root / "check3"
        work.mkdir()
        warnings: list[str] = []
        self.assertEqual(pt.verify_deb_file("jarry", deb, work, warnings), [])
        self.assertTrue(any("app.jar" in w for w in warnings), warnings)


class Shards(unittest.TestCase):
    def entries(self, sizes):
        return [pt.Entry(f"p{i}", "1", f"pool/p{i}.deb", "0", size, (), ()) for i, size in enumerate(sizes)]

    def test_shards_cover_every_package_exactly_once(self):
        plan = self.entries([900, 5, 70, 300, 1, 44, 600, 12, 8, 150])
        lots = [pt.split_into_shards(plan, f"{k}/4") for k in range(1, 5)]
        names = [e.name for lot in lots for e in lot]
        self.assertEqual(sorted(names), sorted(e.name for e in plan))
        self.assertEqual(len(names), len(set(names)))

    def test_a_package_stays_in_its_lot_whatever_else_is_in_the_plan(self):
        small = self.entries([5, 10, 20, 30, 40, 50, 60, 70])
        large = self.entries([5, 10, 20, 30, 40, 50, 60, 70] + [999] * 40)
        for k in range(1, 5):
            in_small = {e.name for e in pt.split_into_shards(small, f"{k}/4")}
            in_large = {e.name for e in pt.split_into_shards(large, f"{k}/4")}
            self.assertTrue(in_small <= in_large)

    def test_lots_are_roughly_equal_in_count(self):
        plan = self.entries([1] * 3000)
        counts = [len(pt.split_into_shards(plan, f"{k}/4")) for k in range(1, 5)]
        self.assertLess(max(counts) - min(counts), 150, counts)
        self.assertLess(max(counts), 900, counts)  # a release holds at most 1,000 files

    def test_bad_shard_is_refused(self):
        with self.assertRaises(SystemExit):
            pt.split_into_shards(self.entries([1]), "5/4")


class FileNames(unittest.TestCase):
    def test_characters_github_would_rename_are_replaced(self):
        entry = pt.Entry("libc++", "1:28c~rc+1", "pool/main/libc/libc++/libc++_28c_aarch64.deb", "0", 1, (), ())
        self.assertEqual(pt.deb_file_name(entry), "libc___28c_rc_1_aarch64.deb")


class FlatBootstrapSources(unittest.TestCase):
    def test_flat_repositories_are_written_with_a_slash_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "stock.zip"), Path(tmp, "out.zip")
            sample_bootstrap(src)
            pt.patch_bootstrap(src, dst, "", ("https://x.test/apt-1", "https://x.test/apt-2"))
            with zipfile.ZipFile(dst) as zf:
                sources = zf.read(pt.SOURCES_LIST).decode()
        self.assertIn("deb [trusted=yes] https://x.test/apt-1 /\n", sources)
        self.assertIn("deb [trusted=yes] https://x.test/apt-2 /\n", sources)
        self.assertNotIn("stable main", sources)


class RepositoryTable(unittest.TestCase):
    def test_every_lot_has_a_release_name(self):
        tags = [pt.release_tag(repo, lot) for repo, source in pt.SOURCE_REPOS.items()
                for lot in range(1, source.lots + 1)]
        self.assertEqual(len(tags), len(set(tags)))
        self.assertIn("apt-1", tags)
        self.assertIn("apt-x11-2", tags)
        self.assertNotIn("apt-main-1", tags)  # main keeps the plain names

    def test_workflow_matrix_matches_the_table(self):
        workflow = Path(__file__).parent.parent.joinpath(".github/workflows/packages.yml").read_text()
        in_workflow = sorted(re.findall(r"tag: (apt-[\w-]+)", workflow))
        in_table = sorted(pt.release_tag(repo, lot) for repo, source in pt.SOURCE_REPOS.items()
                          for lot in range(1, source.lots + 1))
        self.assertEqual(in_workflow, in_table)

    def test_several_versions_of_a_package_are_all_kept(self):
        text = ("Package: a\nVersion: 1\nFilename: p/a1.deb\nSHA256: 0\nSize: 1\n\n"
                "Package: a\nVersion: 2\nFilename: p/a2.deb\nSHA256: 0\nSize: 1\n")
        self.assertEqual([e.version for e in pt.parse_index_entries(text)], ["1", "2"])
        self.assertEqual(pt.parse_index(text)["a"].version, "2")


class RepositorySwitches(unittest.TestCase):
    def switch_deb(self) -> bytes:
        data = make_tar([
            (f"./data/data/{OLD}/files/usr/etc/apt/sources.list.d/x11.list",
             b"deb https://packages-cf.termux.dev/apt/termux-x11/ x11 main\n"),
            (f"./data/data/{OLD}/files/usr/bin/tool", ELF_LIKE),
        ])
        control = make_tar([("./control", b"Package: x11-repo\nVersion: 8.4-1\nArchitecture: all\nDescription: x\n"),
                            ("./md5sums", b"")])
        return pt.write_ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.xz", data)])

    def test_only_the_two_switch_packages_get_overrides(self):
        self.assertTrue(pt.source_overrides("x11-repo", "https://r.test"))
        self.assertTrue(pt.source_overrides("root-repo", "https://r.test"))
        self.assertEqual(pt.source_overrides("tur-repo", "https://r.test"), {})
        self.assertEqual(pt.source_overrides("git", "https://r.test"), {})

    def test_x11_list_names_every_lot(self):
        (content,) = pt.source_overrides("x11-repo", "https://r.test").values()
        lines = [l for l in content.decode().splitlines() if l.startswith("deb ")]
        self.assertEqual(lines, [f"deb [trusted=yes] https://r.test/apt-x11-{n} /" for n in range(1, 3)])

    def test_sources_list_points_at_our_releases_and_checksums_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "work").mkdir()
            src, dst = root / "in.deb", root / "out.deb"
            src.write_bytes(self.switch_deb())
            pt.patch_deb_file(src, dst, root / "work", pt.source_overrides("x11-repo", "https://r.test"))
            members = dict(pt.read_ar(dst.read_bytes()))
            data = read_tar(members["data.tar.xz"])
            listed = data[f"./data/data/{NEW}/files/usr/etc/apt/sources.list.d/x11.list"][1].decode()
            self.assertIn("deb [trusted=yes] https://r.test/apt-x11-1 /", listed)
            self.assertIn("deb [trusted=yes] https://r.test/apt-x11-2 /", listed)
            self.assertNotIn("termux.dev", listed)
            work = root / "check"
            work.mkdir()
            self.assertEqual(pt.verify_deb_file("x11-repo", dst, work), [])




class PackageUrls(unittest.TestCase):
    def test_epoch_colon_is_not_escaped(self):
        self.assertEqual(
            pt.package_url_path("pool/main/openssl_1:3.5.2_aarch64.deb"),
            "pool/main/openssl_1:3.5.2_aarch64.deb",
        )

    def test_other_characters_are_escaped(self):
        self.assertEqual(pt.package_url_path("pool/main/a b+c.deb"), "pool/main/a%20b%2Bc.deb")


class PublishRelease(unittest.TestCase):
    def fake_run(self, code, stderr=""):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, code, stdout="", stderr=stderr)

        return run, calls

    def test_an_existing_release_is_left_alone(self):
        run, calls = self.fake_run(0)
        pr.ensure_release("o/r", "apt-1", "t", "n", run=run)
        self.assertEqual(len(calls), 1)

    def test_a_rate_limit_is_not_taken_for_a_missing_release(self):
        run, calls = self.fake_run(1, "gh: HTTP 403: API rate limit exceeded for user ID 1")
        with self.assertRaises(subprocess.CalledProcessError):
            pr.ensure_release("o/r", "apt-1", "t", "n", run=run)
        self.assertEqual(len(calls), 1)

    def test_plan_sends_only_what_changed_and_the_index_last(self):
        existing = {"a.deb": "1", "b.deb": "2", "gone.deb": "3", "Packages": "old", "Release": "same"}
        local = {"a.deb": "1", "b.deb": "changed", "new.deb": "4", "Packages": "new", "Release": "same", "Packages.gz": "z"}
        delete, upload = pr.plan(existing, local)
        self.assertEqual(delete, ["gone.deb"])
        self.assertEqual(upload, ["b.deb", "new.deb", "Packages", "Packages.gz"])

    def test_an_unknown_digest_counts_as_different(self):
        self.assertEqual(pr.plan({"a.deb": ""}, {"a.deb": "1"}), ([], ["a.deb"]))

    def test_a_finished_release_has_nothing_to_do(self):
        files = {"a.deb": "1", "Packages": "p", "Release": "r"}
        self.assertEqual(pr.plan(dict(files), files), ([], []))

    def test_batches_keep_the_index_files_alone_and_last(self):
        names = [f"{i}.deb" for i in range(5)] + ["Packages", "Packages.xz", "Release"]
        self.assertEqual(
            list(pr.batches(names, size=2)),
            [["0.deb", "1.deb"], ["2.deb", "3.deb"], ["4.deb"], ["Packages", "Packages.xz", "Release"]],
        )
        self.assertEqual(list(pr.batches([])), [])

    def test_waiting_until_the_quota_resets(self):
        self.assertEqual(pr.seconds_to_wait(0, 1000, 900), 115)
        self.assertEqual(pr.seconds_to_wait(0, 1000, 2000), 15)
        self.assertEqual(pr.seconds_to_wait(4000, 1000, 900), pr.PAUSE)

    def test_a_failed_request_is_retried_after_waiting(self):
        calls, slept = [], []

        def action():
            calls.append(1)
            if len(calls) < 3:
                raise subprocess.CalledProcessError(1, "gh")
            return "ok"

        self.assertEqual(pr.retrying(action, lambda: (0, 1500), sleep=slept.append, now=lambda: 1000), "ok")
        self.assertEqual(slept, [515, 515])

    def test_it_gives_up_after_the_last_try(self):
        def action():
            raise subprocess.CalledProcessError(1, "gh")

        slept = []
        with self.assertRaises(subprocess.CalledProcessError):
            pr.retrying(action, lambda: (900, 0), sleep=slept.append, tries=3)
        self.assertEqual(len(slept), 2)

    LIMITED = subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: API rate limit exceeded for user ID 1")

    def test_a_rate_limit_is_waited_out_for_longer_than_the_tries(self):
        calls, slept = [], []

        def action():
            calls.append(1)
            if len(calls) < 30:
                raise self.LIMITED
            return "ok"

        # The API claims plenty of quota (the real run saw this); the wait still grows and the tries do not count.
        self.assertEqual(pr.retrying(action, lambda: (4000, 0), sleep=slept.append, now=lambda: 1000, tries=3, max_wait=10**6), "ok")
        self.assertEqual(slept[:4], [60, 120, 240, 300])
        self.assertEqual(len(slept), 29)

    def test_a_rate_limit_that_never_lifts_gives_up_after_max_wait(self):
        slept = []

        def action():
            raise self.LIMITED

        with self.assertRaises(subprocess.CalledProcessError):
            pr.retrying(action, lambda: (4000, 0), sleep=slept.append, now=lambda: 1000, max_wait=1000)
        self.assertGreaterEqual(sum(slept), 1000)
        self.assertLess(sum(slept), 1000 + pr.LONG_PAUSE)

    def test_a_used_up_quota_sleeps_until_the_reset_even_when_limited(self):
        slept, calls = [], []

        def action():
            calls.append(1)
            if len(calls) < 2:
                raise self.LIMITED
            return "ok"

        pr.retrying(action, lambda: (0, 3000), sleep=slept.append, now=lambda: 1000)
        self.assertEqual(slept, [2015])

    def test_only_rate_limit_messages_count_as_limited(self):
        self.assertTrue(pr.is_rate_limited(self.LIMITED))
        self.assertTrue(pr.is_rate_limited(subprocess.CalledProcessError(1, "gh", stderr="You have exceeded a secondary Rate Limit")))
        self.assertFalse(pr.is_rate_limited(subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")))
        self.assertFalse(pr.is_rate_limited(subprocess.CalledProcessError(1, "gh")))

    def test_local_files_are_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.deb").write_bytes(b"abc")
            (Path(tmp) / "sub").mkdir()
            self.assertEqual(
                pr.local_files(tmp),
                {"a.deb": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
            )


class SyncToR2(unittest.TestCase):
    def test_only_what_the_folder_no_longer_has_is_stale(self):
        self.assertEqual(sync_r2.stale(["a.deb", "old.deb", "Packages"], ["a.deb", "Packages", "new.deb"]), ["old.deb"])
        self.assertEqual(sync_r2.stale([], ["a.deb"]), [])

    def test_the_copy_is_skipped_without_a_bucket(self):
        with mock.patch.dict(os.environ, {"R2_BUCKET": "", "R2_ACCOUNT_ID": ""}), mock.patch.object(
            sync_r2, "sync"
        ) as sync:
            self.assertEqual(sync_r2.main(["sync_r2.py", "apt-1", "x"]), 0)
            sync.assert_not_called()

    def test_files_go_first_then_the_index_then_the_removals(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.deb", "Packages", "Packages.gz", "Release"):
                (Path(tmp) / name).write_bytes(b"x")
            calls = []

            def fake_aws(env, *args, capture=False):
                calls.append(args)
                if "Contents[].[Key,Size]" in args:
                    return json.dumps([["apt-1/a.deb", 5], ["apt-2/b.deb", 7]])
                return json.dumps(["apt-1/a.deb", "apt-1/gone.deb"]) if capture else None

            with mock.patch.object(sync_r2, "aws", fake_aws):
                sync_r2.sync("apt-1", tmp, "bucket", "https://e")
        kinds = [(c[0], c[1]) for c in calls]
        self.assertEqual(
            kinds,
            [("s3api", "list-objects-v2"), ("s3", "sync"), ("s3", "cp"), ("s3", "cp"), ("s3", "cp"),
             ("s3api", "list-objects-v2"), ("s3", "rm")],
        )
        self.assertTrue(calls[2][2].endswith("Packages") and calls[4][2].endswith("Release"))
        self.assertEqual(calls[6][1:3], ("rm", "s3://bucket/apt-1/gone.deb"))

    def test_the_bucket_never_goes_over_the_limit(self):
        sizes = {"apt-1/a.deb": 4_000, "apt-2/b.deb": 3_000, "apt-3/c.deb": 2_000, "bootstrap/z": 100}
        # apt-1 is replaced by its new size; everything else stays.
        self.assertEqual(sync_r2.fits(sizes, "apt-1", 4_400, 9_500), (True, 9_500))
        self.assertEqual(sync_r2.fits(sizes, "apt-1", 4_401, 9_500), (False, 9_501))
        self.assertEqual(sync_r2.fits({}, "apt-1", 100, 9_500), (True, 100))
        self.assertEqual(sync_r2.fits(sizes, "apt-1", 0, 9_500), (True, 5_100), "prefixes are matched with the slash")

    def test_sizes_are_read_from_pages_that_may_be_null_or_nested(self):
        self.assertEqual(sync_r2.parse_sizes(None), {})
        self.assertEqual(sync_r2.parse_sizes([None, [["a/x", 1]], ["a/y", "2"], None]), {"a/x": 1, "a/y": 2})

    def test_a_lot_that_does_not_fit_copies_nothing_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.deb").write_bytes(b"x" * 100)
            calls = []

            def fake_aws(env, *args, capture=False):
                calls.append(args)
                return json.dumps([["apt-2/b.deb", 950]])

            with mock.patch.object(sync_r2, "aws", fake_aws), mock.patch.object(sync_r2, "note") as note:
                self.assertFalse(sync_r2.sync("apt-1", tmp, "bucket", "https://e", max_bytes=1_000))
        self.assertEqual(len(calls), 1, "only the size listing")
        self.assertIn("apt-1", note.call_args[0][0])

    def test_an_empty_prefix_lists_nothing(self):
        with mock.patch.object(sync_r2, "aws", lambda *a, **k: "null"):
            self.assertEqual(sync_r2.remote_names({"endpoint": "e"}, "b", "apt-1"), [])


class IndexChangedDuringRun(unittest.TestCase):
    """Termux publishes while a 25 minute run is going: the index read at the start can name files that are gone."""

    def entry(self, version):
        return pt.Entry("ncdu", version, f"pool/main/n/ncdu/ncdu_{version}_aarch64.deb", "0" * 64, 10, (), ())

    def index(self, *versions):
        return "\n\n".join(
            f"Package: ncdu\nVersion: {v}\nFilename: pool/main/n/ncdu/ncdu_{v}_aarch64.deb\nSHA256: {'0' * 64}\nSize: 10"
            for v in versions
        ).encode()

    def run_with(self, results, fresh_index):
        """results: version -> report problems; fresh_index: bytes the re-read of the index gives (or an error)."""
        calls = []

        def once(entry, *args):
            calls.append(entry.version)
            return {"name": entry.name, "version": entry.version, "size": 10, "file": f"{entry.name}_{entry.version}.deb",
                    "problems": results[entry.version], "warnings": []}

        def fetch(url):
            if isinstance(fresh_index, Exception):
                raise fresh_index
            return fresh_index

        with mock.patch.object(pt, "process_package_once", once), mock.patch.object(pt, "fetch", fetch):
            report = pt.process_package(self.entry("1.23.0"), "b", {}, Path("o"), Path("s"), True, True, "https://i/Packages")
        return report, calls

    NOT_FOUND = ["ncdu: RuntimeError: Could not fetch https://x/ncdu_1.23.0_aarch64.deb: HTTP Error 404: Not Found"]

    def test_the_newer_version_is_used_when_the_old_file_is_gone(self):
        report, calls = self.run_with({"1.23.0": self.NOT_FOUND, "1.24.0": []}, self.index("1.24.0"))
        self.assertEqual(calls, ["1.23.0", "1.24.0"])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["version"], "1.24.0")
        self.assertIn("replaced 1.23.0 by 1.24.0", report["warnings"][-1])

    def test_a_package_termux_removed_is_skipped_not_failed(self):
        report, calls = self.run_with({"1.23.0": self.NOT_FOUND}, self.index())
        self.assertEqual(calls, ["1.23.0"])
        self.assertEqual(report["problems"], [])
        self.assertNotIn("file", report)
        self.assertIn("no longer in Termux's index", report["warnings"][0])

    def test_a_file_still_listed_but_missing_stays_a_problem(self):
        report, calls = self.run_with({"1.23.0": self.NOT_FOUND}, self.index("1.23.0"))
        self.assertEqual(report["problems"], self.NOT_FOUND)
        self.assertEqual(calls, ["1.23.0"])

    def test_other_problems_are_never_retried(self):
        report, calls = self.run_with({"1.23.0": ["ncdu: still has the old name"]}, self.index("1.24.0"))
        self.assertEqual(report["problems"], ["ncdu: still has the old name"])
        self.assertEqual(calls, ["1.23.0"])

    def test_an_unreadable_index_keeps_the_original_problem(self):
        report, calls = self.run_with({"1.23.0": self.NOT_FOUND}, RuntimeError("offline"))
        self.assertEqual(report["problems"], self.NOT_FOUND)
        self.assertEqual(calls, ["1.23.0"])


if __name__ == "__main__":
    unittest.main()
