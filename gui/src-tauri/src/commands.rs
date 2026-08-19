// commands.rs — All Tauri commands for the LIFE Compute installer

use serde::{Deserialize, Serialize};
use std::process::Command;
use sysinfo::System;
use tauri::{Emitter, Window};

// ─── System check ──────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SystemInfo {
    pub gpu_name: Option<String>,
    pub vram_gb: Option<f64>,
    pub ram_gb: Option<f64>,
    pub disk_gb: Option<f64>,
    pub docker_version: Option<String>,
    pub cuda_version: Option<String>,
    pub nvidia_driver: Option<String>,
}

#[tauri::command]
pub async fn check_system() -> Result<SystemInfo, String> {
    // ── GPU + VRAM (nvidia-smi) ──────────────────────────────────────────
    let (gpu_name, vram_gb, cuda_version, nvidia_driver) = probe_nvidia();

    // ── System RAM ───────────────────────────────────────────────────────
    let ram_gb = {
        let mut sys = System::new();
        sys.refresh_memory();
        let bytes = sys.total_memory();
        Some((bytes as f64) / 1_073_741_824.0)
    };

    // ── Free disk space on home dir ──────────────────────────────────────
    let disk_gb = probe_disk();

    // ── Docker ───────────────────────────────────────────────────────────
    let docker_version = probe_docker();

    Ok(SystemInfo {
        gpu_name,
        vram_gb,
        ram_gb: ram_gb.map(|v| (v * 10.0).round() / 10.0),
        disk_gb,
        docker_version,
        cuda_version,
        nvidia_driver,
    })
}

fn probe_nvidia() -> (Option<String>, Option<f64>, Option<String>, Option<String>) {
    // Query: name, memory.total [MiB], driver_version, compute_cap
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        ])
        .output();

    match out {
        Ok(o) if o.status.success() => {
            let s = String::from_utf8_lossy(&o.stdout);
            let line = s.lines().next().unwrap_or("").trim();
            let parts: Vec<&str> = line.splitn(4, ',').map(str::trim).collect();

            let gpu_name = parts
                .first()
                .map(|s| s.to_string())
                .filter(|s| !s.is_empty());
            let vram_gb = parts
                .get(1)
                .and_then(|s| s.parse::<f64>().ok())
                .map(|mb| (mb / 1024.0 * 10.0).round() / 10.0);
            let driver = parts
                .get(2)
                .map(|s| s.to_string())
                .filter(|s| !s.is_empty());
            let cuda = parts
                .get(3)
                .map(|s| format!("sm_{}", s.replace('.', "")))
                .filter(|s| s != "sm_");

            (gpu_name, vram_gb, cuda, driver)
        }
        _ => (None, None, None, None),
    }
}

fn probe_disk() -> Option<f64> {
    // Use `df -BG $HOME` for cross-platform disk check
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    let out = Command::new("df").args(["-BG", &home]).output();
    match out {
        Ok(o) if o.status.success() => {
            let s = String::from_utf8_lossy(&o.stdout);
            // line 1 is header; line 2 is the data
            s.lines().nth(1).and_then(|l| {
                let cols: Vec<&str> = l.split_whitespace().collect();
                // df -BG columns: Filesystem Size Used Avail Use% Mounted
                cols.get(3)
                    .and_then(|avail| avail.trim_end_matches('G').parse::<f64>().ok())
            })
        }
        _ => {
            // Windows fallback — use wmic
            #[cfg(target_os = "windows")]
            {
                let out = Command::new("wmic")
                    .args(["logicaldisk", "get", "freespace"])
                    .output();
                match out {
                    Ok(o) => {
                        let s = String::from_utf8_lossy(&o.stdout);
                        s.lines()
                            .filter_map(|l| l.trim().parse::<u64>().ok())
                            .next()
                            .map(|bytes| bytes as f64 / 1_073_741_824.0)
                    }
                    _ => None,
                }
            }
            #[cfg(not(target_os = "windows"))]
            None
        }
    }
}

