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
}

/* ─── Molecules panel ──────────────────────────────────────── */
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

/* ─── LIFE Earned panel ────────────────────────────────────── */
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

/* ─── Cancer Targets panel ─────────────────────────────────── */
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

/* ─── Global Network panel ─────────────────────────────────── */
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
  const [stats, setStats] = useState(null)
  const [lastPoll, setLastPoll] = useState(null)

  useEffect(() => {
    async function poll() {
      try {
        const res  = await fetch('/stats.json?' + Date.now())
        const data = await res.json()
        setStats(data)
        setLastPoll(new Date())
      } catch {
        /* daemon not running yet — keep showing last state */
      }
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const alive = stats?.alive ?? false
  const mols  = stats?.molecules_screened ?? 0
  const life  = stats?.life_earned        ?? 0
  const tgts  = stats?.targets_contributed ?? []
  const glob  = stats?.global_mock        ?? {}
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

        {/* Stats grid */}
        <div style={S.grid}>
          <MoleculesPanel count={mols} />
          <LifeEarnedPanel earned={life} />
          <TargetsPanel targets={tgts} />
          <NetworkPanel global={glob} recent={recent} />
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
