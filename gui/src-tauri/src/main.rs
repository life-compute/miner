// main.rs — Tauri 2.0 entry point
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    life_compute_installer_lib::run()
}
