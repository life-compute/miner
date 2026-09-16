/**
 * verify_discovery_nft.js — discovery-NFT pipeline verifier.
 *
 * Sibling of the verify_peg_* / verify_affinity_fix family, and the canonical
 * gate for scripts/mint_discovery_nft.js plus the public gallery that renders
 * its output.
 *
 * Every mint between 2026-09-14 and 2026-09-16 failed with NameTooLong (0xb):
 * 109 attempts, 0 successes, silently — saveRegistry runs downstream of
 * createNft, so output/discoveries.json never left its empty initial state and
 * the gallery showed zero discoveries. The umi client serializes name/symbol
 * as variable-length strings and validates neither, so nothing catches an
 * over-long name before the chain rejects it.
 *
 *   A. Source parses; no builder emits a name wider than the on-chain cap.
 *   B. assertFieldFits guards both fields and runs before the dry-run exit.
 *   C. Dry-run yields a short name, keeps the date off-chain, writes no registry.
 *   D. A known-good mint still satisfies the cap on-chain.
 *   E. discoveries.html maps modality/affinity/mint_tx into a correct card.
 *
 * D and E need devnet; they are SKIPPED, not failed, when it is unreachable,
 * so the script still works offline. A-C are always enforced. Nothing is
 * minted and no real registry is touched: the dry-run writes to a scratch dir
 * that is removed on exit.
 *
 * Usage:
 *   node scripts/verify_discovery_nft.js
 *   node scripts/verify_discovery_nft.js --offline
 *   NODE_PATH=/tmp/life-compute/core/node_modules node scripts/verify_discovery_nft.js
 */

'use strict';

const { execFileSync } = require('child_process');
const fs   = require('fs');
const os   = require('os');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const MINT = path.join(REPO, 'scripts/mint_discovery_nft.js');

// Metaplex packages live in the Anchor core dir, same as the mint script.
const NODE_PATH = process.env.NODE_PATH || '/tmp/life-compute/core/node_modules';
const PAGE      = process.env.DISCOVERIES_HTML ||
                  '/tmp/life-compute.github.io/discoveries.html';

// Byte caps verified empirically against the deployed program: 32B accepted,
// 33B returns NameTooLong (mpl-token-metadata@3.4.0).
const MAX_NAME_BYTES   = 32;
const MAX_SYMBOL_BYTES = 10;

// First successful mint (devnet). Immutable, so it stays a valid fixture.
const KNOWN_MINT = 'CQ8yGe94RazwgwBPY4WwPa9cNQ8Eu5bX1yUKRCoyXfnZ';
const RPC        = 'https://api.devnet.solana.com';

const offline = process.argv.includes('--offline');
const bytes   = (s) => Buffer.byteLength(String(s), 'utf8');

let checks = 0;
const fails = [];