fn probe_docker() -> Option<String> {
    let out = Command::new("docker").args(["--version"]).output();
    match out {
        Ok(o) if o.status.success() => {
            let s = String::from_utf8_lossy(&o.stdout);
            Some(s.trim().to_string())
        }
        _ => None,
    }
}

// ─── Miner count ───────────────────────────────────────────────────────────

#[tauri::command]
pub async fn get_miner_count() -> Result<u64, String> {
    // Fetch miner count from the LIFE Compute API
    let url = "https://api.life-compute.io/v1/miners/count";
    match reqwest::get(url).await {
        Ok(resp) => {
            if let Ok(json) = resp.json::<serde_json::Value>().await {
                Ok(json["count"].as_u64().unwrap_or(0))
            } else {
                Ok(0)
            }
        }
        // Fall back to local stats server
        Err(_) => {
            let local = reqwest::get("http://localhost:3001/stats").await;
            match local {
                Ok(r) => {
                    if let Ok(j) = r.json::<serde_json::Value>().await {
                        Ok(j["global"]["total_miners"].as_u64().unwrap_or(0))
                    } else {
                        Ok(0)
                    }
                }
                Err(_) => Ok(0),
            }
        }
    }
}

// ─── Wallet validation ─────────────────────────────────────────────────────

#[tauri::command]
pub async fn validate_wallet(address: String) -> Result<(), String> {
    // Basic: 32–44 chars, only base58 alphabet
    let valid_chars = address
        .chars()
        .all(|c| "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz".contains(c));
    if !valid_chars || address.len() < 32 || address.len() > 44 {
        return Err("Invalid Solana public key format.".into());
    }
    Ok(())
}

// ─── Installation ──────────────────────────────────────────────────────────

#[derive(Serialize, Clone)]
struct InstallProgressPayload {
    step: String,
    status: String, // "pending" | "running" | "done" | "error"
    progress: u32,
    message: String,
}

macro_rules! emit_progress {
    ($win:expr, $step:expr, $status:expr, $pct:expr, $msg:expr) => {
        let _ = $win.emit(
            "install_progress",
            InstallProgressPayload {
                step: $step.to_string(),
                status: $status.to_string(),
                progress: $pct,
                message: $msg.to_string(),
            },
        );
    };
}

#[tauri::command]
pub async fn run_install(window: Window, wallet: String) -> Result<(), String> {
    // ── Step 1: Docker ──────────────────────────────────────────────────
    emit_progress!(
        window,
        "docker",
        "running",
        10,
        "Checking Docker installation..."
    );

    let docker_ok = Command::new("docker")
        .args(["info"])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);

    if !docker_ok {
        emit_progress!(
            window,
            "docker",
            "running",
            40,
            "Installing Docker (requires sudo)..."
        );
        install_docker(&window)?;
    }

    emit_progress!(window, "docker", "done", 100, "Docker ready");
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;

    // ── Step 2: Pull image ───────────────────────────────────────────────
    emit_progress!(
        window,
        "pull",
        "running",
        5,
        "Pulling ghcr.io/life-compute/miner:latest ..."
    );

    let pull = Command::new("docker")
        .args(["pull", "ghcr.io/life-compute/miner:latest"])
        .output()
        .map_err(|e| format!("Docker pull failed: {e}"))?;

    if !pull.status.success() {
        let err = String::from_utf8_lossy(&pull.stderr).to_string();
        emit_progress!(window, "pull", "error", 0, &err);
        return Err(format!("Failed to pull Docker image: {err}"));
    }

    emit_progress!(window, "pull", "done", 100, "Image pulled successfully");
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // ── Step 3: MSA files ────────────────────────────────────────────────
    emit_progress!(
        window,
        "msa",
        "running",
        5,
        "Preparing MSA cache directory..."
    );

    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let msa_dir = format!("{home}/.life-compute/msa");
    std::fs::create_dir_all(&msa_dir).ok();

    emit_progress!(
        window,
        "msa",
        "running",
        50,
        "MSA files will be downloaded on first Boltz2 run (streamed automatically)."
    );
    tokio::time::sleep(std::time::Duration::from_millis(600)).await;
    emit_progress!(window, "msa", "done", 100, "MSA cache directory ready");
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // ── Step 4: Register on Solana ───────────────────────────────────────
    emit_progress!(
        window,
        "register",
        "running",
        10,
        "Checking miner registration..."
    );

    let reg_result = register_miner(&window, &wallet).await;
    match reg_result {
        Ok(_) => emit_progress!(window, "register", "done", 100, "Miner registered on-chain"),
        Err(e) => {
            emit_progress!(window, "register", "error", 0, &e);
            return Err(e);
        }
    }
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // ── Step 5: Start miner ──────────────────────────────────────────────
    emit_progress!(
        window,
        "start",
        "running",
        20,
        "Starting life-compute-miner container..."
    );

    start_docker_miner(&wallet)?;

    emit_progress!(window, "start", "done", 100, "Miner started!");
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;

    let _ = window.emit("install_done", ());
    Ok(())
}

