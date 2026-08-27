"""
life_brain_model.py — LIFE-BRAIN model classes and featurization.

Imported by both life_brain.py (training) and life_brain_gateway.py (inference).
Never import this from miner_daemon.py directly — use life_brain_gateway.py.

Architecture
------------
Shared embedding table:
  nn.Embedding(4096, 16) — target_id used directly as index (u16 fits in 4096).
  New target IDs are automatically assigned their own slot, no registration needed.

Protein / mRNA branch (identical architecture, separate weight tensors):
  Input: 2056-dim (Morgan2048 + 8 RDKit physico-chem descriptors) + 16-dim target embed
  Linear(2072 → 256) → ReLU → Dropout(0.3) → Linear(256,64) → ReLU → Linear(64,2)
  Output: [predicted_affinity, log_variance]

CRISPR branch:
  Sequence encoder: Conv1d(4→32, k=3, pad=1) → ReLU → Conv1d(32→32, k=3, pad=1) → ReLU
                    → GlobalAvgPool → 32-dim
  Handcrafted features: 25-dim (GC, hotspot_hamming, PAM_flag, homopolymer,
                                  dinucleotide×16, stem_loop, gc_clamp, first_half_gc,
                                  second_half_gc, off_target_proxy)
  Concat: 32 + 25 + 16 = 73-dim
  Linear(73→64) → ReLU → Dropout(0.3) → Linear(64,32) → ReLU → Linear(32,2)
  Output: [predicted_iptm, log_variance]

CPU-only: all tensors/modules use device='cpu' exclusively.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("life-brain")

# ── Constants ─────────────────────────────────────────────────────────────────
EMBED_DIM   = 16
MAX_EMBED   = 4096   # max target_id index; u16 target_ids are all < 65536 but range is ≤ 3009
MORGAN_BITS = 2048
N_RDKIT     = 8      # MW, logP, HBD, HBA, TPSA, RotBonds, RingCount, HeavyAtoms
SMILES_FEAT_DIM = MORGAN_BITS + N_RDKIT   # 2056

# CRISPR dims
CONV_OUT_DIM      = 32   # after global avg pool
N_HANDCRAFTED     = 25
CRISPR_FUSION_DIM = CONV_OUT_DIM + N_HANDCRAFTED + EMBED_DIM   # 73

# Modality ranges (from on-chain target_id u16)
PROTEIN_ID_MIN = 0
PROTEIN_ID_MAX = 1999
MRNA_ID_MIN    = 2000
MRNA_ID_MAX    = 2029
CRISPR_ID_MIN  = 3000
CRISPR_ID_MAX  = 3009

_DINUCS = [
    "AA", "AC", "AG", "AT",
    "CA", "CC", "CG", "CT",
    "GA", "GC", "GG", "GT",
    "TA", "TC", "TG", "TT",
]


# ── Modality resolver ─────────────────────────────────────────────────────────

def modality_from_target_id(target_id: Optional[int], seq: str = "") -> str:
    """
    Primary: target_id numeric range.
    Fallback: ACGT/length-20 heuristic when target_id is None.
    """
    if target_id is not None:
        tid = int(target_id)
        if PROTEIN_ID_MIN <= tid <= PROTEIN_ID_MAX:
            return "protein"
        if MRNA_ID_MIN <= tid <= MRNA_ID_MAX:
            return "mrna"
        if CRISPR_ID_MIN <= tid <= CRISPR_ID_MAX:
            return "crispr"
    # Fallback: sequence heuristic
    s = seq.upper().strip()
    if len(s) == 20 and all(c in "ACGT" for c in s):
        return "crispr"
    return "protein"


# ── Protein / mRNA featurization ──────────────────────────────────────────────

def featurize_smiles(smiles: str) -> Optional[list[float]]:
    """
    Return 2056-dim feature vector [Morgan2048 | 8 RDKit descriptors] or None.
    Lazy rdkit import — module loads cleanly without rdkit installed.
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors
        from rdkit.Chem import rdFingerprintGenerator as rfg
    except ImportError:
        return None

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None

    try:
        gen = rfg.GetMorganGenerator(radius=2, fpSize=MORGAN_BITS)
        fp  = list(gen.GetFingerprintAsNumPy(mol).astype(float))
    except Exception:
        return None

    try:
        desc = [
            Descriptors.MolWt(mol),
            Descriptors.MolLogP(mol),
            rdMolDescriptors.CalcNumHBD(mol),
            rdMolDescriptors.CalcNumHBA(mol),
            Descriptors.TPSA(mol),
            rdMolDescriptors.CalcNumRotatableBonds(mol),
            rdMolDescriptors.CalcNumRings(mol),
            float(mol.GetNumHeavyAtoms()),
        ]
    except Exception:
        desc = [0.0] * N_RDKIT

    return fp + desc   # 2056 floats


