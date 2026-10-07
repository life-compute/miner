// PM2 ecosystem — LIFE Compute Miner (devnet)
// Nova dashboard already on :3000; life-compute uses :3001
//
// Epoch advancement is handled automatically inside miner_daemon.py.
// Each miner checks whether the epoch has expired at the start of every
// scoring cycle and calls advance_epoch if so.  No separate crank process
// is needed — the first miner to detect an expired epoch advances it and
// all miners benefit.
module.exports = {
  apps: [
    {
      name: 'life-miner',
      script: 'miner_daemon.py',
      interpreter: 'python3',
      cwd: __dirname,
      env_file: '.env',
      autorestart: true,
      max_restarts: 20,
      exp_backoff_restart_delay: 5000,
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
    {
      name: 'life-dashboard',
      script: 'dashboard/server.cjs',
      interpreter: 'node',
      cwd: __dirname,
      env: { DASHBOARD_PORT: '3001' },
      autorestart: true,
      max_restarts: 10,
      watch: false,
    },
    {
      // LIFE-BRAIN — Network-wide self-learning system.
      // Fully independent of life-miner and life-validator.
      // CPU-only: CUDA_VISIBLE_DEVICES="" is set inside life_brain_runner.py
      // before any torch import. Boltz2 GPU resources are never touched.
      name: 'life-brain',
      script: 'life_brain_runner.py',
      interpreter: 'python3',
      cwd: __dirname,
      env_file: '.env',            // picks up SOLANA_RPC from the same .env
      env: {
        CUDA_VISIBLE_DEVICES: '',  // belt-and-suspenders at the PM2 env level
      },
      autorestart: true,
      max_restarts: 15,
      exp_backoff_restart_delay: 5000,
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
    {
      // pegRNA agreement observer — one-shot, re-run every 30 min.
      //
      // Reads output/life_peg_daemon_scores.jsonl (Stage 4) and re-evaluates
      // each candidate through the stdlib validator path (peg_model.json),
      // appending the miner-vs-validator delta to output/life_peg_observer.jsonl.
      //
      // autorestart:false is load-bearing. The script exits 0 in seconds;
      // without it PM2 would hot-loop it continuously instead of every 30 min.
      // Between runs the process sits in `stopped` — that is the healthy
      // steady state for this entry, not a fault.
      //
      // Never add --rebuild here: it truncates and re-observes everything.
      name: 'life-peg-observer',
      script: 'scripts/peg_observer.py',
      interpreter: 'python3',
      cwd: __dirname,
      autorestart: false,
      cron_restart: '*/30 * * * *',
      env: {
        CUDA_VISIBLE_DEVICES: '',  // pure-stdlib path; never touches the GPU
      },
      watch: false,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
    },
  ],
};
