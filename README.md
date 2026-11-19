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
</div>

This repository carries what users download: the signed APKs and the package repository.

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
| **Termux helpers** | The app answers Termux's `am` socket, so `am`, `termux-open`, `termux-open-url`, `termux-setup-storage` and `termux-reload-settings` work as in Termux |
| **`termux-api`** | `termux-toast`, `termux-clipboard-get/set`, `termux-vibrate`, `termux-battery-status`, `termux-notification`, `termux-notification-remove` and `termux-share` are answered by the app itself, no extra APK |

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

1. Download `shellterm version.apk` from the [latest release](https://github.com/Graywizard888/Shellterm-app/releases).
2. Open it and allow installs from your browser or file manager when Android asks.
3. Start Shellterm and wait for the base system to download and unpack. It needs a connection, about 33 MB for the download.
4. Let the first-run package upgrade finish, then accept or skip the style setup.

Updates install over the previous version as long as they are signed with the same key.

**Requirements:** Android 10 or newer, a 64-bit ARM (arm64-v8a) phone or tablet.

---

## Packages

Termux's prebuilt packages are renamed byte for byte (`com.termux` to `com.gwterm`, same length, nothing is compiled).
They are published from here as release assets, split into lots because a release holds at most 1,000 files:

| Repository | Releases | Packages | How to use it |
|---|---|---|---|
| **main** (what `pkg` uses by default) | `apt-1` to `apt-4` | about 3,000, 9.5 GB | on by default |
| **x11** (graphical programs) | `apt-x11-1`, `apt-x11-2` | about 1,200, 5.5 GB | `pkg install x11-repo` |
| **root** (for rooted phones) | `apt-root-1` | about 100, 0.2 GB | `pkg install root-repo` |

Each release is a flat apt repository: `deb [trusted=yes] https://github.com/Graywizard888/Shellterm-app/releases/download/apt-1 /`
(and so on). The `x11-repo` and `root-repo` packages are rewritten so that they point at these releases
instead of Termux's servers, which would serve packages that do not run in this app. TUR (the community repository) is
not hosted.

| What | Where |
|---|---|
| **Bootstrap fallback** | The `bootstrap` release: a patched copy of Termux's bootstrap zip with its SHA-256, apt pointed at the four main releases; used when Termux's own download fails |
| **Older apps** | Versions up to 1.0.2 read the earlier repository on GitHub Pages (`https://graywizard888.github.io/Shellterm-app/apt`). It is no longer rebuilt and stays as it was |

Everything is built by the **Packages** workflow (Actions tab). Started by hand, or by itself every Monday at 03:23 UTC
to pick up Termux's updates, it patches, checks and publishes; on a
pull request it only checks (each package is downloaded, patched, verified and deleted again, nothing is stored). Each
package is checked for the old name in paths, content and checksums, for PAX tar headers that dpkg refuses, and that
`dpkg-deb` can read and unpack it. One job runs per lot (11 of them, in parallel).

**Rate limit.** Publishing sends about 4,000 files, one API request each, and the workflow's own token may make only
about 1,000 requests an hour for the whole repository ("API rate limit exceeded for installation"). So publishing uses
a personal access token when the repository has the secret `PACKAGES_TOKEN`: on GitHub open *Settings > Developer
settings > Personal access tokens > Fine-grained tokens*, create one for this repository only with *Contents: Read and
write* (a classic token needs `public_repo`), then add it under this repository's *Settings > Secrets and variables >
Actions* as `PACKAGES_TOKEN`. It gets 5,000 requests an hour of its own. Without the secret the workflow falls back
to its own token. In both cases `packages/publish_release.py` sends only files that changed (compared by SHA-256), so a
re-run continues where the last one stopped and a weekly run sends little, and it waits for the limit to reset when it
is reached.

```bash
python3 packages/patch_termux.py --help
python3 -m unittest discover -s packages   # the patcher's tests
# try a few packages on your own machine (downloads them, patches, checks, deletes):
python3 packages/patch_termux.py repo --all --repo-name main --only jq,wget --check --discard --out /tmp/out
```

---

## Acknowledgments

Shellterm stands on [Termux](https://github.com/termux): the bootstrap, the packages and `pkg` come from the
[termux-packages](https://github.com/termux/termux-packages) project and keep their own licenses. The terminal style
setup uses [T-Header](https://github.com/remo7777/T-Header) by remo7777.

---

## Community

[Report a bug](https://github.com/Graywizard888/Shellterm/issues) · [Source code](https://github.com/Graywizard888/Shellterm) · [Releases](https://github.com/Graywizard888/Shellterm-app/releases)
