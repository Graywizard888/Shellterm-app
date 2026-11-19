#!/usr/bin/env bash
# Turns a folder of .deb files into a "flat" apt repository in place: Packages, Packages.gz, Packages.xz and Release
# next to the packages. Used for the release assets (apt-1 .. apt-4), where every file sits in one folder.
# apt reads it with a source line whose suite is a single slash:  deb [trusted=yes] <url of the folder> /
# Usage: make-flat-repo.sh <folder with .deb files>
set -euo pipefail
dir=${1:?usage: make-flat-repo.sh <folder with .deb files>}

(
  cd "$dir"
  # "Filename: ./x.deb" becomes "Filename: x.deb": release asset URLs have no folders to walk through.
  dpkg-scanpackages --multiversion . /dev/null 2>/dev/null | sed 's|^Filename: \./|Filename: |' > Packages
  gzip -9kf Packages
  xz -9kf Packages
  # Release lists only the index files (apt-ftparchive would hash every .deb too).
  {
    echo "Origin: Shellterm"
    echo "Label: Shellterm"
    echo "Suite: stable"
    echo "Architectures: aarch64 all"
    echo "Date: $(LC_ALL=C date -Ru)"
    echo "SHA256:"
    for f in Packages Packages.gz Packages.xz; do
      printf ' %s %16d %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$(stat -c %s "$f")" "$f"
    done
  } > Release
)
echo "Flat repository ready: $(find "$dir" -maxdepth 1 -name '*.deb' | wc -l) packages"
