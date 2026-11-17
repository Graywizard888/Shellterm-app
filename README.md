<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/hero-dark.svg">
    <img src=".github/readme/hero-light.svg" width="840" alt="Shellterm: a real terminal for Android, with packages. App icon with a prompt, followed by the wordmark.">
  </picture>
</div>

<p align="center">
  <b>A Termux-like terminal for Android with its own emulator core, real <code>pkg</code> / <code>apt</code> packages and a liquid-glass UI.</b>
  <br>
  <i>No ads. No trackers. Just a terminal.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Android_10+-brightgreen.svg" />
  <img src="https://img.shields.io/badge/ABI-arm64--v8a-blue.svg" />
  <img src="https://img.shields.io/github/v/release/Graywizard888/Shellterm-app.svg?logo=github&label=Release&cacheSeconds=3600" />
  <img src="https://img.shields.io/github/downloads/Graywizard888/Shellterm-app/total?logo=github&cacheSeconds=3600" />
</p>

<div align="center">
  <a href="https://github.com/Graywizard888/Shellterm-app/releases">
    <img src="https://img.shields.io/badge/Download-Latest_Release-blue?style=for-the-badge&logo=github" alt="Download the latest release">
  </a>
  <a href="https://github.com/Graywizard888/Shellterm">
    <img src="https://img.shields.io/badge/Source-Shellterm-black?style=for-the-badge&logo=github" alt="Source code">
  </a>
</div>

This repository carries what users download: the signed APKs and the package repository. The app's source code is in
[Graywizard888/Shellterm](https://github.com/Graywizard888/Shellterm).

---

## Features

Shellterm keeps the Termux way of working and builds its own terminal engine and interface around it.

<details open>
<summary><b>Terminal</b></summary>

| Feature | Description |
|---|---|
| **Own emulator core** | Written in Rust: parser, screen buffer and scrollback, exposed to the app through JNI |
| **Native PTY** | C/C++ process spawning with `forkpty`, so programs behave as they do in any terminal |
| **Truecolor and 256 colors** | `xterm-256color` with 24-bit color |
| **Glyph-atlas rendering** | The grid is drawn inside a Compose layer, which is what lets the glass chrome blur it |
| **Exit closes the app** | Typing `exit` leaves the app without a dialog; a shell that fails to start still shows Restart |

</details>

<details open>
<summary><b>Touch & Keyboard</b></summary>

| Feature | Description |
|---|---|
| **Scroll and fling** | Drag the scrollback with momentum; in full-screen programs the drag becomes the mouse wheel or arrow keys |
| **Select and copy** | Long-press to select text, then Copy from the actions pill |
| **Pinch to zoom** | Change the font size with two fingers |
| **Mouse reporting** | Taps and drags reach programs that ask for the mouse (SGR and classic encodings) |
| **Extra keys** | Two rows of icon keys with latching Ctrl and Alt; the gear at the top-left shows or hides the toolbar |
| **Hardware keyboards** | Modifier combinations and cursor keys are encoded like xterm |

</details>

<details open>
<summary><b>Packages</b></summary>

| Feature | Description |
|---|---|
| **Real `pkg` / `apt`** | A Termux base system with dpkg and apt, set up on first launch |
| **Termux's own bootstrap** | Downloaded from Termux's release page and patched on the phone while it unpacks |
| **Mirror groups** | On first launch the app times the repository mirrors, picks the fastest group and gives apt its mirrors, fastest first |
| **Own apt repository** | Termux's packages renamed for `com.gwterm`, published from this repository |
| **Exec hook** | Programs in the app's data folder are started through the system loader, which Android 10+ requires |
| **First-run upgrade** | `pkg update && pkg upgrade` runs visibly in the terminal before the style setup is offered |

</details>

<details>
<summary><b>Look & Feel</b></summary>

| Feature | Description |
|---|---|
| **Liquid-glass chrome** | Toolbar, extra keys, sheets and dialogs blur what is behind them (Android 12+, tinted glass on Android 10 and 11) |
| **Material 3 Expressive** | Spring-based motion on every component, tuned fast, default or slow per component |
| **Theme and font size** | Light, dark or system theme, adjustable font size, optional haptics |
| **T-Header style setup** | Offered once after the first run: banner, prompt, icon font and colors |

</details>

<details>
<summary><b>Crash Reports</b></summary>

| Feature | Description |
|---|---|
| **Three kinds of crash** | Kotlin exceptions, native crashes (our C/C++, the Rust core, any library) and unexpected exits such as the system closing the app |
| **Report sheet** | Device facts, stack or backtrace and the last 250 logcat lines, with Copy, Share and Relaunch |
| **Kept on the device** | Plain text files in the app's private folder; the newest five stay |
| **Readable release traces** | Each release ships its R8 `mapping.txt`, so stack traces can be retraced |

</details>

---

## Install

1. Download `shellterm-release.apk` from the [latest release](https://github.com/Graywizard888/Shellterm-app/releases).
2. Open it and allow installs from your browser or file manager when Android asks.
3. Start Shellterm and wait for the base system to download and unpack. It needs a connection, about 33 MB for the download.
4. Let the first-run package upgrade finish, then accept or skip the style setup.

Updates install over the previous version as long as they are signed with the same key.

**Requirements:** Android 10 or newer, a 64-bit ARM (arm64-v8a) phone or tablet.

---

## Packages

Termux's prebuilt packages are renamed byte for byte (`com.termux` to `com.gwterm`, same length, nothing is compiled)
and published from here:

| What | Where |
|---|---|
| **apt repository** | `https://graywizard888.github.io/Shellterm-app/apt` (GitHub Pages) |
| **Second copy** | `https://raw.githubusercontent.com/Graywizard888/Shellterm-app/apt-mirror` (the `apt-mirror` branch, rebuilt by every run), for networks that cannot reach `github.io` |
| **Mirror list** | `https://graywizard888.github.io/Shellterm-app/mirrors.txt`, source in [`packages/mirrors.txt`](packages/mirrors.txt) |
| **Bootstrap fallback** | The `bootstrap` release: a patched copy of Termux's bootstrap zip with its SHA-256, used when Termux's own download fails |

Everything is built by the **Packages** workflow (Actions tab, run manually, a few minutes):

```bash
python3 packages/patch_termux.py --help
python3 -m unittest discover -s packages   # the patcher's tests
```

One-time setup for a fork: Settings > Pages > Source: GitHub Actions.

---

## Build the app

The source is in [Shellterm](https://github.com/Graywizard888/Shellterm). It needs JDK 21, the Android SDK (platform 37,
build-tools 37.0.0, NDK 30.0.16248370), CMake and Rust with the `aarch64-linux-android` target.

```bash
./gradlew :app:assembleRelease   # the only app variant
./gradlew test && (cd native/terminal-core && cargo test --release)
```

`assembleRelease` runs lint over every module with warnings as errors, then R8 with narrow keep rules. There is no debug
variant. Release builds are signed with your own key, given through the `SHELLTERM_KEYSTORE_*` variables described in the
source repository's README.

---

## Acknowledgments

Shellterm stands on [Termux](https://github.com/termux): the bootstrap, the packages and `pkg` come from the
[termux-packages](https://github.com/termux/termux-packages) project and keep their own licenses. The terminal style
setup uses [T-Header](https://github.com/remo7777/T-Header) by remo7777.

---

## Community

[Report a bug](https://github.com/Graywizard888/Shellterm/issues) · [Source code](https://github.com/Graywizard888/Shellterm) · [Releases](https://github.com/Graywizard888/Shellterm-app/releases)
