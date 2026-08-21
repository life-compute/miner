/**
 * LIFE Compute — Discovery NFT Minter
 * ─────────────────────────────────────────────────────────────────────────────
 * Mints a Metaplex Token-Metadata NFT on Solana for every novel molecule that
 * clears the discovery threshold (top-10% affinity for its target) and has
 * been validator-confirmed on-chain.
 *
 * Usage (called by miner_daemon.py after a confirmed novel HIT):
 *   node scripts/mint_discovery_nft.js '<json-args>'
 *
 * Required JSON args:
 *   rpc             – Solana RPC URL
 *   authKeypair     – path to fee-payer keypair JSON (byte array)
 *   smiles          – SMILES string of the discovered molecule
 *   affinity        – Boltz2 affinity score (kcal/mol, negative = good)
 *   targetId        – gene/target string  e.g. "TP53"
 *   targetName      – human-readable protein name
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

function buildMetadata(args) {
  const date    = args.timestamp.slice(0, 10);                // YYYY-MM-DD
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
    { trait_type: 'target_id',           value: args.targetId },
    { trait_type: 'target_name',         value: args.targetName },
    { trait_type: 'uniprot_id',          value: args.uniprotId },
    { trait_type: 'smiles',              value: args.smiles },
    { trait_type: 'affinity_kcal_mol',   value: String(args.affinity) },
    { trait_type: 'discovery_rank',      value: String(args.discoveryRank) },
    { trait_type: 'discovery_number',    value: String(num) },
    { trait_type: 'miner_wallet',        value: args.minerWallet },
    { trait_type: 'validator_tx',        value: args.validatorTx },
    { trait_type: 'timestamp',           value: args.timestamp },
    { trait_type: 'source',              value: 'LIFE Compute / Boltz2' },
    { trait_type: 'proceeds',            value: '100% to LIFE Foundation' },
  ];

  return { name, symbol, description, attributes };
}

// ── Minimal off-chain JSON uploader (data URI — no external upload needed) ──
// Metaplex reads the URI at display time.  We embed metadata inline so the
// script works without Arweave/IPFS credentials.  A real deployment would swap
// this for umi.use(irysUploader()) or a pinata call.
function buildDataUri(meta, args) {
  const json = {
    name:        meta.name,
    symbol:      meta.symbol,
    description: meta.description,
    image:       'https://life-compute.github.io/assets/discovery-nft.png',
    external_url: 'https://life-compute.io',
    attributes:  meta.attributes,
    properties: {
      category: 'image',
      creators: [
        { address: args.foundationWallet, share: 100 },
      ],
    },
  };
  return 'data:application/json;base64,' +
    Buffer.from(JSON.stringify(json)).toString('base64');
}

// ── Discovery registry helpers ───────────────────────────────────────────────
function loadRegistry(registryPath) {
  try {
    if (fs.existsSync(registryPath)) {
      return JSON.parse(fs.readFileSync(registryPath, 'utf8'));
    }
  } catch (e) {
    log('registry load error (starting fresh):', e.message);
  }
  return { discoveries: [], smiles_index: {}, target_counts: {} };
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

  const dryRun  = Boolean(args.dryRun);
  const cluster = args.cluster || 'devnet';

  log('smiles    :', args.smiles.slice(0, 80));
  log('target    :', args.targetId, '/', args.targetName);
  log('affinity  :', args.affinity, 'kcal/mol');
  log('validator :', args.validatorTx);
  log('miner     :', args.minerWallet);
  log('foundation:', args.foundationWallet);
  log('rank      :', args.discoveryRank, '| discovery#:', args.discoveryNumber);
  log('dryRun    :', dryRun);
  log('cluster   :', cluster);

  // ── Registry: duplicate SMILES check ──────────────────────────────────────
  const registry = loadRegistry(args.registryPath);
  const smilesKey = args.smiles.trim();

  if (registry.smiles_index[smilesKey]) {
    const prev = registry.smiles_index[smilesKey];
    log(`DUPLICATE SMILES — already minted as discovery #${prev.discovery_number} tx=${prev.mint_tx}`);
    const result = {
      status: 'duplicate',
      reason: 'smiles_already_minted',
      previous_mint: prev,
    };
    process.stdout.write(JSON.stringify(result) + '\n');
    process.exit(0);
  }

  // ── Build metadata ─────────────────────────────────────────────────────────
  const meta    = buildMetadata(args);
  const dataUri = buildDataUri(meta, args);
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
    uri:          dataUri,
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
    smiles:            args.smiles,
    affinity:          Number(args.affinity),
    miner_wallet:      args.minerWallet,
    validator_tx:      args.validatorTx,
    timestamp:         args.timestamp,
    nft_name:          meta.name,
    mint_address:      mintAddr,
    mint_tx:           mintTx,
    foundation_wallet: args.foundationWallet,
    cluster,
  };

  registry.discoveries.push(entry);
  registry.smiles_index[smilesKey] = {
    discovery_number: entry.discovery_number,
    mint_address:     mintAddr,
    mint_tx:          mintTx,
  };
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
