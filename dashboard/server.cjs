/**
 * LIFE Compute Dashboard — static file server on :3001
 *
 * Endpoints
 * ─────────
 *  GET /stats          Public stats (anyone) — miner status, molecules, $LIFE, targets, scoring history
 *  GET /private/stats  Private diagnostics (localhost only) — PULSE, ART, SCOUT, generated molecules
 *  GET /stats.json     Legacy alias for /stats (kept for backward compat)
 *  GET /adaptive.json  Legacy alias for private stats (kept for backward compat, localhost-gated)
 *  GET /*              Static files from dist/
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

/* ── Localhost guard ─────────────────────────────────────────────── */
function isLocalhost(req) {
  const addr = req.socket.remoteAddress || '';
  return (
    addr === '127.0.0.1' ||
    addr === '::1'        ||
    addr === '::ffff:127.0.0.1'
  );
}

/* ── Base58 encoding (Solana pubkey display, no external deps) ───── */
const BASE58_CHARS = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function toBase58(bytes) {
  let num = BigInt('0x' + Buffer.from(bytes).toString('hex') || '0');
  if (num === 0n) return '1';
  let result = '';
  while (num > 0n) {
    result = BASE58_CHARS[Number(num % 58n)] + result;
    num /= 58n;
  }
  for (const b of bytes) {
    if (b !== 0) break;
    result = '1' + result;
  }
  return result;
}

/* ── Miner pubkey (derived from miner-keypair.json, 64-byte [priv|pub]) */
let _minerPubkey = null;
function getMinerPubkey() {
  if (_minerPubkey) return _minerPubkey;
  try {
    const kp = JSON.parse(fs.readFileSync(path.join(ROOT, 'miner-keypair.json'), 'utf8'));
    const pubBytes = Buffer.from(kp.slice(32));
    _minerPubkey = toBase58(pubBytes);
  } catch { _minerPubkey = '—'; }
  return _minerPubkey;
}

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

/* ── /stats — PUBLIC data ────────────────────────────────────────── */
function buildPublicStats() {
  const s         = readJson(STATS) || {};
  const boltzRows = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 200);

  // Scoring history: best boltz score per 5-min epoch bucket (TARGET_REFRESH = 300s)
  const EPOCH_WINDOW = 300;
  const epochBuckets = {};
  for (const r of boltzRows) {
    if (typeof r.boltz_score !== 'number') continue;
    const ts    = r.ts || 0;
    const epoch = Math.floor(ts / EPOCH_WINDOW);
    if (!epochBuckets[epoch] || r.boltz_score > epochBuckets[epoch].best_score) {
      epochBuckets[epoch] = {
        epoch,
        best_score: Math.round(r.boltz_score * 10000) / 10000,
        target_id:  r.target_id || '?',
        ts_iso:     ts ? new Date(ts * 1000).toISOString() : null,
      };
    }
  }
  const scoringHistory = Object.values(epochBuckets)
    .sort((a, b) => a.epoch - b.epoch)
    .slice(-20);

  return {
    alive:               s.alive               ?? false,
    current_target:      s.current_target      ?? '—',
    miner_id:            getMinerPubkey(),
    molecules_screened:  s.molecules_screened  ?? 0,
    life_earned:         s.life_earned         ?? 0,
    targets_contributed: s.targets_contributed ?? [],
    scoring_history:     scoringHistory,
    global:              s.global              ?? {},
    started_at:          s.started_at          ?? null,
    last_updated:        s.last_updated        ?? null,
    ts:                  Date.now(),
  };
}

