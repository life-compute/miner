# LIFE Compute Miner — GUI Installer

A cross-platform desktop installer for the LIFE Compute miner, built with **Tauri 2.0 + React + Tailwind**.

> Your GPU could help cure cancer. Earn $LIFE tokens.

## Screenshots

Five-screen biopunk installer (`#020805` background, `#00ff41` green, `#ff69b4` pink):

| Screen | Description |
|---|---|
| 1 · Welcome | DNA helix logo, animated tagline, live network stats |
| 2 · System Check | Auto-detect GPU/VRAM via nvidia-smi, RAM, disk, Docker |
| 3 · Wallet Setup | Solana address validation, Phantom link, miner slot / fee status |
| 4 · Install Progress | Real-time Docker pull, MSA setup, Solana registration |
| 5 · Dashboard | Live $LIFE earned, molecules screened, GPU stats, Start/Stop |

## Download

Pre-built installers are attached to each [GitHub Release](https://github.com/life-compute/miner/releases):

| Platform | File |
|---|---|
| 🐧 Linux | `.AppImage` or `.deb` |
| 🪟 Windows | `.msi` (recommended) or NSIS `.exe` |
| 🍎 macOS | `.dmg` (universal — Intel + Apple Silicon) |

## Requirements

| | Minimum |
|---|---|
| GPU | NVIDIA ≥ 8 GB VRAM |
| RAM | 16 GB |
| Disk | 20 GB free |
| OS | Ubuntu 20.04+ / Windows 10+ / macOS 10.15+ |
| CUDA | 11.8+ |
| Docker | 24.0+ with NVIDIA Container Toolkit |
| Wallet | Solana wallet ([Phantom](https://phantom.app)) |

## Build locally

```bash
# Prerequisites: Rust 1.70+, Node 20+, platform dev libs (see below)
cd gui
npm install
npm run tauri build
```

### Linux dev deps (Ubuntu/Debian)
```bash
sudo apt-get install -y \
  libwebkit2gtk-4.1-dev libgtk-3-dev \
  libayatana-appindicator3-dev librsvg2-dev \
  patchelf libssl-dev pkg-config
```

### Windows
Install [Docker Desktop](https://docker.com), then build with `npm run tauri build`.

### macOS
Install Xcode command-line tools, then build with `npm run tauri build -- --target universal-apple-darwin`.

## Architecture

```
gui/
├── src/                     # React frontend
│   ├── App.jsx              # Screen router
│   ├── screens/
│   │   ├── Welcome.jsx      # Screen 1: logo + tagline
│   │   ├── SystemCheck.jsx  # Screen 2: nvidia-smi + sysinfo
│   │   ├── WalletSetup.jsx  # Screen 3: Solana key validation
│   │   ├── InstallProgress.jsx  # Screen 4: Docker pull + Solana register
│   │   └── Dashboard.jsx    # Screen 5: stats from localhost:3001
│   ├── components/
│   │   ├── MatrixBg.jsx     # Falling DNA/binary matrix canvas
│   │   └── GlowPanel.jsx    # Reusable biopunk panel
│   └── index.css            # Biopunk theme tokens
├── src-tauri/
│   ├── src/
│   │   ├── main.rs          # Entry point
│   │   ├── lib.rs           # Tauri app setup
│   │   └── commands.rs      # check_system, run_install, get_stats...
│   ├── tauri.conf.json      # Window size, bundle targets
│   └── Cargo.toml
└── public/
    └── dna-helix.svg        # Generated biopunk DNA helix logo
```

## CI/CD

`.github/workflows/gui-build.yml` builds on all three platforms in parallel:

- **Linux**: `ubuntu-22.04` → `.AppImage` + `.deb`
- **Windows**: `windows-latest` → `.msi` + NSIS `.exe`
- **macOS**: `macos-latest` → universal `.dmg` (arm64 + x86_64)

A GitHub Release is created automatically with all artifacts when pushed to `main`.

### Secrets required for CI

| Secret | Purpose |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | Tauri updater signing (optional — skip for now) |
| `APPLE_CERTIFICATE` | macOS code signing (base64-encoded .p12) |
| `APPLE_CERTIFICATE_PASSWORD` | Certificate password |
| `APPLE_ID` | Apple developer account email |
| `APPLE_PASSWORD` | App-specific password |
| `APPLE_TEAM_ID` | Apple team ID |
| `APPLE_SIGNING_IDENTITY` | Signing identity string |

The Linux and Windows builds work without any secrets configured. macOS notarization is optional.
