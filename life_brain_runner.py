"""
life_brain_runner.py — PM2 entry point for the life-brain process.

Responsibilities
----------------
1. Enforce CPU-only (CUDA_VISIBLE_DEVICES="") before any torch import.
2. pip install guard: ensure torch and sklearn are available.
3. Start life_brain_ingest background thread (polls Solana every 5 min).
4. Main loop: check for new confirmed rows every 60s; retrain every 20 new rows.
5. Never import miner_daemon.py, validator_daemon.py, or their sub-modules.

PM2 process name: life-brain
"""
import os
import sys
import logging
import time
import subprocess

# ── CPU-only enforcement: must be set BEFORE any torch import ─────────────────
os.environ["CUDA_VISIBLE_DEVICES"] = ""

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [life-brain]  %(levelname)-8s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("life-brain")


# ── pip install guard ─────────────────────────────────────────────────────────

def _ensure_deps() -> bool:
    """
    Ensure torch, scikit-learn, and numpy are importable.
    Installs the CPU-only torch wheel if missing (no CUDA download).
    Returns True on success, False if installation fails.
    """
    required = [("torch", "torch"), ("sklearn", "scikit-learn"), ("numpy", "numpy")]
    for import_name, pip_name in required:
        try:
            __import__(import_name)
            log.info(f"[DEPS] {pip_name} ✔")
        except ImportError:
            log.warning(f"[DEPS] {pip_name} not found — installing…")
            install_args = [sys.executable, "-m", "pip", "install", "--quiet"]
            if pip_name == "torch":
                # CPU-only torch wheel to avoid downloading CUDA builds
                install_args += [
                    "torch", "--index-url",
                    "https://download.pytorch.org/whl/cpu",
                ]
            else:
                install_args.append(pip_name)
            try:
                result = subprocess.run(install_args, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    log.error(f"[DEPS] pip install {pip_name} failed:\n{result.stderr[-500:]}")
                    return False
                log.info(f"[DEPS] {pip_name} installed ✔")
            except subprocess.TimeoutExpired:
                log.error(f"[DEPS] pip install {pip_name} timed out after 300s")
                return False
            except Exception as e:
                log.error(f"[DEPS] pip install {pip_name} raised: {e}")
                return False
    return True


# ── Git push for snapshot (Part C hook) ──────────────────────────────────────

def _push_snapshot(snapshot_path: str, work_dir: str) -> None:
    """
    Commit and push output/life_brain_snapshot.json to origin.
    Logs a clear ERROR on any failure — never silently swallowed.
    """
    try:
        rel = os.path.relpath(snapshot_path, work_dir)
        # Stage only the snapshot file
        result = subprocess.run(
            ["git", "add", rel],
            cwd=work_dir, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.error(f"[SNAPSHOT-PUSH] git add failed (rc={result.returncode}):\n{result.stderr.strip()[-400:]}")
            return

        # Check if there is actually anything to commit
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=work_dir, capture_output=True, timeout=10,
        )
        if diff_result.returncode == 0:
            log.info("[SNAPSHOT-PUSH] Snapshot unchanged since last commit — skipping push")
            return

        import datetime
        ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        result = subprocess.run(
            ["git", "commit", "-m", f"chore: LIFE-BRAIN snapshot {ts_str}"],
            cwd=work_dir, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.error(f"[SNAPSHOT-PUSH] git commit failed (rc={result.returncode}):\n{result.stderr.strip()[-400:]}")
            return

        result = subprocess.run(
            ["git", "push"],
            cwd=work_dir, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.error(
                f"[SNAPSHOT-PUSH] git push FAILED (rc={result.returncode}) — "
                f"snapshot is on disk but not pushed to GitHub.\n"
                f"stderr: {result.stderr.strip()[-600:]}\n"
                f"stdout: {result.stdout.strip()[-200:]}\n"
                f"Fix: ensure git credentials are configured on this machine "
                f"(e.g. SSH key or HTTPS token in ~/.netrc)."
            )
            return

        log.info(f"[SNAPSHOT-PUSH] Snapshot pushed to origin ✔ ({rel})")

    except subprocess.TimeoutExpired as e:
        log.error(f"[SNAPSHOT-PUSH] git operation timed out: {e}")
    except Exception as e:
        log.error(f"[SNAPSHOT-PUSH] Unexpected error during git push: {e}", exc_info=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("  LIFE-BRAIN — NETWORK-WIDE SELF-LEARNING SYSTEM")
    log.info("  CPU-ONLY (CUDA_VISIBLE_DEVICES=\"\")")
    log.info("=" * 60)

    # Verify GPU is invisible
    try:
        import torch
        if torch.cuda.is_available():
            log.warning(
                "[LIFE-BRAIN] WARNING: torch reports CUDA available despite "
                "CUDA_VISIBLE_DEVICES=\"\" — GPU access may not be fully blocked. "
                "Proceeding with CPU tensors only."
            )
        else:
            log.info("[LIFE-BRAIN] GPU check: CUDA not visible ✔ (CPU-only confirmed)")
    except Exception:
        pass

    # ── Load config from environment ──────────────────────────────────────────
    solana_rpc = os.environ.get("SOLANA_RPC", "https://api.devnet.solana.com")
    work_dir   = os.path.dirname(os.path.abspath(__file__))
    snapshot_path = os.path.join(work_dir, "output", "life_brain_snapshot.json")

    log.info(f"[LIFE-BRAIN] Solana RPC: {solana_rpc}")
    log.info(f"[LIFE-BRAIN] Work dir:   {work_dir}")

    # ── Start ingestion background thread ─────────────────────────────────────
    from adaptive.life_brain_ingest import start_background_ingest
    start_background_ingest(solana_rpc)

    # ── Load persisted model + report from prior run ──────────────────────────
    from adaptive import life_brain as brain
    brain.load_persisted_report()
    brain.load_persisted_model()

    log.info("[LIFE-BRAIN] Init complete — entering main polling loop")
    log.info(f"[LIFE-BRAIN] Retrain trigger: every {brain.RETRAIN_EVERY} new confirmed rows")
    log.info(f"[LIFE-BRAIN] Minimum rows per branch: {brain.MIN_ROWS_PER_BRANCH}")

    poll_interval = 60   # seconds between retrain checks
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300   # log heartbeat every 5 min

    while True:
        try:
            from adaptive.life_brain_ingest import consume_new_row_count
            new_rows = consume_new_row_count()

            rows = brain.load_dataset()
            total = len(rows)

            # Heartbeat log
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                rep = brain.get_report()
                branches = rep.get("branches", {})
                log.info(
                    f"[LIFE-BRAIN] Heartbeat — total_rows={total}, "
                    f"protein={'trusted' if branches.get('protein',{}).get('trusted') else 'learning'}, "
                    f"mrna={'trusted' if branches.get('mrna',{}).get('trusted') else 'learning'}, "
                    f"crispr={'trusted' if branches.get('crispr',{}).get('trusted') else 'learning'}"
                )
                last_heartbeat = time.time()

            if brain.should_retrain(rows) or new_rows >= brain.RETRAIN_EVERY:
                log.info(f"[LIFE-BRAIN] Retrain triggered: total_rows={total}, new_since_last={new_rows}")
                try:
                    report = brain.retrain(rows)
                    log.info(
                        f"[LIFE-BRAIN] Retrain complete in {report.get('train_elapsed_s', '?')}s — "
                        f"protein={'trusted' if report.get('branches',{}).get('protein',{}).get('trusted') else 'not trusted'}, "
                        f"mrna={'trusted' if report.get('branches',{}).get('mrna',{}).get('trusted') else 'not trusted'}, "
                        f"crispr={'trusted' if report.get('branches',{}).get('crispr',{}).get('trusted') else 'not trusted'}"
                    )
                    # Part C: push snapshot to GitHub
                    if os.path.exists(snapshot_path):
                        _push_snapshot(snapshot_path, work_dir)
                except Exception as e:
                    log.error(f"[LIFE-BRAIN] retrain failed: {e}", exc_info=True)
            else:
                log.debug(
                    f"[LIFE-BRAIN] rows={total}, "
                    f"rows_at_last_train={brain._row_count_at_last_train}, "
                    f"need {brain.RETRAIN_EVERY} more to retrain"
                )

        except Exception as e:
            log.error(f"[LIFE-BRAIN] Main loop error: {e}", exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    if not _ensure_deps():
        log.error("[LIFE-BRAIN] Dependency installation failed — exiting.")
        sys.exit(1)
    main()
