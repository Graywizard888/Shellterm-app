# Shellterm-app

Hosts the package side of [Shellterm](https://github.com/Graywizard888/Shellterm) (the Android app; app ID `com.gwterm`).

Termux's prebuilt packages are renamed byte for byte (`com.termux` to `com.gwterm`, same length, nothing is compiled)
and published from here:

- **apt repository**: `https://graywizard888.github.io/Shellterm-app/apt` (GitHub Pages), plus `mirrors.txt` next to it,
  the mirror groups the app times on first launch (`packages/mirrors.txt`).
- **`bootstrap` release**: a patched copy of Termux's bootstrap zip with its SHA-256. The app normally downloads
  Termux's own bootstrap and patches it on the device; this one is the fallback.

Everything is built by the **Packages** workflow (Actions, run manually, a few minutes):
`python3 packages/patch_termux.py --help`, tests with `python3 -m unittest discover -s packages`.

One-time setup: Settings > Pages > Source: GitHub Actions.
