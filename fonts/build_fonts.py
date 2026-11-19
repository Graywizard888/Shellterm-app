#!/usr/bin/env python3
"""Builds the GitHub Pages site that hosts the fonts of Shellterm's font picker.

Takes one file from each Nerd Fonts release zip (the "Nerd Font Mono" Regular: every character is one cell wide, and
the icons, powerline symbols, box drawing and braille a terminal needs are in it), checks it, and writes

    <out>/fonts/<file>.ttf|.otf  the font, as the app downloads it
    <out>/fonts/licenses/<id>.txt the license that travels with it
    <out>/fonts/fonts.json        the list the app reads: name, file, size, SHA-256, license name
    <out>/index.html, .nojekyll

The zips are big (up to 270 MB) and hold many files, so each is read over HTTP with range requests: only the file
list at its end and the one font wanted are fetched, a few MB per family. A family that fails the checks below (or has
no license file to publish) is skipped and reported, and the rest still go out; Hack must pass.

    python3 fonts/build_fonts.py --out site             # reads the zips from GitHub
    python3 fonts/build_fonts.py --out site --zips DIR  # uses <DIR>/<zip name> when present (offline runs, tests)
    python3 fonts/build_fonts.py --out site --only hack,fira-code
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile

VERSION = "v3.5.1"
BASE = f"https://github.com/ryanoasis/nerd-fonts/releases/download/{VERSION}"

# (id, zip name without .zip, name shown in the app, license name, file to take when the zip holds several Regular
# ones). Chosen for a terminal: monospaced coding fonts. Left out on purpose: proportional families (Arimo, Tinos,
# Ubuntu, Overpass, Recursive, OpenDyslexic), the symbols-only font, and the pixel fonts that lack box drawing.
FONTS = [
    ("hack", "Hack", "Hack", "MIT, Bitstream Vera", None),
    ("jetbrains-mono", "JetBrainsMono", "JetBrains Mono", "OFL 1.1", "JetBrainsMonoNerdFontMono-Regular.ttf"),
    ("fira-code", "FiraCode", "Fira Code", "OFL 1.1", None),
    ("cascadia-code", "CascadiaCode", "Cascadia Code", "OFL 1.1", None),
    ("cascadia-mono", "CascadiaMono", "Cascadia Mono", "OFL 1.1", None),
    ("meslo", "Meslo", "Meslo LG S", "Apache 2.0", "MesloLGSNerdFontMono-Regular.ttf"),
    ("0xproto", "0xProto", "0xProto", "OFL 1.1", None),
    ("adwaita-mono", "AdwaitaMono", "Adwaita Mono", "OFL 1.1", None),
    ("agave", "Agave", "Agave", "OFL 1.1", None),
    ("anonymous-pro", "AnonymousPro", "Anonymice Pro", "OFL 1.1", None),
    ("atkinson-mono", "AtkinsonHyperlegibleMono", "Atkinson Hyperlegible Mono", "OFL 1.1", None),
    ("bitstream-vera", "BitstreamVeraSansMono", "Bitstream Vera Sans Mono", "Bitstream Vera", None),
    ("blex-mono", "IBMPlexMono", "IBM Plex Mono", "OFL 1.1", None),
    ("code-new-roman", "CodeNewRoman", "Code New Roman", "OFL 1.1", None),
    ("comic-shanns", "ComicShannsMono", "Comic Shanns Mono", "MIT", None),
    ("commit-mono", "CommitMono", "Commit Mono", "OFL 1.1", None),
    ("cousine", "Cousine", "Cousine", "OFL 1.1", None),
    ("d2coding", "D2Coding", "D2Coding", "OFL 1.1", None),
    ("dejavu-sans-mono", "DejaVuSansMono", "DejaVu Sans Mono", "Bitstream Vera", None),
    ("departure-mono", "DepartureMono", "Departure Mono", "OFL 1.1", None),
    ("droid-sans-mono", "DroidSansMono", "Droid Sans Mono", "Apache 2.0", None),
    ("envy-code-r", "EnvyCodeR", "Envy Code R", "OFL 1.1", None),
    ("fantasque", "FantasqueSansMono", "Fantasque Sans Mono", "OFL 1.1", None),
    ("fira-mono", "FiraMono", "Fira Mono", "OFL 1.1", None),
    ("geist-mono", "GeistMono", "Geist Mono", "OFL 1.1", None),
    ("go-mono", "Go-Mono", "Go Mono", "BSD 3-Clause", None),
    ("google-sans-code", "GoogleSansCode", "Google Sans Code", "OFL 1.1", None),
    ("hasklig", "Hasklig", "Hasklig", "OFL 1.1", None),
    ("hermit", "Hermit", "Hermit", "OFL 1.1", None),
    ("ia-writer", "iA-Writer", "iA Writer Mono", "OFL 1.1", None),
    ("inconsolata", "Inconsolata", "Inconsolata", "OFL 1.1", None),
    ("intel-one-mono", "IntelOneMono", "Intel One Mono", "OFL 1.1", None),
    ("iosevka", "Iosevka", "Iosevka", "OFL 1.1", None),
    ("iosevka-term", "IosevkaTerm", "Iosevka Term", "OFL 1.1", None),
    ("lekton", "Lekton", "Lekton", "OFL 1.1", None),
    ("liberation-mono", "LiberationMono", "Liberation Mono", "OFL 1.1", None),
    ("lilex", "Lilex", "Lilex", "OFL 1.1", None),
    ("martian-mono", "MartianMono", "Martian Mono", "OFL 1.1", None),
    ("monaspace-neon", "Monaspace", "Monaspace Neon", "OFL 1.1", "MonaspiceNeNerdFontMono-Regular.otf"),
    ("monoid", "Monoid", "Monoid", "MIT or OFL 1.1", None),
    ("mononoki", "Mononoki", "Mononoki", "OFL 1.1", None),
    ("roboto-mono", "RobotoMono", "Roboto Mono", "Apache 2.0", None),
    ("share-tech-mono", "ShareTechMono", "Share Tech Mono", "OFL 1.1", None),
    ("source-code-pro", "SourceCodePro", "Source Code Pro", "OFL 1.1", None),
    ("space-mono", "SpaceMono", "Space Mono", "OFL 1.1", None),
    ("terminus", "Terminus", "Terminus", "OFL 1.1", None),
    ("ubuntu-mono", "UbuntuMono", "Ubuntu Mono", "Ubuntu Font Licence", None),
    ("victor-mono", "VictorMono", "Victor Mono", "OFL 1.1", None),
    ("zed-mono", "ZedMono", "Zed Mono", "OFL 1.1", None),
]
MUST_PASS = {"hack"}

FILE_PATTERN = re.compile(r"(^|/)[^/]*(NerdFontMono|NFM)-Regular\.(ttf|otf)$")
LICENSE_PATTERN = re.compile(r"(^|/)[^/]*(LICENSE|LICENCE|OFL|COPYING)[^/]*$", re.IGNORECASE)

# What a terminal font must hold: (label, code points, how many must be present).
REQUIRED = [
    ("ASCII", range(0x20, 0x7F), 95),
    ("box drawing", range(0x2500, 0x2580), 128),
    ("block elements", range(0x2580, 0x25A0), 32),
    ("braille", range(0x2800, 0x2900), 256),
    ("powerline", [0xE0A0, 0xE0A1, 0xE0A2, 0xE0B0, 0xE0B1, 0xE0B2, 0xE0B3, 0xE0B4, 0xE0B6, 0xE0B8, 0xE0BA, 0xE0BC], 12),
    ("seti icons", range(0xE5FA, 0xE6B8), 190),
    ("devicons", range(0xE700, 0xE7C6), 198),
    ("font awesome", range(0xF000, 0xF100), 256),
    ("octicons", range(0xF400, 0xF4A9), 169),
    ("material design", range(0xF0001, 0xF1AF1), 6896),
]


class HttpFile(io.RawIOBase):
    """A read-only, seekable view of a URL, fetched in 1 MiB blocks with range requests (zipfile reads this way)."""

    BLOCK = 1 << 20

    def __init__(self, url):
        self.url = url
        self.pos = 0
        self.cache = {}
        first = self._get(0, 0)  # a one-byte probe tells the size: "Content-Range: bytes 0-0/<size>"
        self.size = int(first[1])

    def _get(self, start, end, tries=5):
        for attempt in range(1, tries + 1):
            try:
                req = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    if r.status != 206:
                        raise OSError(f"{self.url}: the server does not support range requests ({r.status})")
                    total = r.headers["Content-Range"].rsplit("/", 1)[1]
                    return r.read(), total
            except OSError as e:
                if attempt == tries:
                    raise
                print(f"  {self.url}: {e}; retry {attempt}", file=sys.stderr)
                time.sleep(3 * attempt)

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=os.SEEK_SET):
        self.pos = {os.SEEK_SET: offset, os.SEEK_CUR: self.pos + offset, os.SEEK_END: self.size + offset}[whence]
        return self.pos

    def readinto(self, buf):
        want = min(len(buf), self.size - self.pos)
        done = 0
        while done < want:
            block, offset = divmod(self.pos, self.BLOCK)
            if block not in self.cache:
                if len(self.cache) >= 8:
                    self.cache.pop(next(iter(self.cache)))
                start = block * self.BLOCK
                self.cache[block] = self._get(start, min(start + self.BLOCK, self.size) - 1)[0]
            chunk = self.cache[block][offset:offset + want - done]
            buf[done:done + len(chunk)] = chunk
            done += len(chunk)
            self.pos += len(chunk)
        return done


def validate(path, name):
    """Raises ValueError unless the file is a TrueType font with every cell the same width and the glyphs above."""
    from fontTools.ttLib import TTFont  # pip install fonttools

    with open(path, "rb") as f:
        if f.read(4) not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            raise ValueError(f"{name}: not a TrueType or OpenType font")
    font = TTFont(path)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    for label, points, want in REQUIRED:
        have = sum(1 for c in points if c in cmap)
        if have < want:
            raise ValueError(f"{name}: {label} has {have} of {want} characters")
    widths = {hmtx[cmap[c]][0] for c in range(0x21, 0x7F)}
    widths |= {hmtx[cmap[c]][0] for c in range(0x2500, 0x2580)}
    widths |= {hmtx[cmap[c]][0] for c in (0xE0B0, 0xE0B2, 0xE700, 0xF000)}
    if len(widths) != 1:
        raise ValueError(f"{name}: not monospaced, widths {sorted(widths)}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_font(names, wanted, zip_name):
    if wanted:
        if wanted not in names:
            raise ValueError(f"{zip_name} has no {wanted}")
        return wanted
    # A .ttf beats an .otf; then the shortest name (the plain family, not a "NL" or other variant).
    found = sorted((n for n in names if FILE_PATTERN.search(n)), key=lambda n: (n.endswith(".otf"), len(n), n))
    if not found:
        raise ValueError(f"{zip_name} has no Nerd Font Mono Regular file")
    return found[0]


def build_one(font_id, zip_name, name, license_name, wanted, fonts_dir, licenses_dir, zips):
    local = os.path.join(zips, zip_name + ".zip") if zips else ""
    source = local if local and os.path.isfile(local) else HttpFile(f"{BASE}/{zip_name}.zip")
    with zipfile.ZipFile(source) as z:
        names = z.namelist()
        member = pick_font(names, wanted, zip_name)
        licenses = sorted(n for n in names if LICENSE_PATTERN.search(n))
        if not licenses:
            raise ValueError(f"{zip_name} has no license file, so it cannot be published")
        file = os.path.basename(member)
        ttf = os.path.join(fonts_dir, file)
        with z.open(member) as src, open(ttf, "wb") as dst:
            shutil.copyfileobj(src, dst)
        with z.open(licenses[0]) as src, open(os.path.join(licenses_dir, font_id + ".txt"), "wb") as dst:
            shutil.copyfileobj(src, dst)
    try:
        validate(ttf, name)
    except Exception:
        os.remove(ttf)
        os.remove(os.path.join(licenses_dir, font_id + ".txt"))
        raise
    return {
        "id": font_id,
        "name": name,
        "file": file,
        "bytes": os.path.getsize(ttf),
        "sha256": sha256(ttf),
        "license": license_name,
        "licenseFile": f"licenses/{font_id}.txt",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--zips", help="folder holding already downloaded zips")
    ap.add_argument("--only", help="comma separated ids to build (default: all)")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    fonts_dir = os.path.join(args.out, "fonts")
    licenses_dir = os.path.join(fonts_dir, "licenses")
    shutil.rmtree(args.out, ignore_errors=True)
    os.makedirs(licenses_dir)

    index, skipped = [], []
    for font_id, zip_name, name, license_name, wanted in FONTS:
        if only and font_id not in only:
            continue
        try:
            entry = build_one(font_id, zip_name, name, license_name, wanted, fonts_dir, licenses_dir, args.zips)
        except Exception as e:  # one family must not stop the others
            print(f"SKIPPED {name}: {e}")
            skipped.append((font_id, str(e)))
            continue
        index.append(entry)
        print(f"ok      {name}: {entry['file']}, {entry['bytes']} bytes")

    failed_required = [i for i, _ in skipped if i in MUST_PASS]
    if failed_required:
        raise SystemExit(f"required font(s) failed: {', '.join(failed_required)}")
    if not index:
        raise SystemExit("no font was built")

    with open(os.path.join(fonts_dir, "fonts.json"), "w") as f:
        json.dump({"source": f"Nerd Fonts {VERSION}", "fonts": index}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, ".nojekyll"), "w"):
        pass
    rows = "".join(f'<li><a href="fonts/{i["file"]}">{i["name"]}</a> ({i["bytes"] // 1024} KB, {i["license"]})</li>' for i in index)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(
            "<!doctype html><meta charset=utf-8><title>Shellterm fonts</title>"
            f"<h1>Shellterm fonts</h1><p>Nerd Fonts {VERSION}, Mono Regular. Licenses in fonts/licenses.</p><ul>{rows}</ul>\n"
        )
    total = sum(i["bytes"] for i in index)
    print(f"\nwrote {len(index)} fonts ({total / 1048576:.0f} MB) to {args.out}; skipped {len(skipped)}")
    for font_id, why in skipped:
        print(f"  skipped {font_id}: {why}")


if __name__ == "__main__":
    main()