# ── CRISPR featurization ──────────────────────────────────────────────────────

def _hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def _count_stem_loop(seq: str) -> float:
    """Heuristic: fraction of 5′ bases that are complementary to 3′ bases."""
    comp = {"A": "T", "T": "A", "C": "G", "G": "C"}
    pairs = sum(1 for i in range(5) if comp.get(seq[i]) == seq[-(i + 1)])
    return pairs / 5.0


def featurize_crispr_handcrafted(seq: str, hotspots: list[str] | None = None) -> Optional[list[float]]:
    """
    25-dim handcrafted feature vector for a 20-mer gRNA.

    Dims 0–3:    GC content, hotspot Hamming, PAM flag, max homopolymer
    Dims 4–19:   Dinucleotide frequencies (16 dims)
    Dim 20:      Stem-loop score
    Dim 21:      GC clamp (last-5 GC ratio)
    Dim 22:      First-half GC
    Dim 23:      Second-half GC
    Dim 24:      Off-target proxy (0 without a database; placeholder)
    """
    seq = seq.upper()
    if len(seq) != 20 or not all(c in "ACGT" for c in seq):
        return None

    gc = (seq.count("G") + seq.count("C")) / 20.0

    if hotspots:
        valid_hs = [h for h in hotspots if len(h) == 20]
        min_dist = float(min(_hamming(seq, h) for h in valid_hs)) if valid_hs else 10.0
    else:
        min_dist = 10.0
    ham_norm = min_dist / 20.0

    tail = seq[-3:]
    pam_flag = 0.0 if (tail[1] == "G" and tail[2] == "G") else 1.0

    max_run = cur = 1
    for i in range(1, 20):
        cur = cur + 1 if seq[i] == seq[i - 1] else 1
        max_run = max(max_run, cur)
    homopolymer = max_run / 20.0

    dinu = {d: 0.0 for d in _DINUCS}
    for i in range(19):
        di = seq[i:i + 2]
        if di in dinu:
            dinu[di] += 1.0
    dinu_feats = [dinu[d] / 19.0 for d in _DINUCS]

    stem_loop = _count_stem_loop(seq)
    gc_clamp  = (seq[-5:].count("G") + seq[-5:].count("C")) / 5.0
    first_gc  = (seq[:10].count("G") + seq[:10].count("C")) / 10.0
    second_gc = (seq[10:].count("G") + seq[10:].count("C")) / 10.0
    off_tgt   = 0.0   # placeholder; no off-target db at inference time

    feats = [gc, ham_norm, pam_flag, homopolymer] + dinu_feats + [stem_loop, gc_clamp, first_gc, second_gc, off_tgt]
    assert len(feats) == N_HANDCRAFTED, f"expected {N_HANDCRAFTED}, got {len(feats)}"
    return feats


