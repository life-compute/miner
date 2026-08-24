/**
 * LIFE Compute — Discovery NFT Minter
 * ─────────────────────────────────────────────────────────────────────────────
 * Mints a Metaplex Token-Metadata NFT on Solana for every novel discovery that
 * clears the discovery threshold (top-10% for its target) and has been
 * validator-confirmed on-chain.
 *
 * Supports two discovery modalities:
 *   • Protein/mRNA  — small-molecule binders scored by Boltz2 (affinity)
 *   • CRISPR gRNA   — 20-mer guide RNA sequences scored by three analytical
 *                     metrics (on-target, off-target, delivery)
 *
 * Usage (called by miner_daemon.py after a confirmed novel HIT):
 *   node scripts/mint_discovery_nft.js '<json-args>'
 *
 * ── Required JSON args (ALL sources) ─────────────────────────────────────────
 *   rpc             – Solana RPC URL
 *   authKeypair     – path to fee-payer keypair JSON (byte array)
 *   smiles          – SMILES string  OR  20-mer gRNA sequence
 *   affinity        – Boltz2 affinity score (kcal/mol, negative = good)
 *   targetId        – gene/target string  e.g. "TP53" or "TP53_CRISPR"
 *   targetName      – human-readable protein/target name
 *   uniprotId       – UniProt accession
 *   minerWallet     – discoverer pubkey (base58)
 *   validatorTx     – on-chain tx signature that confirmed the result
 *   timestamp       – ISO-8601 UTC timestamp
 *   discoveryRank   – ordinal hit for this target (1, 2, 3 …)
 *   discoveryNumber – global sequential LIFE discovery count (#N)
 *   foundationWallet – receiver of the minted NFT (base58)
 *   registryPath    – absolute path to output/discoveries.json
 *   dryRun          – (optional) if true, skip actual minting
 *   cluster         – (optional) "devnet" | "mainnet-beta" (default: devnet)
 *
 * ── Additional args for CRISPR gRNA discoveries ───────────────────────────────
 *   isCrispr          – true  (boolean flag)
 *   geneName          – gene symbol e.g. "TP53"
 *   cancerIndication  – e.g. "colorectal, lung, breast, ovarian"
 *   grnaOnTarget      – on-target efficiency score  (0–1)
 *   grnaOffTarget     – off-target risk score        (0–1)
 *   grnaDelivery      – delivery compatibility score (0–1.1)
 *
 * stdout: one JSON line (last line) consumed by Python — {status, mint, tx, …}
 * stderr: verbose diagnostic log
 */

'use strict';

// Metaplex packages live in the Anchor core dir's node_modules.
// NODE_PATH is set by the Python caller; this is just a readable comment.
// (Module.globalPaths mutation is unreliable in Node ≥ 18 — use NODE_PATH.)

const { createUmi }           = require('@metaplex-foundation/umi-bundle-defaults');
const { keypairIdentity, generateSigner, percentAmount, createSignerFromKeypair }
                              = require('@metaplex-foundation/umi');
const { createNft, mplTokenMetadata }
                              = require('@metaplex-foundation/mpl-token-metadata');
const { fromWeb3JsKeypair }   = require('@metaplex-foundation/umi-web3js-adapters');
const fs   = require('fs');
const path = require('path');

function log(...args) {
  process.stderr.write('[mint_discovery_nft] ' + args.join(' ') + '\n');
}

