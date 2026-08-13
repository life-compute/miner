/**
 * LIFE Compute Dashboard — static file server on :3001
 *
 * Endpoints
 * ─────────
 *  GET  /stats          Public stats (anyone) — miner status, molecules, $LIFE, targets, scoring history
 *  GET  /private/stats  Private diagnostics (localhost only) — PULSE, ART, SCOUT, generated molecules
 *  GET  /stats.json     Legacy alias for /stats (kept for backward compat)
 *  GET  /adaptive.json  Legacy alias for private stats (kept for backward compat, localhost-gated)
 *  GET  /agent/status   Public: whether ANTHROPIC_API_KEY is configured
 *  POST /agent/chat     AI chat proxy → Anthropic API (requires ANTHROPIC_API_KEY in .env)
 *  GET  /*              Static files from dist/
 */
const http  = require('http');
const https = require('https');
const fs    = require('fs');
const path  = require('path');
const { exec } = require('child_process');

const PORT         = parseInt(process.env.DASHBOARD_PORT || '3001');
const DIST         = path.join(__dirname, 'dist');
const ROOT         = path.join(__dirname, '..');
const STATS        = path.join(ROOT, 'stats.json');
const OUT          = path.join(ROOT, 'output');
const ADAPTIVE_DIR = path.join(ROOT, 'adaptive');

/* ── .env fallback loader (PM2 env_file handles prod; this covers direct runs) */
(function loadDotEnv() {
  try {
    const envPath = path.join(ROOT, '.env');
    const lines = fs.readFileSync(envPath, 'utf8').split('\n');
    for (const line of lines) {
      const eq = line.indexOf('=');
      if (eq < 0 || line.trimStart().startsWith('#')) continue;
      const k = line.slice(0, eq).trim();
      const v = line.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
      if (k && !process.env[k]) process.env[k] = v;
    }
  } catch { /* .env absent — fine */ }
})();

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

/* ── Miner alive detection (robust — does not rely on stats.json "alive" field) ─
 *
 * Priority:
 *  1. stats.json "last_updated" freshness — daemon writes every POLL_SECONDS (60s).
 *     If written within the last 5 min, daemon is running.
 *  2. life_boltz_scores.jsonl file mtime — Boltz2 appends a row each scoring cycle.
 *     If modified within the last 5 min, miner is actively scoring.
 *  3. Fallback: stored "alive" flag (may be absent if stats.json was reset from template).
 *
 * Background: stats.json.template omits the "alive" key. If the template overwrites
 * stats.json (install/reset), the field disappears and the dashboard shows OFFLINE even
 * while the daemon is running. Freshness checks are immune to this.
 */
const ALIVE_WINDOW_MS = 5 * 60 * 1000; // 5 minutes
const BOLTZ_FILE      = path.join(OUT, 'life_boltz_scores.jsonl');

function isMinerAlive(s) {
  // 1. stats.json last_updated freshness
  if (s.last_updated) {
    try {
      const age = Date.now() - new Date(s.last_updated).getTime();
      if (age >= 0 && age < ALIVE_WINDOW_MS) return true;
    } catch { /* malformed date — fall through */ }
  }
  // 2. life_boltz_scores.jsonl file mtime
  try {
    const mtime = fs.statSync(BOLTZ_FILE).mtimeMs;
    if ((Date.now() - mtime) < ALIVE_WINDOW_MS) return true;
  } catch { /* file doesn't exist yet — fall through */ }
  // 3. Stored flag (fallback — may be absent)
  return s.alive ?? false;
}