/* ── /private/stats — LOCAL diagnostics (localhost only) ─────────── */
function buildPrivateStats() {
  const pulseState  = readJson(path.join(OUT, 'life_pulse_state.json')) || {};
  const pulseRows   = tailJsonl(path.join(OUT, 'life_pulse_data.jsonl'),   200);
  const scoutRows   = tailJsonl(path.join(OUT, 'life_scout_log.jsonl'),     50);
  const artReport   = readJson(path.join(OUT, 'life_art_report.json'))   || {};
  const generated   = tailJsonl(path.join(OUT, 'life_generated.jsonl'),     20);
  const boltzRows   = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 100);
  const lastScout   = scoutRows.length ? scoutRows[scoutRows.length - 1] : null;

  const familyCounts = {};
  let topProxy = 0;
  for (const r of pulseRows) {
    const fam = r.family || 'general';
    familyCounts[fam] = (familyCounts[fam] || 0) + 1;
    if ((r.proxy_score || 0) > topProxy) topProxy = r.proxy_score;
  }

  const RETRAIN_EVERY   = 50;
  const boltzCount      = boltzRows.length;
  const retrainPct      = Math.min(100, Math.round((boltzCount % RETRAIN_EVERY) / RETRAIN_EVERY * 100));
  const nextRetrain     = RETRAIN_EVERY - (boltzCount % RETRAIN_EVERY);

  return {
    pulse: {
      sobol_index:       pulseState.next_index     ?? 0,
      total_evaluated:   pulseRows.length,
      top_proxy_score:   Math.round(topProxy * 1000) / 1000,
      family_counts:     familyCounts,
      recent:            pulseRows.slice(-8).reverse().map(r => ({
        family:      r.family,
        scaffold:    r.scaffold_name,
        smiles:      (r.smiles || '').slice(0, 40),
        proxy_score: Math.round((r.proxy_score || 0) * 1000) / 1000,
      })),
    },
    art: {
      ready:             artReport.ready       ?? false,
      n_rows:            artReport.n_rows      ?? 0,
      r2:                artReport.r2          ?? null,
      reason:            artReport.reason      ?? 'awaiting scores',
      n_features:        artReport.n_features  ?? 525,
      feature_importances: artReport.feature_importances ?? {},
      boltz_accumulated: boltzCount,
      retrain_progress:  retrainPct,
      next_retrain_in:   nextRetrain,
    },
    scout: {
      last_family:       lastScout?.family         || '—',
      last_phase:        lastScout?.phase          || '—',
      n_diverse:         lastScout?.n_diverse      || 0,
      n_passed_filter:   lastScout?.n_passed_filter || 0,
      best_score:        lastScout?.best_score     ?? null,
      target_id:         lastScout?.target_id      || '—',
      ts:                lastScout?.ts             || null,
    },
    generated:           generated.map(r => ({
      smiles:       (r.smiles || '').slice(0, 60),
      method:       r.method      || '—',
      art_score:    r.art_score   ?? null,
      boltz_score:  r.boltz_score ?? null,
      target:       r.target      || '?',
      ts:           r.ts          ? new Date(r.ts * 1000).toISOString() : null,
    })),
    ts: Date.now(),
  };
}

/* ── Legacy /adaptive.json aggregator ───────────────────────────── */
function buildAdaptive() {
  const priv  = buildPrivateStats();
  const boltz = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 100);
  return {
    pulse:   priv.pulse,
    art:     priv.art,
    scout:   priv.scout,
    scoring_history: boltz.slice(-20).reverse().map(r => ({
      smiles:      (r.smiles || '').slice(0, 36),
      boltz_score: typeof r.boltz_score === 'number'
        ? Math.round(r.boltz_score * 10000) / 10000 : null,
      target_id:   r.target_id || '?',
      ts:          r.ts ? new Date(r.ts * 1000).toISOString() : null,
    })),
    ts: Date.now(),
  };
}

/* ── HTTP server ─────────────────────────────────────────────────── */
http.createServer((req, res) => {
  const url    = req.url.split('?')[0];
  const local  = isLocalhost(req);

  /* /stats — public */
  if (url === '/stats') {
    try {
      res.writeHead(200, {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(JSON.stringify(buildPublicStats()));
    } catch (e) { res.writeHead(500); res.end(JSON.stringify({ error: String(e) })); }
    return;
  }

  /* /private/stats — localhost only */
  if (url === '/private/stats') {
    if (!local) {
      res.writeHead(403, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Private endpoint — accessible from localhost only.' }));
      return;
    }
    try {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(buildPrivateStats()));
    } catch (e) { res.writeHead(500); res.end(JSON.stringify({ error: String(e) })); }
    return;
  }

  /* /stats.json — legacy alias for /stats (public) */
  if (url === '/stats.json') {
    try {
      res.writeHead(200, {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(fs.readFileSync(STATS));
    } catch { res.writeHead(500); res.end('{}'); }
    return;
  }

  /* /adaptive.json — legacy alias, localhost-gated */
  if (url === '/adaptive.json') {
    if (!local) {
      res.writeHead(403, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Private endpoint — accessible from localhost only.' }));
      return;
    }
    try {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(buildAdaptive()));
    } catch (e) { res.writeHead(500); res.end(JSON.stringify({ error: String(e) })); }
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
  console.log(`Public stats feed     → http://localhost:${PORT}/stats`);
  console.log(`Private diagnostics   → http://localhost:${PORT}/private/stats  (localhost only)`);
});