function ordinalSuffix(n) {
  const s = ['th','st','nd','rd'];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// ── Metadata builders ─────────────────────────────────────────────────────────

/**
 * Build metadata for a CRISPR gRNA discovery.
 * Name: "LIFE Discovery #N — [GENE] gRNA [DATE]"
 */
function buildCrisprMetadata(args) {
  const date    = args.timestamp.slice(0, 10);           // YYYY-MM-DD
  const rank    = ordinalSuffix(Number(args.discoveryRank));
  const num     = Number(args.discoveryNumber);
  const gene    = args.geneName || args.targetId.replace('_CRISPR', '');

  const name    = `LIFE Discovery #${num} — ${gene} gRNA ${date}`;
  const symbol  = 'LIFE-DSC';

  const description =
    `Top-10% CRISPR guide RNA for ${gene} discovered by LIFE Compute miner ` +
    `${args.minerWallet}. ` +
    `This 20-mer gRNA is the ${rank} validated guide for ${gene} ` +
    `identified by the LIFE decentralized drug-discovery network. ` +
    `Cancer indication: ${args.cancerIndication || 'oncology'}.`;

  const attributes = [
    { trait_type: 'modality',           value: 'CRISPR gRNA' },
    { trait_type: 'target_id',          value: args.targetId },
    { trait_type: 'gene_name',          value: gene },
    { trait_type: 'cancer_indication',  value: args.cancerIndication || '' },
    { trait_type: 'uniprot_id',         value: args.uniprotId },
    { trait_type: 'grna_sequence',      value: args.smiles },           // 20-mer
    { trait_type: 'on_target_score',    value: String(Number(args.grnaOnTarget  || 0).toFixed(4)) },
    { trait_type: 'off_target_score',   value: String(Number(args.grnaOffTarget || 0).toFixed(4)) },
    { trait_type: 'delivery_score',     value: String(Number(args.grnaDelivery  || 0).toFixed(4)) },
    { trait_type: 'combined_affinity',  value: String(args.affinity) },
    { trait_type: 'discovery_rank',     value: String(args.discoveryRank) },
    { trait_type: 'discovery_number',   value: String(num) },
    { trait_type: 'miner_wallet',       value: args.minerWallet },
    { trait_type: 'validator_tx',       value: args.validatorTx },
    { trait_type: 'timestamp',          value: args.timestamp },
    { trait_type: 'source',             value: 'LIFE Compute / CRISPR gRNA Optimizer' },
    { trait_type: 'proceeds',           value: '100% to LIFE Foundation' },
  ];

  return { name, symbol, description, attributes };
}

/**
 * Build metadata for a protein / mRNA small-molecule discovery.
 * Name: "LIFE Discovery #N — [TARGET] [DATE]"  (original format)
 */
function buildMoleculeMetadata(args) {
  const date    = args.timestamp.slice(0, 10);
  const rank    = ordinalSuffix(Number(args.discoveryRank));
  const num     = Number(args.discoveryNumber);

  const name    = `LIFE Discovery #${num} — ${args.targetId} ${date}`;
  const symbol  = 'LIFE-DSC';

  const description =
    `First confirmed computational hit for ${args.targetName} ` +
    `discovered by LIFE Compute miner ${args.minerWallet}. ` +
    `This molecule is the ${rank} validated binder for ${args.targetId} ` +
    `identified by the LIFE decentralized drug-discovery network.`;

  const attributes = [
    { trait_type: 'modality',           value: 'small molecule' },
    { trait_type: 'target_id',          value: args.targetId },
    { trait_type: 'target_name',        value: args.targetName },
    { trait_type: 'uniprot_id',         value: args.uniprotId },
    { trait_type: 'smiles',             value: args.smiles },
    { trait_type: 'affinity_kcal_mol',  value: String(args.affinity) },
    { trait_type: 'discovery_rank',     value: String(args.discoveryRank) },
    { trait_type: 'discovery_number',   value: String(num) },
    { trait_type: 'miner_wallet',       value: args.minerWallet },
    { trait_type: 'validator_tx',       value: args.validatorTx },
    { trait_type: 'timestamp',          value: args.timestamp },
    { trait_type: 'source',             value: 'LIFE Compute / Boltz2' },
    { trait_type: 'proceeds',           value: '100% to LIFE Foundation' },
  ];

  return { name, symbol, description, attributes };
}

function buildMetadata(args) {
  return args.isCrispr ? buildCrisprMetadata(args) : buildMoleculeMetadata(args);
}

// ── Off-chain metadata: save to disk + return a short URL ───────────────────
// Solana transactions have a 1232-byte hard limit.  A base64-encoded JSON blob
// in the uri field blows this limit (~3700 bytes).  Instead we:
//   1. Write the full metadata JSON to output/discovery_metadata/<n>.json
//   2. Return a short canonical URL that fits comfortably inside the tx.
// The URI is stored on-chain; wallets fetch it at display time.
// On devnet the URL does not need to be live at mint time — only at display time.
function buildMetadataUri(meta, args) {
  const isCrispr = Boolean(args.isCrispr);
  const discNum  = Number(args.discoveryNumber);

  const json = {
    name:        meta.name,
    symbol:      meta.symbol,
    description: meta.description,
    image:       isCrispr
      ? 'https://life-compute.github.io/assets/discovery-nft-crispr.png'
      : 'https://life-compute.github.io/assets/discovery-nft.png',
    external_url: 'https://life-compute.io',
    attributes:  meta.attributes,
    properties: {
      category: 'image',
      creators: [
        { address: args.foundationWallet, share: 100 },
      ],
    },
  };

  // Save full metadata locally for reference / future hosting
  try {
    const registryDir = path.dirname(args.registryPath);
    const metaDir     = path.join(registryDir, 'discovery_metadata');
    if (!fs.existsSync(metaDir)) fs.mkdirSync(metaDir, { recursive: true });
    const metaPath = path.join(metaDir, `${discNum}.json`);
    fs.writeFileSync(metaPath, JSON.stringify(json, null, 2));
    log(`metadata saved → ${metaPath}`);
  } catch (e) {
    log(`WARNING: could not save metadata file: ${e.message}`);
  }

  // Short canonical URL — well under Solana's tx size limit
  return `https://life-compute.io/discoveries/${discNum}`;
}

// ── Discovery registry helpers ───────────────────────────────────────────────

/**
 * Registry schema:
 *   { discoveries: [...], smiles_index: {}, grna_index: {}, target_counts: {} }
 *
 * grna_index keys on the 20-mer sequence (same as smiles_index keys on SMILES).
 * Both gates use the same duplicate-prevention logic; they are separate indices
 * so SMILES and gRNA sequences can never collide even if they share characters.
 */
function loadRegistry(registryPath) {
  try {
    if (fs.existsSync(registryPath)) {
      const reg = JSON.parse(fs.readFileSync(registryPath, 'utf8'));
      // Backfill grna_index for registries created before CRISPR support
      if (!reg.grna_index) reg.grna_index = {};
      return reg;
    }
  } catch (e) {
    log('registry load error (starting fresh):', e.message);
  }
  return { discoveries: [], smiles_index: {}, grna_index: {}, target_counts: {} };
}

function saveRegistry(registryPath, registry) {
  const dir = path.dirname(registryPath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(registryPath, JSON.stringify(registry, null, 2));
}

// ── Main ─────────────────────────────────────────────────────────────────────
(async () => {
  const args = JSON.parse(process.argv[2]);

  // ── Required field validation ──────────────────────────────────────────────
  const required = ['rpc','authKeypair','smiles','affinity','targetId',
                    'targetName','uniprotId','minerWallet','validatorTx',
                    'timestamp','discoveryRank','discoveryNumber',
                    'foundationWallet','registryPath'];
  for (const k of required) {
    if (args[k] === undefined || args[k] === null || args[k] === '') {
      const err = { status: 'error', error: `Missing required arg: ${k}` };
      process.stdout.write(JSON.stringify(err) + '\n');
      process.exit(1);
    }
  }

  // ── CRISPR-specific validation ─────────────────────────────────────────────
  const isCrispr = Boolean(args.isCrispr);
  if (isCrispr) {
    // 20-mer: exactly 20 nucleotide characters A/C/G/T/U (case-insensitive)
    if (!/^[ACGTUacgtu]{20}$/.test(String(args.smiles).trim())) {
      const err = {
        status: 'error',
        error:  `Invalid gRNA sequence (must be 20-mer ACGTU): '${args.smiles}'`,
      };
      process.stdout.write(JSON.stringify(err) + '\n');
      process.exit(1);
    }
  }

  const dryRun  = Boolean(args.dryRun);
  const cluster = args.cluster || 'devnet';

  if (isCrispr) {
    log('modality  : CRISPR gRNA');
    log('gRNA      :', args.smiles);
    log('gene      :', args.geneName || args.targetId);
    log('indication:', args.cancerIndication || '(none)');
    log('on-target :', args.grnaOnTarget);
    log('off-target:', args.grnaOffTarget);
    log('delivery  :', args.grnaDelivery);
  } else {
    log('modality  : small molecule');
    log('smiles    :', String(args.smiles).slice(0, 80));
  }
  log('target    :', args.targetId, '/', args.targetName);
  log('affinity  :', args.affinity, 'kcal/mol');
  log('validator :', args.validatorTx);
  log('miner     :', args.minerWallet);
  log('foundation:', args.foundationWallet);
  log('rank      :', args.discoveryRank, '| discovery#:', args.discoveryNumber);
  log('dryRun    :', dryRun);
  log('cluster   :', cluster);

  // ── Registry: duplicate gate ───────────────────────────────────────────────
  // CRISPR uses grna_index (keyed on the 20-mer); molecules use smiles_index.
  const registry   = loadRegistry(args.registryPath);
  const seqKey     = String(args.smiles).trim();
  const dupeIndex  = isCrispr ? registry.grna_index : registry.smiles_index;
  const dupeLabel  = isCrispr ? 'gRNA sequence' : 'SMILES';

  if (dupeIndex[seqKey]) {
    const prev = dupeIndex[seqKey];
    log(`DUPLICATE ${dupeLabel} — already minted as discovery #${prev.discovery_number} tx=${prev.mint_tx}`);
    const result = {
      status: 'duplicate',
      reason: isCrispr ? 'grna_already_minted' : 'smiles_already_minted',
      previous_mint: prev,
    };
    process.stdout.write(JSON.stringify(result) + '\n');
    process.exit(0);
  }

  // ── Build metadata ─────────────────────────────────────────────────────────
  const meta    = buildMetadata(args);
  const metadataUri = buildMetadataUri(meta, args);
  log('NFT name  :', meta.name);

  if (dryRun) {
    log('DRY RUN — skipping actual mint');
    const result = {
      status:           'dry_run',
      nft_name:         meta.name,
      symbol:           meta.symbol,
      foundation_wallet: args.foundationWallet,
      metadata_preview: meta,
    };
    process.stdout.write(JSON.stringify(result) + '\n');
    process.exit(0);
  }

  // ── Load fee-payer keypair ────────────────────────────────────────────────
  const rawBytes  = JSON.parse(fs.readFileSync(args.authKeypair, 'utf8'));
  const { Keypair } = require('@solana/web3.js');
  const web3Kp    = Keypair.fromSecretKey(Buffer.from(rawBytes));

  // ── Set up Metaplex UMI ───────────────────────────────────────────────────
  const umi = createUmi(args.rpc).use(mplTokenMetadata());
  const umiKp   = fromWeb3JsKeypair(web3Kp);
  const signer  = createSignerFromKeypair(umi, umiKp);
  umi.use(keypairIdentity(signer));

  const mintSigner = generateSigner(umi);
  log('mint address (new):', mintSigner.publicKey);

  // ── Foundation wallet (NFT destination) ──────────────────────────────────
  const { publicKey } = require('@metaplex-foundation/umi');
  const foundationPk  = publicKey(args.foundationWallet);

  // ── Mint the NFT ──────────────────────────────────────────────────────────
  log('Sending createNft transaction…');
  const txResult = await createNft(umi, {
    mint:         mintSigner,
    name:         meta.name,
    symbol:       meta.symbol,
    uri:          metadataUri,
    sellerFeeBasisPoints: percentAmount(0),   // 0% royalty; foundation takes 100% of sales
    isMutable:    false,                      // provenance immutable
    tokenOwner:   foundationPk,
    creators: [{ address: foundationPk, verified: false, share: 100 }],
  }).sendAndConfirm(umi, { confirm: { commitment: 'confirmed' } });

  const mintTx   = Buffer.from(txResult.signature).toString('base64');
  const mintAddr = mintSigner.publicKey.toString();

  log('✔ NFT minted');
  log('  mint    :', mintAddr);
  log('  tx      :', mintTx);
  log('  explorer: https://explorer.solana.com/address/' + mintAddr +
      '?cluster=' + cluster);

  // ── Update discovery registry ─────────────────────────────────────────────
  const entry = {
    discovery_number:  Number(args.discoveryNumber),
    discovery_rank:    Number(args.discoveryRank),
    target_id:         args.targetId,
    target_name:       args.targetName,
    uniprot_id:        args.uniprotId,
    miner_wallet:      args.minerWallet,
    validator_tx:      args.validatorTx,
    timestamp:         args.timestamp,
    nft_name:          meta.name,
    mint_address:      mintAddr,
    mint_tx:           mintTx,
    foundation_wallet: args.foundationWallet,
    cluster,
    // Modality-specific fields
    ...(isCrispr ? {
      modality:          'crispr_grna',
      grna_sequence:     seqKey,
      gene_name:         args.geneName || args.targetId.replace('_CRISPR', ''),
      cancer_indication: args.cancerIndication || '',
      on_target_score:   Number(args.grnaOnTarget  || 0),
      off_target_score:  Number(args.grnaOffTarget || 0),
      delivery_score:    Number(args.grnaDelivery  || 0),
      affinity:          Number(args.affinity),
    } : {
      modality:  'small_molecule',
      smiles:    seqKey,
      affinity:  Number(args.affinity),
    }),
  };

  registry.discoveries.push(entry);

  // Index by the sequence key in the correct gate
  const indexEntry = {
    discovery_number: entry.discovery_number,
    mint_address:     mintAddr,
    mint_tx:          mintTx,
  };
  if (isCrispr) {
    registry.grna_index[seqKey] = indexEntry;
  } else {
    registry.smiles_index[seqKey] = indexEntry;
  }

  registry.target_counts[args.targetId] =
    (registry.target_counts[args.targetId] || 0) + 1;

  saveRegistry(args.registryPath, registry);
  log('Registry saved:', args.registryPath);

  const result = {
    status:           'minted',
    nft_name:         meta.name,
    mint_address:     mintAddr,
    mint_tx:          mintTx,
    discovery_number: entry.discovery_number,
    foundation_wallet: args.foundationWallet,
    explorer: `https://explorer.solana.com/address/${mintAddr}?cluster=${cluster}`,
  };
  process.stdout.write(JSON.stringify(result) + '\n');
})().catch(err => {
  const out = { status: 'error', error: err.message || String(err) };
  process.stderr.write('[mint_discovery_nft] FATAL: ' + err.message + '\n');
  process.stdout.write(JSON.stringify(out) + '\n');
  process.exit(1);
});