/* ── /stats — PUBLIC data ────────────────────────────────────────── */
function buildPublicStats() {
  const s         = readJson(STATS) || {};
  // Read ALL boltz rows so early high scores (e.g. 0.0174 from day 1) are included.
  // tailJsonl(200) was silently dropping the best historical scores.
  const boltzRows = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 100000);
  const g         = s.global || {};

  // Scoring history: best boltz score per time bucket.
  // Bucket size is adaptive: target ~20–30 data points across the full data span.
  // With only the last 200 rows (5-min buckets) the chart showed repeated 0.0097
  // because the best-ever rows (from 3 days ago) fell outside the window.
  // Scoring history: best boltz score per 5-minute bucket, last 2 hours only.
  // Fixed window ensures recent scores appear immediately (no adaptive stretch).
  const TWO_HOURS_S = 2 * 3600;
  const FIVE_MIN_S  = 300;
  const now_s       = Date.now() / 1000;
  const cutoff_s    = now_s - TWO_HOURS_S;

  const validRows = boltzRows.filter(r => typeof r.boltz_score === 'number' && r.ts > cutoff_s);

  const epochBuckets = {};
  for (const r of validRows) {
    const epoch = Math.floor(r.ts / FIVE_MIN_S);
    if (!epochBuckets[epoch] || r.boltz_score > epochBuckets[epoch].best_score) {
      epochBuckets[epoch] = {
        epoch,
        best_score: Math.round(r.boltz_score * 10000) / 10000,
        target_id:  r.target_id || '?',
        ts_iso:     new Date(r.ts * 1000).toISOString(),
      };
    }
  }
  // Sort ascending (oldest→newest) so the chart renders left-to-right.
  // Cap at 30 buckets. history[history.length-1] is the most recent.
  const scoringHistory = Object.values(epochBuckets)
    .sort((a, b) => a.epoch - b.epoch)
    .slice(-30);

  // Network stats — pass through real on-chain values; null means "—" in UI.
  // Never substitute mock numbers.
  const network = {
    total_miners:       g.total_miners       ?? null,
    molecules_screened: g.molecules_screened ?? null,
    targets_solved:     g.targets_solved     ?? null,
  };

  return {
    alive:               isMinerAlive(s),
    current_target:      s.current_target      ?? '—',
    miner_id:            getMinerPubkey(),
    molecules_screened:  s.molecules_screened  ?? 0,
    life_earned:         s.life_earned         ?? 0,
    targets_contributed: s.targets_contributed ?? [],
    scoring_history:     scoringHistory,
    network,
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

/* ── LIFE AGENT — Anthropic API proxy ───────────────────────────── */

/** Read full POST body as parsed JSON. */
function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try { resolve(JSON.parse(body)); }
      catch (e) { reject(new Error('Invalid JSON body')); }
    });
    req.on('error', reject);
  });
}

/** Call Anthropic Messages API; return assistant text. */
function callAnthropic(apiKey, system, messages) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({
      model: 'claude-sonnet-4-6',
      max_tokens: 4096,
      system,
      messages,
    });
    const options = {
      hostname: 'api.anthropic.com',
      path: '/v1/messages',
      method: 'POST',
      headers: {
        'x-api-key':         apiKey,
        'anthropic-version': '2023-06-01',
        'content-type':      'application/json',
        'content-length':    Buffer.byteLength(payload),
      },
    };
    const req = https.request(options, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.error) {
            reject(new Error(parsed.error.message || `Anthropic API error (${res.statusCode})`));
          } else if (parsed.content && parsed.content[0]) {
            resolve(parsed.content[0].text);
          } else {
            reject(new Error(`Unexpected Anthropic response: ${data.slice(0, 200)}`));
          }
        } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

/** Build LIFE AGENT system prompt injecting live miner stats. */
function buildAgentSystemPrompt() {
  const stats = buildPublicStats();
  const statsStr = JSON.stringify({
    alive:               stats.alive,
    current_target:      stats.current_target,
    molecules_screened:  stats.molecules_screened,
    life_earned:         stats.life_earned,
    targets_contributed: stats.targets_contributed,
    network:             stats.network,
    last_updated:        stats.last_updated,
    peak_boltz_score:    stats.scoring_history.length
      ? Math.max(...stats.scoring_history.map(r => r.best_score || 0))
      : null,
  }, null, 2);

  return `You are LIFE AGENT — an AI assistant built into the LIFE Compute cancer drug discovery mining network. You help miners maximize their $LIFE earnings by building better molecule search algorithms.

You have access to their current mining stats: ${statsStr}

Your specialties:
- Helping miners build custom adaptive stacks (PULSE sweeps, ML predictors, molecule generators)
- Writing Python code for better molecule selection strategies
- Explaining cancer target biology and what chemical features bind well
- Debugging Boltz2 scoring issues
- Optimizing mining performance
- Explaining LIFE Compute mechanics

The adaptive/ directory is where miners build their custom search stack. life_generate.py, life_chembl.py and life_diversity.py are provided as starting tools. Help miners build life_pulse.py, life_art.py and life_scout.py themselves for competitive advantage.

You can save Python files directly to the miner's adaptive/ directory. When you write a complete Python file, end your response with: SAVE_FILE: filename.py — this will show a save button to the miner.

Current network stats: ${statsStr}`;
}

