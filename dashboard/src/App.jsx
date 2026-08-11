import { useState, useEffect, useRef } from 'react'

/* ─── Theme tokens ─────────────────────────────────────────── */
const T = {
  bg:         '#0a0a0a',
  surface:    '#111111',
  border:     '#1a2a1a',
  accent:     '#00ff88',
  accentDim:  '#00cc6a',
  accentGlow: 'rgba(0,255,136,0.12)',
  warn:       '#ff6b35',
  muted:      '#4a5568',
  text:       '#e2e8f0',
  textDim:    '#718096',
  purple:     '#a78bfa',
  purpleGlow: 'rgba(167,139,250,0.15)',
  gold:       '#ffe066',
  blue:       '#60a5fa',
  blueGlow:   'rgba(96,165,250,0.12)',
}

/* ─── Inline styles ────────────────────────────────────────── */
const S = {
  app: {
    minHeight:       '100vh',
    background:      T.bg,
    color:           T.text,
    fontFamily:      "'JetBrains Mono', 'Fira Mono', 'Cascadia Code', monospace",
    padding:         '0',
    margin:          '0',
  },
  header: {
    borderBottom:    `1px solid ${T.border}`,
    padding:         '24px 32px',
    background:      `linear-gradient(180deg, #0f1a0f 0%, ${T.bg} 100%)`,
    display:         'flex',
    flexDirection:   'column',
    alignItems:      'center',
    gap:             '8px',
    position:        'relative',
  },
  tagline: {
    fontSize:        '22px',
    fontWeight:      700,
    color:           T.accent,
    letterSpacing:   '0.04em',
    textShadow:      `0 0 24px ${T.accent}`,
  },
  subtitle: {
    fontSize:        '13px',
    color:           T.textDim,
    letterSpacing:   '0.12em',
    textTransform:   'uppercase',
  },
  statusDot: (alive) => ({
    position:        'absolute',
    top:             '24px',
    right:           '32px',
    display:         'flex',
    alignItems:      'center',
    gap:             '8px',
    fontSize:        '13px',
    color:           alive ? T.accent : T.warn,
  }),
  grid: {
    display:         'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
    gap:             '20px',
    padding:         '28px 32px',
    maxWidth:        '1400px',
    margin:          '0 auto',
  },
  sectionLabel: {
    gridColumn:      '1 / -1',
    fontSize:        '11px',
    letterSpacing:   '0.18em',
    textTransform:   'uppercase',
    color:           T.textDim,
    paddingBottom:   '4px',
    borderBottom:    `1px solid ${T.border}`,
    marginTop:       '8px',
  },
  panel: {
    background:      T.surface,
    border:          `1px solid ${T.border}`,
    borderRadius:    '12px',
    padding:         '28px',
    position:        'relative',
    overflow:        'hidden',
    transition:      'border-color 0.3s',
  },
  panelGlow: {
    position:        'absolute',
    top:             0,
    left:            0,
    right:           0,
    height:          '2px',
    background:      `linear-gradient(90deg, transparent, ${T.accent}, transparent)`,
  },
  panelGlowPurple: {
    position:        'absolute',
    top:             0,
    left:            0,
    right:           0,
    height:          '2px',
    background:      `linear-gradient(90deg, transparent, ${T.purple}, transparent)`,
  },
  panelGlowBlue: {
    position:        'absolute',
    top:             0,
    left:            0,
    right:           0,
    height:          '2px',
    background:      `linear-gradient(90deg, transparent, ${T.blue}, transparent)`,
  },
  panelTitle: {
    fontSize:        '11px',
    color:           T.textDim,
    letterSpacing:   '0.14em',
    textTransform:   'uppercase',
    marginBottom:    '16px',
    display:         'flex',
    alignItems:      'center',
    gap:             '8px',
  },
  bigNumber: {
    fontSize:        '52px',
    fontWeight:      700,
    color:           T.accent,
    lineHeight:      1,
    textShadow:      `0 0 32px ${T.accentGlow}`,
    marginBottom:    '8px',
    fontVariantNumeric: 'tabular-nums',
  },
  label: {
    fontSize:        '13px',
    color:           T.textDim,
  },
  targetList: {
    listStyle:       'none',
    padding:         0,
    margin:          0,
    display:         'flex',
    flexDirection:   'column',
    gap:             '10px',
  },
  targetItem: {
    display:         'flex',
    alignItems:      'center',
    gap:             '12px',
    padding:         '10px 14px',
    background:      '#0d1a0d',
    borderRadius:    '8px',
    border:          `1px solid ${T.border}`,
    fontSize:        '13px',
    animation:       'fadeIn 0.4s ease',
  },
  targetDot: {
    width:           '8px',
    height:          '8px',
    borderRadius:    '50%',
    background:      T.accent,
    boxShadow:       `0 0 6px ${T.accent}`,
    flexShrink:      0,
  },
  globalRow: {
    display:         'flex',
    justifyContent:  'space-between',
    alignItems:      'center',
    padding:         '14px 0',
    borderBottom:    `1px solid ${T.border}`,
  },
  globalLabel: {
    fontSize:        '13px',
    color:           T.textDim,
  },
  globalValue: {
    fontSize:        '16px',
    fontWeight:      600,
    color:           T.accent,
    fontVariantNumeric: 'tabular-nums',
  },
  submissionList: {
    marginTop:       '20px',
    display:         'flex',
    flexDirection:   'column',
    gap:             '6px',
    maxHeight:       '200px',
    overflowY:       'auto',
  },
  submissionItem: {
    display:         'flex',
    justifyContent:  'space-between',
    fontSize:        '11px',
    color:           T.textDim,
    padding:         '5px 0',
    borderBottom:    `1px solid #111`,
  },
  footer: {
    borderTop:       `1px solid ${T.border}`,
    padding:         '16px 32px',
    display:         'flex',
    justifyContent:  'space-between',
    alignItems:      'center',
    fontSize:        '12px',
    color:           T.muted,
  },
  pulseDot: {
    width:           '8px',
    height:          '8px',
    borderRadius:    '50%',
    background:      T.accent,
    display:         'inline-block',
    marginRight:     '6px',
    animation:       'pulse 2s infinite',
  },
  /* ── Adaptive panel micro-styles ── */
  kv: {
    display:         'flex',
    justifyContent:  'space-between',
    alignItems:      'center',
    padding:         '9px 0',
    borderBottom:    `1px solid #161616`,
    fontSize:        '13px',
  },
  kvLast: {
    display:         'flex',
    justifyContent:  'space-between',
    alignItems:      'center',
    padding:         '9px 0',
    fontSize:        '13px',
  },
  pill: (color) => ({
    background:      color + '22',
    border:          `1px solid ${color}55`,
    color:           color,
    borderRadius:    '4px',
    padding:         '2px 8px',
    fontSize:        '11px',
    fontWeight:      600,
  }),
  progressBar: (pct, color) => ({
    height:          '6px',
    borderRadius:    '3px',
    background:      '#1a1a1a',
    overflow:        'hidden',
    position:        'relative',
    marginTop:       '6px',
  }),
  progressFill: (pct, color) => ({
    position:        'absolute',
    top:             0, left: 0,
    height:          '100%',
    width:           pct + '%',
    background:      color,
    borderRadius:    '3px',
    transition:      'width 0.8s ease',
    boxShadow:       `0 0 8px ${color}88`,
  }),
  familyBar: {
    display:         'flex',
    gap:             '6px',
    flexWrap:        'wrap',
    marginTop:       '10px',
  },
  scoreRow: {
    display:         'grid',
    gridTemplateColumns: '1fr 80px 60px 70px',
    gap:             '8px',
    padding:         '6px 0',
    borderBottom:    `1px solid #161616`,
    fontSize:        '11px',
    alignItems:      'center',
  },
}