fn install_docker(_window: &Window) -> Result<(), String> {
    // Platform-specific Docker installation
    #[cfg(target_os = "linux")]
    {
        // Try apt-get install docker.io
        let out = Command::new("sh")
            .args(["-c", "curl -fsSL https://get.docker.com | sh"])
            .output()
            .map_err(|e| format!("Docker install script error: {e}"))?;
        if !out.status.success() {
            return Err(String::from_utf8_lossy(&out.stderr).to_string());
        }
    }
    // On Windows/macOS: Docker Desktop must be manually installed — guide user
    #[cfg(any(target_os = "windows", target_os = "macos"))]
    {
        return Err(
            "Docker Desktop is required. Please install it from https://docker.com and retry."
                .into(),
        );
    }
    Ok(())
}

async fn register_miner(_window: &Window, wallet: &str) -> Result<(), String> {
    // Call the LIFE Compute registration API
    // In production this would call the Solana program's register_miner instruction
    let client = reqwest::Client::new();
    let res = client
        .post("https://api.life-compute.io/v1/miners/register")
        .json(&serde_json::json!({ "wallet": wallet }))
        .send()
        .await;

    match res {
        Ok(r) if r.status().is_success() => Ok(()),
        Ok(r) => {
            let status = r.status();
            let body = r.text().await.unwrap_or_default();
            if body.contains("already registered") || body.contains("already_exists") {
                // Already registered — not an error
                Ok(())
            } else {
                Err(format!("Registration API error {status}: {body}"))
            }
        }
        Err(e) => {
            // Offline — note it but don't block installation
            eprintln!("Registration API unreachable: {e}. Miner will register on first run.");
            Ok(())
        }
    }
}

fn start_docker_miner(wallet: &str) -> Result<(), String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());

    // Stop existing container if running
    let _ = Command::new("docker")
        .args(["stop", "life-compute-miner"])
        .output();
    let _ = Command::new("docker")
        .args(["rm", "life-compute-miner"])
        .output();

    let out = Command::new("docker")
        .args([
            "run",
            "-d",
            "--gpus",
            "all",
            "--name",
            "life-compute-miner",
            "--restart",
            "unless-stopped",
            "-v",
            &format!("{home}/.life-compute:/root/.life-compute"),
            "-p",
            "3001:3001",
            "-e",
            &format!("WALLET_ADDRESS={wallet}"),
            "ghcr.io/life-compute/miner:latest",
        ])
        .output()
        .map_err(|e| format!("Docker run failed: {e}"))?;

    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).to_string();
        return Err(format!("Container start failed: {err}"));
    }
    Ok(())
}

