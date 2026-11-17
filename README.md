# Shellterm-app

Releases and package hosting for **Shellterm**, a Termux-like terminal for Android. The app's source code is in the
[Shellterm](https://github.com/Graywizard888/Shellterm) repository; this repository carries what users download.

## About the app

- **Terminal for Android 10 and newer**, arm64-v8a only. App ID `com.gwterm`.
- **Own emulator core**: Rust (parser, screen buffer, scrollback), C/C++ for the PTY and JNI, drawn with Jetpack Compose.
- **Liquid-glass Material 3 UI**: glass toolbar, extra keys, sheets and dialogs, with spring animations.
- **Real packages**: a base system and `pkg` / `apt` like Termux, installed on first launch from Termux's own bootstrap.
- **Touch**: tap, scroll and fling, long-press to select text, pinch to zoom, mouse support for programs that ask for it.
- **First run**: updates and upgrades the packages, picks the fastest mirror group, and offers the T-Header style setup.
- **Typing `exit`** closes the app.
- **Crash reports**: if the app crashes, a sheet shows the report with Copy, Share and Relaunch.

## Releases

The signed APK of each version is attached to a [release](https://github.com/Graywizard888/Shellterm-app/releases) here
(`shellterm-release.apk`, with its R8 `mapping.txt` for reading release stack traces). Install it over the previous one;
updates only work when the signing key stays the same.

The `bootstrap` release holds the patched base system (below). App versions get their own tags.

## Packages

Termux's prebuilt packages are renamed byte for byte (`com.termux` to `com.gwterm`, same length, nothing is compiled)
and published from here:

- **apt repository**: `https://graywizard888.github.io/Shellterm-app/apt` (GitHub Pages), plus `mirrors.txt` next to it,
  the mirror groups the app times on first launch (`packages/mirrors.txt`).
- **`bootstrap` release**: a patched copy of Termux's bootstrap zip with its SHA-256. The app normally downloads
  Termux's own bootstrap and patches it on the device; this one is the fallback.

Everything is built by the **Packages** workflow (Actions, run manually, a few minutes):
`python3 packages/patch_termux.py --help`, tests with `python3 -m unittest discover -s packages`.

One-time setup: Settings > Pages > Source: GitHub Actions.