/* ─── Animated counter hook ────────────────────────────────── */
function useAnimatedNumber(target, duration = 800) {
  const [display, setDisplay] = useState(target)
  const prevRef = useRef(target)
  const rafRef  = useRef(null)

  useEffect(() => {
    const start = prevRef.current
    const diff  = target - start
    if (diff === 0) return
    const startTime = performance.now()

    const step = (now) => {
      const t = Math.min((now - startTime) / duration, 1)
      const eased = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t
      setDisplay(Math.round(start + diff * eased))
      if (t < 1) rafRef.current = requestAnimationFrame(step)
      else { prevRef.current = target; setDisplay(target) }
    }
    rafRef.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(rafRef.current)
  }, [target, duration])

  return display
}

/* ─── Panel icon map ───────────────────────────────────────── */
const ICONS = {
  molecules: '🔬',
  life:      '✦',
  targets:   '🧬',
  network:   '🌐',
  pulse:     '⚡',
  art:       '🧠',
  scout:     '🎯',
  scoring:   '📊',
}

/* ─── EXISTING PANELS (unchanged) ─────────────────────────── */

function MoleculesPanel({ count }) {
  const animated = useAnimatedNumber(count)
  return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.molecules}</span> Molecules Screened
      </div>
      <div style={S.bigNumber}>{animated.toLocaleString()}</div>
      <div style={S.label}>candidate drug molecules evaluated this session</div>
    </div>
  )
}