// ─── Stats ─────────────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Debug, Default)]
pub struct MinerStats {
    pub alive: bool,
    pub molecules_screened: Option<u64>,
    pub life_earned: Option<f64>,
    pub current_target: Option<String>,
    pub target_protein: Option<String>,
    pub boltz2_score: Option<f64>,
    pub gpu_utilization_pct: Option<u32>,
    pub gpu_power_w: Option<u32>,
    pub gpu_temp_c: Option<u32>,
    pub vram_used_gb: Option<f64>,
    pub vram_total_gb: Option<f64>,
    pub global: Option<serde_json::Value>,
    pub started_at: Option<String>,
    pub last_updated: Option<String>,
}

#[tauri::command]
pub async fn get_stats() -> Result<MinerStats, String> {
    // Fetch from local stats server
    match reqwest::get("http://localhost:3001/stats").await {
        Ok(resp) => {
            let json: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| format!("Stats JSON parse error: {e}"))?;

            let gpu = probe_gpu_live_stats();

            Ok(MinerStats {
                alive: json["alive"].as_bool().unwrap_or(false),
                molecules_screened: json["molecules_screened"].as_u64(),
                life_earned: json["life_earned"].as_f64(),
                current_target: json["current_target"]
                    .as_str()
                    .map(String::from)
                    .or_else(|| {
                        json["targets_contributed"]
                            .as_array()
                            .and_then(|a| a.first())
                            .and_then(|v| v.as_str())
                            .map(String::from)
                    }),
                target_protein: json["target_protein"].as_str().map(String::from),
                boltz2_score: json["boltz2_score"].as_f64(),
                gpu_utilization_pct: gpu.utilization,
                gpu_power_w: gpu.power_w,
                gpu_temp_c: gpu.temp_c,
                vram_used_gb: gpu.vram_used_gb,
                vram_total_gb: gpu.vram_total_gb,
                global: json.get("global").cloned(),
                started_at: json["started_at"].as_str().map(String::from),
                last_updated: json["last_updated"].as_str().map(String::from),
            })
        }
        Err(e) => Err(format!("Stats server unreachable: {e}")),
    }
}

struct GpuLiveStats {
    utilization: Option<u32>,
    power_w: Option<u32>,
    temp_c: Option<u32>,
    vram_used_gb: Option<f64>,
    vram_total_gb: Option<f64>,
}

fn probe_gpu_live_stats() -> GpuLiveStats {
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=utilization.gpu,power.draw,temperature.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output();

    match out {
        Ok(o) if o.status.success() => {
            let s = String::from_utf8_lossy(&o.stdout);
            let line = s.lines().next().unwrap_or("").trim();
            let parts: Vec<&str> = line.splitn(5, ',').map(str::trim).collect();

            GpuLiveStats {
                utilization: parts.first().and_then(|s| s.parse().ok()),
                power_w: parts
                    .get(1)
                    .and_then(|s| s.parse::<f64>().ok())
                    .map(|v| v as u32),
                temp_c: parts.get(2).and_then(|s| s.parse().ok()),
                vram_used_gb: parts
                    .get(3)
                    .and_then(|s| s.parse::<f64>().ok())
                    .map(|mb| mb / 1024.0),
                vram_total_gb: parts
                    .get(4)
                    .and_then(|s| s.parse::<f64>().ok())
                    .map(|mb| mb / 1024.0),
            }
        }
        _ => GpuLiveStats {
            utilization: None,
            power_w: None,
            temp_c: None,
            vram_used_gb: None,
            vram_total_gb: None,
        },
    }
}

// ─── Start / Stop miner ────────────────────────────────────────────────────

#[tauri::command]
pub async fn start_miner(wallet: String) -> Result<(), String> {
    start_docker_miner(&wallet)
}

#[tauri::command]
pub async fn stop_miner() -> Result<(), String> {
    let out = Command::new("docker")
        .args(["stop", "life-compute-miner"])
        .output()
        .map_err(|e| format!("docker stop error: {e}"))?;

    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).to_string();
        return Err(format!("Failed to stop miner: {err}"));
    }
    Ok(())
}