/* ── HTTP server ─────────────────────────────────────────────────── */
http.createServer(async (req, res) => {
  const url    = req.url.split('?')[0];
  const local  = isLocalhost(req);

  /* CORS preflight — allow all origins for dashboard use */
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin':  '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    });
    res.end();
    return;
  }

  /* /pulse — public: LIFE PULSE sweep status ───────────────────────────────── */
  if (url === '/pulse') {
    try {
      const STATE_JSON_PATH  = path.join(OUT, 'life_pulse_state.json');
      const PULSE_JSONL_PATH = path.join(OUT, 'life_pulse_data.jsonl');

      const state = readJson(STATE_JSON_PATH) || {};
      const rows  = tailJsonl(PULSE_JSONL_PATH, 5000);

      // Active if last sweep timestamp within last 90 seconds
      const lastSweepTs = state.last_sweep_ts || 0;
      const sweepActive = (Date.now() / 1000 - lastSweepTs) < 90;

      // Top 3 molecules by proxy_score
      const sorted = rows
        .filter(r => typeof r.proxy_score === 'number' && r.proxy_score > 0)
        .sort((a, b) => b.proxy_score - a.proxy_score)
        .slice(0, 3)
        .map(r => ({
          smiles:      (r.smiles || '').slice(0, 60),
          family:      r.family || '—',
          scaffold:    r.scaffold_name || '—',
          proxy_score: Math.round((r.proxy_score || 0) * 10000) / 10000,
          source:      r.source || 'pulse',
          ts:          r.ts ? new Date(r.ts * 1000).toISOString() : null,
        }));

      const tamAttempts = state.tanimoto_attempts || 0;
      const tamPasses   = state.tanimoto_passes   || 0;
      const tamPassRate = tamAttempts > 0
        ? Math.round((tamPasses / tamAttempts) * 10000) / 100
        : null;

      res.writeHead(200, {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(JSON.stringify({
        active:             sweepActive,
        total_evaluated:    state.total_evaluated     ?? rows.length,
        sobol_index:        state.next_index          ?? 0,
        current_batch_size: state.current_batch_size  ?? 200,
        last_sweep_ts:      lastSweepTs               || null,
        top_molecules:      sorted,
        mutant_attempted:   state.mutant_attempted    ?? 0,
        mutant_accepted:    state.mutant_accepted     ?? 0,
        tanimoto_attempts:  tamAttempts,
        tanimoto_passes:    tamPasses,
        tanimoto_pass_rate: tamPassRate,
        ts:                 Date.now(),
      }));
    } catch (e) { res.writeHead(500); res.end(JSON.stringify({ error: String(e) })); }
    return;
  }

  /* /feed — public: last 20 raw boltz scores, newest first */
  if (url === '/feed') {
    try {
      const rows = tailJsonl(path.join(OUT, 'life_boltz_scores.jsonl'), 20)
        .reverse()  // newest first
        .map(r => ({
          ts:          r.ts ? new Date(r.ts * 1000).toISOString() : null,
          target_id:   r.target_id   || '?',
          smiles:      (r.smiles     || '').slice(0, 30),
          boltz_score: typeof r.boltz_score === 'number'
                         ? Math.round(r.boltz_score * 10000) / 10000
                         : null,
          affinity:    typeof r.affinity === 'number'
                         ? Math.round(r.affinity * 1000) / 1000
                         : null,
          hit:         r.hit ?? false,
          source:      r.source || 'unknown',
        }));
      res.writeHead(200, {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(JSON.stringify({ rows, ts: Date.now() }));
    } catch (e) { res.writeHead(500); res.end(JSON.stringify({ error: String(e) })); }
    return;
  }

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

  /* ── LIFE AGENT endpoints ─────────────────────────────────────── */

  /* /agent/status — public: tells frontend if ANTHROPIC_API_KEY is configured */
  if (url === '/agent/status') {
    const configured = !!(process.env.ANTHROPIC_API_KEY || '').trim();
    res.writeHead(200, {
      'Content-Type':                'application/json',
      'Access-Control-Allow-Origin': '*',
    });
    res.end(JSON.stringify({ configured }));
    return;
  }

  /* /agent/chat — POST: proxy chat request to Anthropic API */
  if (url === '/agent/chat') {
    if (req.method !== 'POST') {
      res.writeHead(405, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'Method Not Allowed — use POST' }));
      return;
    }
    const apiKey = (process.env.ANTHROPIC_API_KEY || '').trim();
    if (!apiKey) {
      res.writeHead(403, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'ANTHROPIC_API_KEY not configured in .env' }));
      return;
    }
    try {
      const body     = await readBody(req);
      const messages = body.messages;
      if (!Array.isArray(messages) || messages.length === 0) {
        throw new Error('messages array required');
      }
      const system = buildAgentSystemPrompt();
      const reply  = await callAnthropic(apiKey, system, messages);
      res.writeHead(200, {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(JSON.stringify({ content: reply }));
    } catch (e) {
      console.error('[LIFE AGENT] chat error:', e.message);
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  /* /agent/write-file — POST: write a .py file into adaptive/ */
  if (url === '/agent/write-file') {
    if (req.method !== 'POST') {
      res.writeHead(405, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ error: 'Method Not Allowed — use POST' }));
      return;
    }
    const apiKey = (process.env.ANTHROPIC_API_KEY || '').trim();
    if (!apiKey) {
      res.writeHead(403, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ error: 'ANTHROPIC_API_KEY not configured in .env' }));
      return;
    }
    try {
      const body = await readBody(req);
      const { filename, content } = body;

      // Validate filename: must end in .py, no path separators
      if (typeof filename !== 'string' || !filename.endsWith('.py') ||
          filename.includes('/') || filename.includes('\\') || filename.includes('..')) {
        res.writeHead(400, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ error: 'filename must be a plain .py filename (no path separators)' }));
        return;
      }
      if (typeof content !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ error: 'content (string) required' }));
        return;
      }

      // Resolve and verify the target path stays within ADAPTIVE_DIR
      const fullPath = path.resolve(ADAPTIVE_DIR, filename);
      if (!fullPath.startsWith(ADAPTIVE_DIR + path.sep) && fullPath !== ADAPTIVE_DIR) {
        res.writeHead(403, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ error: 'Path outside adaptive/ is not allowed' }));
        return;
      }

      // Ensure adaptive/ exists
      fs.mkdirSync(ADAPTIVE_DIR, { recursive: true });
      fs.writeFileSync(fullPath, content, 'utf8');

      console.log(`[LIFE AGENT] wrote ${fullPath} (${content.length} bytes)`);
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ success: true, path: fullPath }));
    } catch (e) {
      console.error('[LIFE AGENT] write-file error:', e.message);
      res.writeHead(500, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  /* /agent/restart-miner — POST: restart the life-miner PM2 process */
  if (url === '/agent/restart-miner') {
    if (req.method !== 'POST') {
      res.writeHead(405, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ error: 'Method Not Allowed — use POST' }));
      return;
    }
    const apiKey = (process.env.ANTHROPIC_API_KEY || '').trim();
    if (!apiKey) {
      res.writeHead(403, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ error: 'ANTHROPIC_API_KEY not configured in .env' }));
      return;
    }
    exec('pm2 restart life-miner', { timeout: 15000 }, (err, stdout, stderr) => {
      if (err) {
        console.error('[LIFE AGENT] restart-miner error:', err.message);
        res.writeHead(500, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
        res.end(JSON.stringify({ error: err.message, stderr }));
        return;
      }
      console.log('[LIFE AGENT] pm2 restart life-miner →', stdout.trim());
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify({ success: true, stdout: stdout.trim() }));
    });
    return;
  }

  /* ── Static files from dist/ ─────────────────────────────────── */
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
  console.log(`LIFE AGENT status     → http://localhost:${PORT}/agent/status`);
  console.log(`LIFE AGENT chat       → http://localhost:${PORT}/agent/chat  (POST)`);
  console.log(`LIFE AGENT write-file → http://localhost:${PORT}/agent/write-file  (POST)`);
  console.log(`LIFE AGENT restart    → http://localhost:${PORT}/agent/restart-miner  (POST)`);
});