def onehot_crispr(seq: str):
    """
    Return (4, 20) float32 numpy array for a 20-mer gRNA, or None if invalid.
    Row order: A=0, C=1, G=2, T=3.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    seq = seq.upper()
    if len(seq) != 20 or not all(c in "ACGT" for c in seq):
        return None
    base_idx = {"A": 0, "C": 1, "G": 2, "T": 3}
    oh = np.zeros((4, 20), dtype=np.float32)
    for i, b in enumerate(seq):
        oh[base_idx[b], i] = 1.0
    return oh


# ── Neural network modules ────────────────────────────────────────────────────

def _make_torch():
    """Lazy import; raises if torch not available (caller handles)."""
    import torch
    import torch.nn as nn
    return torch, nn


class ProteinBranch:
    """
    Linear protein/mRNA branch.  Instantiated as nn.Module via _build().
    Kept as a class so import works before torch is verified available.
    """
    @staticmethod
    def build():
        _, nn = _make_torch()

        class _Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.net = nn.Sequential(
                    nn.Linear(SMILES_FEAT_DIM + EMBED_DIM, 256),
                    nn.ReLU(),
                    nn.Dropout(0.3),
                    nn.Linear(256, 64),
                    nn.ReLU(),
                    nn.Linear(64, 2),   # [affinity, log_variance]
                )

            def forward(self, x):       # x: (B, 2072)
                return self.net(x)

        return _Net()


class MRNABranch:
    """Same architecture as ProteinBranch, separate weights."""
    @staticmethod
    def build():
        _, nn = _make_torch()

        class _Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.net = nn.Sequential(
                    nn.Linear(SMILES_FEAT_DIM + EMBED_DIM, 256),
                    nn.ReLU(),
                    nn.Dropout(0.3),
                    nn.Linear(256, 64),
                    nn.ReLU(),
                    nn.Linear(64, 2),
                )

            def forward(self, x):
                return self.net(x)

        return _Net()


class CRISPRBranch:
    """Conv1d sequence encoder + handcrafted features + target embed."""
    @staticmethod
    def build():
        _, nn = _make_torch()

        class _Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = nn.Sequential(
                    nn.Conv1d(4, 32, kernel_size=3, padding=1),
                    nn.ReLU(),
                    nn.Conv1d(32, 32, kernel_size=3, padding=1),
                    nn.ReLU(),
                )
                self.fusion = nn.Sequential(
                    nn.Linear(CRISPR_FUSION_DIM, 64),   # 73 → 64
                    nn.ReLU(),
                    nn.Dropout(0.3),
                    nn.Linear(64, 32),
                    nn.ReLU(),
                    nn.Linear(32, 2),   # [predicted_iptm, log_variance]
                )

            def forward(self, seq_oh, handcrafted, target_embed):
                # seq_oh:      (B, 4, 20)
                # handcrafted: (B, 25)
                # target_embed:(B, 16)
                conv_out  = self.conv(seq_oh)              # (B, 32, 20)
                pooled    = conv_out.mean(dim=2)           # (B, 32)
                combined  = _make_torch()[0].cat([pooled, handcrafted, target_embed], dim=1)  # (B, 73)
                return self.fusion(combined)

        return _Net()


class LifeBrainModel:
    """
    Full LIFE-BRAIN model.  Call .build() to get an nn.Module.

    The returned module has:
      .embedding   — shared nn.Embedding(4096, 16)
      .protein     — ProteinBranch module
      .mrna        — MRNABranch module
      .crispr      — CRISPRBranch module

    Forward routing is done externally in life_brain.py.
    """
    @staticmethod
    def build():
        torch, nn = _make_torch()

        protein_net = ProteinBranch.build()
        mrna_net    = MRNABranch.build()
        crispr_net  = CRISPRBranch.build()

        class _Model(nn.Module):
            def __init__(self):
                super().__init__()
                self.embedding = nn.Embedding(MAX_EMBED, EMBED_DIM)
                self.protein   = protein_net
                self.mrna      = mrna_net
                self.crispr    = crispr_net

            def get_embed(self, target_ids_tensor):
                """Clamp target_ids to [0, MAX_EMBED-1] before lookup."""
                clamped = target_ids_tensor.clamp(0, MAX_EMBED - 1)
                return self.embedding(clamped)

        m = _Model()
        # Ensure strictly CPU — belt-and-suspenders
        return m.to(torch.device("cpu"))

    @staticmethod
    def gaussian_nll(pred, y_true):
        """
        Gaussian NLL loss for a single branch.

        pred:   (B, 2) — [mu, log_var]
        y_true: (B,)
        """
        torch, _ = _make_torch()
        mu      = pred[:, 0]
        log_var = pred[:, 1]
        loss = 0.5 * (torch.exp(-log_var) * (y_true - mu).pow(2) + log_var)
        return loss.mean()
