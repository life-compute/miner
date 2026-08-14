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
  ],
};