function check(cond, msg, detail) {
  checks++;
  if (!cond) fails.push(msg);
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${msg}${detail ? `  [${detail}]` : ''}`);
}

function skip(section, why) {
  console.log(`  ${section}: SKIPPED (${why})`);
}

/** Load the page's script body into global scope and render one entry. */
function renderCard(entry) {
  const body = fs.readFileSync(PAGE, 'utf8')
    .match(/<script>([\s\S]*)<\/script>\s*<\/body>/)[1];

  const els = {};
  // Mirror the real DOM: textContent coerces to string. Stats come from
  // .length, so a non-coercing stub compares unequal to its own output.
  const mk = (id) => (els[id] = {
    id, _t: '', innerHTML: '', style: {}, className: '',
    get textContent() { return this._t; },
    set textContent(v) { this._t = String(v); },
    addEventListener() {}, querySelectorAll: () => [],
    classList: { add() {}, remove() {} },
  });

  global.document = {
    getElementById: (id) => els[id] || mk(id),
    querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {},
  };
  global.window = {};
  global.fetch = async () => ({
    ok: true, status: 200, json: async () => ({ discoveries: [entry] }),
  });

  // Indirect eval → global scope; a direct eval would scope the page's
  // declarations to this strict-mode module and hide loadDiscoveries.
  (0, eval)(body);
  return { els, load: () => global.loadDiscoveries() };
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-discovery-nft-'));

  try {
    // ── A. templates fit the cap ─────────────────────────────────────────
    execFileSync('node', ['--check', MINT]);
    const src = fs.readFileSync(MINT, 'utf8');
    check(true, 'A: mint script parses');
    check(!/LIFE Discovery #\$\{num\} — /.test(src),
          'A: no legacy long-name template');
    const sites = (src.match(/`LIFE Discovery #\$\{num\}`/g) || []).length;
    check(sites === 2, 'A: both builders use the short name', `${sites} sites`);
    for (const n of [1, 999999, 2 ** 31]) {
      const name = `LIFE Discovery #${n}`;
      check(bytes(name) <= MAX_NAME_BYTES, `A: #${n} fits`, `${bytes(name)}B`);
    }

    // ── B. guard wired correctly ─────────────────────────────────────────
    check(/assertFieldFits\('name',\s+meta\.name,\s+MAX_NAME_BYTES\)/.test(src),
          'B: name guard present');
    check(/assertFieldFits\('symbol',\s+meta\.symbol,\s+MAX_SYMBOL_BYTES\)/.test(src),
          'B: symbol guard present');
    check(src.indexOf("assertFieldFits('name'") < src.indexOf('if (dryRun)'),
          'B: guard runs before the dry-run exit');
    check(new RegExp(`const MAX_NAME_BYTES\\s*=\\s*${MAX_NAME_BYTES};`).test(src),
          `B: MAX_NAME_BYTES is ${MAX_NAME_BYTES}`);

    // ── C. dry-run ───────────────────────────────────────────────────────
    const registryPath = path.join(tmp, 'discoveries.json');
    fs.writeFileSync(registryPath, JSON.stringify(
      { discoveries: [], smiles_index: {}, grna_index: {}, target_counts: {} }));

    const args = {
      rpc: RPC,
      authKeypair: path.join(tmp, 'ABSENT.json'),  // must never be read
      smiles: 'GACCTGGAGGTCCAGGTTAC', affinity: -8.577,
      targetId: 'PDL1_CRISPR', targetName: 'PD-L1', uniprotId: 'Q9NZQ7',
      minerWallet: 'verify', validatorTx: 'verify',
      timestamp: '2026-09-16T07:04:33+00:00',
      discoveryRank: 1, discoveryNumber: 1,
      foundationWallet: '2jVdMx7fb88txbG6YoZzC7kT4Tq8rJDaWrNgbZ3ZnqCb',
      registryPath, dryRun: true, cluster: 'devnet', isCrispr: true,
      geneName: 'PDL1', cancerIndication: 'multiple solid tumors',
      grnaOnTarget: 0.82, grnaOffTarget: 0.91, grnaDelivery: 0.74,
    };
    const stdout = execFileSync('node', [MINT, JSON.stringify(args)], {
      env: { ...process.env, NODE_PATH }, cwd: tmp, encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    const dry = JSON.parse(stdout.trim().split('\n').pop());

    check(dry.status === 'dry_run', 'C: dry-run completes', dry.status);
    check(bytes(dry.nft_name) <= MAX_NAME_BYTES, 'C: name fits',
          `${bytes(dry.nft_name)}B ${JSON.stringify(dry.nft_name)}`);
    check(bytes(dry.symbol) <= MAX_SYMBOL_BYTES, 'C: symbol fits',
          `${bytes(dry.symbol)}B`);
    check(/Discovered 2026-09-16\./.test(dry.metadata_preview.description),
          'C: date preserved in off-chain description');
    check(JSON.parse(fs.readFileSync(registryPath, 'utf8')).discoveries.length === 0,
          'C: dry-run writes no registry entry');

    if (offline) {
      skip('D. on-chain    ', '--offline');
      skip('E. gallery     ', '--offline');
      return;
    }

    // ── D. known-good mint on-chain ──────────────────────────────────────
    let onChainName = null;
    try {
      const { createUmi } = require(`${NODE_PATH}/@metaplex-foundation/umi-bundle-defaults`);
      const { mplTokenMetadata, fetchDigitalAsset } =
        require(`${NODE_PATH}/@metaplex-foundation/mpl-token-metadata`);
      const { publicKey } = require(`${NODE_PATH}/@metaplex-foundation/umi`);

      const umi = createUmi(RPC).use(mplTokenMetadata());
      const asset = await fetchDigitalAsset(umi, publicKey(KNOWN_MINT));
      onChainName = asset.metadata.name.replace(/\0+$/, '');

      check(bytes(onChainName) <= MAX_NAME_BYTES, 'D: on-chain name fits',
            `${bytes(onChainName)}B ${JSON.stringify(onChainName)}`);
      check(/^LIFE Discovery #\d+$/.test(onChainName),
            'D: on-chain name matches the current format', onChainName);
      check(asset.mint.supply.toString() === '1', 'D: supply is 1');
    } catch (e) {
      skip('D. on-chain    ', e.message.slice(0, 60));
    }

    // ── E. gallery render ────────────────────────────────────────────────
    if (!fs.existsSync(PAGE)) {
      skip('E. gallery     ', `${PAGE} not checked out`);
      return;
    }

    // Shaped exactly as mint_discovery_nft.js writes a registry entry.
    const entry = {
      discovery_number: 1, target_id: 'PDL1_CRISPR', target_name: 'PD-L1',
      miner_wallet: '4zn1WQ', timestamp: '2026-09-16T07:04:33+00:00',
      nft_name: onChainName || 'LIFE Discovery #1',
      mint_address: KNOWN_MINT, mint_tx: '1EUR4Qj1Q5Kg720Ci6',
      cluster: 'devnet', modality: 'crispr_grna',
      grna_sequence: 'GACCTGGAGGTCCAGGTTAC', gene_name: 'PDL1', affinity: -8.577,
    };

    const { els, load } = renderCard(entry);
    await load();
    const card = els['cards-grid'].innerHTML;

    check(els['stat-total'].textContent === '1', 'E: counted once');
    check(els['stat-crispr'].textContent === '1', 'E: typed as CRISPR');
    check(els['stat-protein'].textContent === '0', 'E: not miscounted as protein');
    check(els['stat-best'].textContent === '-8.5770', 'E: best affinity signed',
          els['stat-best'].textContent);
    check(card.includes('>CRISPR<'), 'E: CRISPR badge rendered');
    check(card.includes('-8.5770'), 'E: affinity on card');
    check(card.includes('cluster=devnet'), 'E: explorer uses the entry cluster');
    check(!card.includes('pointer-events:none'), 'E: explorer link enabled');
    check(!card.includes('Unknown Target'), 'E: target resolved');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main()
  .then(() => {
    console.log('\n' + '='.repeat(74));
    if (fails.length) {
      console.log(`FAILED (${fails.length}/${checks})`);
      fails.forEach((f) => console.log('  -', f));
      process.exit(1);
    }
    console.log(`assertions passed: ${checks}\n\nALL CHECKS PASSED`);
  })
  .catch((e) => {
    console.error('BLOCKER:', e.message);
    process.exit(1);
  });