function LifeEarnedPanel({ earned }) {
  const animated = useAnimatedNumber(Math.floor(earned))
  return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.life}</span> $LIFE Earned
      </div>
      <div style={{ ...S.bigNumber, color: '#ffe066', textShadow: '0 0 32px rgba(255,224,102,0.2)' }}>
        {animated.toLocaleString()}
        <span style={{ fontSize: '22px', marginLeft: '8px', color: '#b8a040' }}>LIFE</span>
      </div>
      <div style={S.label}>1 $LIFE minted per verified on-chain submission</div>
    </div>
  )
}

function TargetsPanel({ targets }) {
  return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.targets}</span> Cancer Targets Contributed To
        <span style={{ marginLeft: 'auto', color: T.accent, fontSize: '16px' }}>
          {targets.length}
        </span>
      </div>
      {targets.length === 0 ? (
        <div style={S.label}>Starting first screening cycle…</div>
      ) : (
        <ul style={S.targetList}>
          {targets.map((t, i) => (
            <li key={i} style={S.targetItem}>
              <div style={S.targetDot} />
              <span>{t}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function NetworkPanel({ global: g, recent }) {
  return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.network}</span> Global Network Stats
        <span style={{ marginLeft: 'auto', fontSize: '10px', color: T.textDim }}>mock data</span>
      </div>

      <div style={S.globalRow}>
        <span style={S.globalLabel}>Total Miners Online</span>
        <span style={S.globalValue}>{(g?.total_miners ?? 0).toLocaleString()}</span>
      </div>
      <div style={S.globalRow}>
        <span style={S.globalLabel}>Global Molecules Screened</span>
        <span style={S.globalValue}>{(g?.total_molecules_screened ?? 0).toLocaleString()}</span>
      </div>
      <div style={{ ...S.globalRow, borderBottom: 'none' }}>
        <span style={S.globalLabel}>Targets Solved (candidates found)</span>
        <span style={S.globalValue}>{g?.targets_solved ?? 0}</span>
      </div>

      {recent && recent.length > 0 && (
        <>
          <div style={{ ...S.panelTitle, marginTop: '20px' }}>Recent Submissions</div>
          <div style={S.submissionList}>
            {[...recent].reverse().map((s, i) => (
              <div key={i} style={S.submissionItem}>
                <span style={{ color: T.accentDim }}>{s.target}</span>
                <span>{s.binding_score?.toFixed(3)} kcal/mol</span>
                <span style={{ color: T.muted }}>{s.ts?.slice(11, 19)}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/* ─── NEW: LIFE PULSE panel ─────────────────────────────────── */
const FAMILY_COLORS = {
  kinase:           T.purple,
  cytokine:         '#f472b6',
  protease:         '#fb923c',
  nuclear_receptor: T.gold,
  general:          T.blue,
}

function PulsePanel({ pulse }) {
  if (!pulse) return null
  const { total_evaluated = 0, top_proxy_score = 0, family_counts = {}, recent = [] } = pulse
  return (
    <div style={{ ...S.panel, border: `1px solid ${T.purple}33` }}>
      <div style={S.panelGlowPurple} />
      <div style={S.panelTitle}>
        <span>{ICONS.pulse}</span> LIFE PULSE
        <span style={{ marginLeft: 'auto', ...S.pill(T.purple) }}>SOBOL</span>
      </div>

      <div style={{ display: 'flex', gap: '32px', marginBottom: '16px' }}>
        <div>
          <div style={{ fontSize: '36px', fontWeight: 700, color: T.purple, lineHeight: 1,
                        textShadow: `0 0 24px ${T.purpleGlow}` }}>
            {total_evaluated.toLocaleString()}
          </div>
          <div style={S.label}>molecules swept</div>
        </div>
        <div>
          <div style={{ fontSize: '36px', fontWeight: 700, color: T.accentDim, lineHeight: 1 }}>
            {top_proxy_score.toFixed(3)}
          </div>
          <div style={S.label}>best proxy score</div>
        </div>
      </div>

      {/* Family distribution pills */}
      <div style={{ ...S.panelTitle, marginBottom: '8px' }}>Family Distribution</div>
      <div style={S.familyBar}>
        {Object.entries(family_counts).map(([fam, cnt]) => (
          <span key={fam} style={S.pill(FAMILY_COLORS[fam] || T.textDim)}>
            {fam.replace('_', ' ')} {cnt}
          </span>
        ))}
        {Object.keys(family_counts).length === 0 &&
          <span style={{ color: T.textDim, fontSize: '12px' }}>Sweep starting…</span>}
      </div>

      {/* Recent molecules */}
      {recent.length > 0 && (
        <>
          <div style={{ ...S.panelTitle, marginTop: '16px', marginBottom: '8px' }}>Recent Molecules</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '140px', overflowY: 'auto' }}>
            {recent.map((r, i) => (
              <div key={i} style={{ display: 'flex', gap: '8px', alignItems: 'center',
                                    padding: '4px 0', borderBottom: '1px solid #161616', fontSize: '11px' }}>
                <span style={S.pill(FAMILY_COLORS[r.family] || T.muted)}>{r.family?.slice(0, 4)}</span>
                <span style={{ color: T.textDim, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
                               whiteSpace: 'nowrap' }}>{r.smiles}</span>
                <span style={{ color: T.accentDim, flexShrink: 0 }}>{r.proxy_score?.toFixed(3)}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/* ─── NEW: LIFE ART panel ───────────────────────────────────── */
function ArtPanel({ art }) {
  if (!art) return null
  const {
    ready = false,
    n_rows = 0,
    r2 = null,
    reason = '—',
    boltz_accumulated = 0,
    retrain_progress = 0,
    next_retrain_in = 50,
    n_features = 525,
  } = art

  return (
    <div style={{ ...S.panel, border: `1px solid ${T.blue}33` }}>
      <div style={S.panelGlowBlue} />
      <div style={S.panelTitle}>
        <span>{ICONS.art}</span> LIFE ART
        <span style={{ marginLeft: 'auto', ...S.pill(ready ? T.accent : T.warn) }}>
          {ready ? 'MODEL LIVE' : 'TRAINING'}
        </span>
      </div>

      <div style={S.kv}>
        <span style={S.globalLabel}>Model Ready</span>
        <span style={{ color: ready ? T.accent : T.warn, fontWeight: 600 }}>
          {ready ? '✓ Deployed' : '✗ Awaiting data'}
        </span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Training Rows (Boltz2 scored)</span>
        <span style={{ color: T.text, fontWeight: 600 }}>{n_rows} / 50</span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>5-fold CV R²</span>
        <span style={{ color: r2 !== null ? (r2 >= 0.25 ? T.accent : T.warn) : T.muted }}>
          {r2 !== null ? r2.toFixed(3) : '—'}
        </span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Feature Dimensions</span>
        <span style={{ color: T.blue }}>
          512-bit Morgan FP + 13 phys-chem = {n_features}
        </span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Total Boltz2 Scores</span>
        <span style={{ color: T.text }}>{boltz_accumulated}</span>
      </div>
      <div style={{ ...S.kvLast, flexDirection: 'column', alignItems: 'stretch' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
          <span style={S.globalLabel}>Next retrain in</span>
          <span style={{ color: T.purple, fontSize: '13px' }}>{next_retrain_in} scores</span>
        </div>
        <div style={S.progressBar(retrain_progress, T.purple)}>
          <div style={S.progressFill(retrain_progress, T.purple)} />
        </div>
      </div>

      {!ready && (
        <div style={{ marginTop: '14px', padding: '10px 14px', background: '#0d0d1a',
                      borderRadius: '8px', border: `1px solid ${T.blue}22`, fontSize: '12px',
                      color: T.textDim, lineHeight: 1.5 }}>
          {reason}. ART auto-deploys when n ≥ 50 scored molecules and 5-fold R² ≥ 0.25.
        </div>
      )}
    </div>
  )
}

/* ─── NEW: LIFE SCOUT panel ─────────────────────────────────── */
const PHASE_COLORS = { explore: T.blue, exploit: T.accent, refine: T.purple }

function ScoutPanel({ scout, adaptive }) {
  if (!scout) return null
  const {
    last_family = '—',
    last_phase  = '—',
    n_diverse   = 0,
    n_passed_filter = 0,
    best_score  = null,
    target_id   = '—',
    ts          = null,
  } = scout

  const phaseColor = PHASE_COLORS[last_phase] || T.textDim
  const artReady   = adaptive?.art?.ready ?? false

  return (
    <div style={{ ...S.panel, border: `1px solid ${T.accent}22` }}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.scout}</span> LIFE SCOUT
        {last_phase !== '—' && (
          <span style={{ ...S.pill(phaseColor), marginLeft: 'auto' }}>
            {last_phase.toUpperCase()}
          </span>
        )}
      </div>

      <div style={S.kv}>
        <span style={S.globalLabel}>Protein Family</span>
        <span style={{ color: FAMILY_COLORS[last_family] || T.text, fontWeight: 600 }}>
          {last_family.replace('_', ' ')}
        </span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Target</span>
        <span style={{ color: T.accentDim }}>{target_id}</span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Passed Family Filter</span>
        <span style={{ color: T.text }}>{n_passed_filter}</span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Diverse Candidates Returned</span>
        <span style={{ color: T.accent, fontWeight: 600 }}>{n_diverse}</span>
      </div>
      <div style={S.kv}>
        <span style={S.globalLabel}>Best Predicted Score</span>
        <span style={{ color: best_score !== null ? T.accent : T.muted }}>
          {best_score !== null ? best_score.toFixed(4) : '—'}
        </span>
      </div>
      <div style={{ ...S.kvLast, gap: '8px', flexWrap: 'wrap' }}>
        <span style={S.globalLabel}>Epoch Phase</span>
        <div style={{ display: 'flex', gap: '6px', marginLeft: 'auto' }}>
          {['explore', 'exploit', 'refine'].map(p => (
            <span key={p} style={{
              ...S.pill(PHASE_COLORS[p]),
              opacity: last_phase === p ? 1 : 0.3,
              fontWeight: last_phase === p ? 700 : 400,
            }}>{p}</span>
          ))}
        </div>
      </div>

      <div style={{ marginTop: '14px', padding: '10px 14px', background: '#0d130d',
                    borderRadius: '8px', border: `1px solid ${T.border}`, fontSize: '12px',
                    color: T.textDim, lineHeight: 1.6 }}>
        <span style={{ color: T.accent }}>Ranking:</span>{' '}
        {artReady ? 'ART model (Morgan FP + physchem RF)' : 'Proxy scorer (ha + logP)'}
        {' · '}
        <span style={{ color: T.accent }}>Filter:</span>{' '}
        {last_family !== '—' ? `${last_family.replace('_', ' ')} pharmacophore` : 'Lipinski Ro5'}
        {' · '}
        <span style={{ color: T.accent }}>Diversity:</span> Tanimoto ≥ 0.65 rejected
      </div>
    </div>
  )
}

/* ─── NEW: Scoring History panel ────────────────────────────── */
function ScoringHistoryPanel({ history }) {
  if (!history || history.length === 0) return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}><span>{ICONS.scoring}</span> Boltz2 Scoring History</div>
      <div style={S.label}>No Boltz2 scores yet — miner is accumulating data…</div>
    </div>
  )

  const scores = history.filter(r => r.boltz_score !== null).map(r => r.boltz_score)
  const best   = scores.length ? Math.max(...scores) : null
  const avg    = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null

  return (
    <div style={S.panel}>
      <div style={S.panelGlow} />
      <div style={S.panelTitle}>
        <span>{ICONS.scoring}</span> Boltz2 Scoring History
        <span style={{ marginLeft: 'auto', color: T.accent }}>{history.length} records</span>
      </div>

      {/* Summary row */}
      <div style={{ display: 'flex', gap: '24px', marginBottom: '16px' }}>
        <div>
          <div style={{ fontSize: '28px', fontWeight: 700, color: T.accent, lineHeight: 1 }}>
            {best !== null ? best.toFixed(4) : '—'}
          </div>
          <div style={S.label}>best boltz score</div>
        </div>
        <div>
          <div style={{ fontSize: '28px', fontWeight: 700, color: T.accentDim, lineHeight: 1 }}>
            {avg !== null ? avg.toFixed(4) : '—'}
          </div>
          <div style={S.label}>session avg</div>
        </div>
      </div>

      {/* Column header */}
      <div style={{ ...S.scoreRow, color: T.muted, borderBottom: `1px solid ${T.border}`,
                    paddingBottom: '6px', marginBottom: '2px' }}>
        <span>SMILES</span><span>Score</span><span>Target</span><span>Time</span>
      </div>

      {/* Rows */}
      <div style={{ maxHeight: '200px', overflowY: 'auto' }}>
        {history.map((r, i) => (
          <div key={i} style={S.scoreRow}>
            <span style={{ color: T.textDim, overflow: 'hidden', textOverflow: 'ellipsis',
                           whiteSpace: 'nowrap' }}>{r.smiles || '—'}</span>
            <span style={{ color: r.boltz_score !== null ? T.accent : T.muted,
                           fontWeight: 600 }}>
              {r.boltz_score !== null ? r.boltz_score.toFixed(4) : '—'}
            </span>
            <span style={{ color: T.accentDim }}>{r.target_id || '?'}</span>
            <span style={{ color: T.muted }}>{r.ts ? r.ts.slice(11, 19) : '—'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ─── Global CSS ───────────────────────────────────────────── */
const CSS = `
  * { box-sizing: border-box; }
  body { margin: 0; background: #0a0a0a; }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(0,255,136,0.4); }
    50%       { opacity: 0.6; box-shadow: 0 0 0 6px rgba(0,255,136,0); }
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: #111; }
  ::-webkit-scrollbar-thumb { background: #1a2a1a; border-radius: 2px; }
`

/* ─── App ──────────────────────────────────────────────────── */
export default function App() {
  const [stats,    setStats]    = useState(null)
  const [adaptive, setAdaptive] = useState(null)
  const [lastPoll, setLastPoll] = useState(null)

  useEffect(() => {
    async function poll() {
      try {
        const [sRes, aRes] = await Promise.all([
          fetch('/stats.json?'    + Date.now()),
          fetch('/adaptive.json?' + Date.now()),
        ])
        if (sRes.ok) setStats(await sRes.json())
        if (aRes.ok) setAdaptive(await aRes.json())
        setLastPoll(new Date())
      } catch { /* daemon not running yet */ }
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const alive  = stats?.alive             ?? false
  const mols   = stats?.molecules_screened ?? 0
  const life   = stats?.life_earned        ?? 0
  const tgts   = stats?.targets_contributed ?? []
  const glob   = stats?.global_mock        ?? stats?.global ?? {}
  const recent = stats?.recent_submissions ?? []

  return (
    <>
      <style>{CSS}</style>
      <div style={S.app}>
        {/* Header */}
        <header style={S.header}>
          <div style={S.tagline}>✦ Your GPU could help cure cancer. Earn $LIFE tokens. ✦</div>
          <div style={S.subtitle}>LIFE Compute — Decentralized Drug Discovery Network</div>
          <div style={S.statusDot(alive)}>
            <span style={{ ...S.pulseDot, background: alive ? T.accent : T.warn }} />
            {alive ? 'Miner running' : 'Waiting for daemon…'}
          </div>
        </header>

        <div style={S.grid}>
          {/* ── Existing panels ── */}
          <div style={S.sectionLabel}>MINER STATUS</div>
          <MoleculesPanel count={mols} />
          <LifeEarnedPanel earned={life} />
          <TargetsPanel targets={tgts} />
          <NetworkPanel global={glob} recent={recent} />

          {/* ── Adaptive AI panels ── */}
          <div style={S.sectionLabel}>ADAPTIVE AI — LIFE PULSE · ART · SCOUT</div>
          <PulsePanel        pulse={adaptive?.pulse} />
          <ArtPanel          art={adaptive?.art} adaptive={adaptive} />
          <ScoutPanel        scout={adaptive?.scout} adaptive={adaptive} />
          <ScoringHistoryPanel history={adaptive?.scoring_history} />
        </div>

        {/* Footer */}
        <footer style={S.footer}>
          <span>LIFE Compute Miner v1.0.0</span>
          <span>
            {lastPoll
              ? `Last updated: ${lastPoll.toLocaleTimeString()}`
              : 'Connecting to daemon…'}
          </span>
          <span>Program: 3dYbT2…xYiC</span>
        </footer>
      </div>
    </>
  )
}
