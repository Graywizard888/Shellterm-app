#!/usr/bin/env bash
# Turns built .deb files into an apt repository (dists/stable/main + pool) that apt and generate-bootstraps can read.
# Usage: make-repo.sh <dir with .deb files> <repo dir>
set -euo pipefail
debs=${1:?usage: make-repo.sh <debs dir> <repo dir>}
repo=${2:?usage: make-repo.sh <debs dir> <repo dir>}

rm -rf "$repo"
mkdir -p "$repo/pool/main" "$repo/dists/stable/main/binary-aarch64" "$repo/dists/stable/main/binary-all"
cp "$debs"/*.deb "$repo/pool/main/"

(
  cd "$repo"
  # Termux packages are "aarch64" or "all"; one index holds both, the "all" index stays empty.
  dpkg-scanpackages --multiversion pool /dev/null > dists/stable/main/binary-aarch64/Packages
  : > dists/stable/main/binary-all/Packages
  for idx in dists/stable/main/binary-aarch64/Packages dists/stable/main/binary-all/Packages; do
    gzip -9kf "$idx"
    xz -9kf "$idx"
  done
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=Shellterm \
    -o APT::FTPArchive::Release::Label=Shellterm \
    -o APT::FTPArchive::Release::Suite=stable \
    -o APT::FTPArchive::Release::Codename=stable \
    -o APT::FTPArchive::Release::Architectures="aarch64 all" \
    -o APT::FTPArchive::Release::Components=main \
    release dists/stable > dists/stable/Release
)
echo "Repository ready: $(ls "$repo/pool/main" | wc -l) packages"
