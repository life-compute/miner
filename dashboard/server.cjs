/**
 * LIFE Compute Dashboard — static file server on :3001
 * Serves the pre-built Vite dist/ and proxies:
 *   /stats.json    — live daemon stats
 *   /adaptive.json — aggregated adaptive-stack data (pulse, art, scout)
 */
const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT    = parseInt(process.env.DASHBOARD_PORT || '3001');
const DIST    = path.join(__dirname, 'dist');
const ROOT    = path.join(__dirname, '..');
const STATS   = path.join(ROOT, 'stats.json');
const OUT     = path.join(ROOT, 'output');

const MIME = {
  '.html': 'text/html',
  '.js':   'application/javascript',
  '.css':  'text/css',
  '.json': 'application/json',
  '.ico':  'image/x-icon',
  '.svg':  'image/svg+xml',
};

/* ── Read last N lines of a JSONL file (cheap tail) ─────────────── */
function tailJsonl(filePath, n = 50) {
  try {
    const lines = fs.readFileSync(filePath, 'utf8')
      .split('\n').filter(Boolean);
    return lines.slice(-n).map(l => { try { return JSON.parse(l); } catch { return null; } })
      .filter(Boolean);
  } catch { return []; }
}

function readJson(filePath) {
  try { return JSON.parse(fs.readFileSync(filePath, 'utf8')); }
  catch { return null; }
}

/* ── /adaptive.json aggregator ───────────────────────────────────── */
function buildAdaptive() {
  const pulseRows   = tailJsonl(path.join(OUT, 'life_pulse_data.jsonl'),   200);
  const boltzRows   = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 100);
  const scoutRows   = tailJsonl(path.join(OUT, 'life_scout_log.jsonl'),     50);
  const artReport   = readJson(path.join(OUT, 'life_art_report.json')) || {};

  /* Pulse stats */
  const familyCounts = {};
  let topProxy = 0;
  for (const r of pulseRows) {
    const fam = r.family || 'general';
    familyCounts[fam] = (familyCounts[fam] || 0) + 1;
    if ((r.proxy_score || 0) > topProxy) topProxy = r.proxy_score;
  }

  /* Boltz stats */
  const boltzByTarget = {};
  let bestBoltz = null;
  for (const r of boltzRows) {
    const tid = r.target_id || 'unknown';
    boltzByTarget[tid] = (boltzByTarget[tid] || 0) + 1;
    if (r.boltz_score !== null && r.boltz_score !== undefined) {
      if (bestBoltz === null || r.boltz_score > bestBoltz) bestBoltz = r.boltz_score;
    }
  }

  /* Scout last entry */
  const lastScout = scoutRows.length ? scoutRows[scoutRows.length - 1] : null;

  /* Retrain progress */
  const RETRAIN_EVERY = 50;
  const boltzCount    = boltzRows.length;
  const retrainPct    = Math.min(100, Math.round((boltzCount % RETRAIN_EVERY) / RETRAIN_EVERY * 100));
  const nextRetrain   = RETRAIN_EVERY - (boltzCount % RETRAIN_EVERY);

  return {
    pulse: {
      total_evaluated: pulseRows.length,
      top_proxy_score: Math.round(topProxy * 1000) / 1000,
      family_counts:   familyCounts,
      recent:          pulseRows.slice(-8).reverse().map(r => ({
        family:      r.family,
        scaffold:    r.scaffold_name,
        smiles:      (r.smiles || '').slice(0, 40),
        proxy_score: Math.round((r.proxy_score || 0) * 1000) / 1000,
      })),
    },
    art: {
      ready:              artReport.ready  || false,
      n_rows:             artReport.n_rows || 0,
      r2:                 artReport.r2     !== undefined ? artReport.r2 : null,
      reason:             artReport.reason || 'awaiting scores',
      boltz_accumulated:  boltzCount,
      retrain_progress:   retrainPct,
      next_retrain_in:    nextRetrain,
      n_features:         artReport.n_features || 525,
    },
    scout: {
      last_family:       lastScout?.family      || '—',
      last_phase:        lastScout?.phase        || '—',
      n_diverse:         lastScout?.n_diverse    || 0,
      n_passed_filter:   lastScout?.n_passed_filter || 0,
      best_score:        lastScout?.best_score   ?? null,
      target_id:         lastScout?.target_id    || '—',
      ts:                lastScout?.ts           || null,
    },
    scoring_history: boltzRows.slice(-20).reverse().map(r => ({
      smiles:      (r.smiles || '').slice(0, 36),
      boltz_score: typeof r.boltz_score === 'number'
        ? Math.round(r.boltz_score * 10000) / 10000 : null,
      target_id:   r.target_id || '?',
      ts:          r.ts ? new Date(r.ts * 1000).toISOString() : null,
    })),
    ts: Date.now(),
  };
}

http.createServer((req, res) => {
  const url = req.url.split('?')[0];

  if (url === '/stats.json') {
    try {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(fs.readFileSync(STATS));
    } catch { res.writeHead(500); res.end('{}'); }
    return;
  }

  if (url === '/adaptive.json') {
    try {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify(buildAdaptive()));
    } catch (e) {
      res.writeHead(500); res.end(JSON.stringify({ error: String(e) }));
    }
    return;
  }

  // Static files from dist/
  let filePath = path.join(DIST, url === '/' ? 'index.html' : url);
  if (!fs.existsSync(filePath)) filePath = path.join(DIST, 'index.html');

  const ext  = path.extname(filePath);
  const mime = MIME[ext] || 'application/octet-stream';
  try {
    res.writeHead(200, { 'Content-Type': mime });
    res.end(fs.readFileSync(filePath));
  } catch { res.writeHead(404); res.end('Not found'); }

}).listen(PORT, () => {
  console.log(`LIFE Compute dashboard → http://localhost:${PORT}`);
  console.log(`Stats live feed       → http://localhost:${PORT}/stats.json`);
  console.log(`Adaptive feed         → http://localhost:${PORT}/adaptive.json`);
});
