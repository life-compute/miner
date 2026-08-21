import { useState, useEffect, useRef, useCallback } from 'react'

/* ─── Biopunk theme tokens ──────────────────────────────────── */
const T = {
  bg:            '#050a05',
  surface:       '#080f08',
  surfaceAlt:    '#0a140a',
  border:        '#00ff4133',
  borderBright:  '#00ff4166',
  green:         '#00ff41',
  greenDim:      '#00cc33',
  greenGlow:     'rgba(0,255,65,0.15)',
  greenGlowStr:  'rgba(0,255,65,0.35)',
  cyan:          '#00ffff',
  cyanDim:       '#00cccc',
  cyanGlow:      'rgba(0,255,255,0.12)',
  purple:        '#9d00ff',
  purpleDim:     '#7700cc',
  purpleGlow:    'rgba(157,0,255,0.15)',
  red:           '#ff003c',
  amber:         '#ff8c00',
  amberDim:      '#cc6600',
  redGlow:       'rgba(255,0,60,0.2)',
  muted:         '#1a3a1a',
  textDim:       '#5a9a5a',
  text:          '#aaddaa',
  textBright:    '#ccffcc',
  mono:          "'Courier New', 'Source Code Pro', 'Lucida Console', monospace",
}

/* ─── Reusable glow box-shadow ──────────────────────────────── */
const glow   = (c, s = 8)  => `0 0 ${s}px ${c}, 0 0 ${s*2}px ${c}44`
const panelShadow = (c)     => `0 0 1px ${c}, 0 0 20px ${c}22, inset 0 0 40px ${c}05`

/* ─── Inline styles ─────────────────────────────────────────── */
const S = {
  wrap: {
    minHeight:   '100vh',
    background:  T.bg,
    color:       T.text,
    fontFamily:  T.mono,
    position:    'relative',
    overflow:    'hidden',
  },
  scanlines: {
    position:    'fixed',
    top: 0, left: 0, right: 0, bottom: 0,
    pointerEvents: 'none',
    zIndex:      1000,
    background:  'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.08) 2px, rgba(0,0,0,0.08) 4px)',
  },
  matrixCanvas: {
    position:    'fixed',
    top: 0, left: 0,
    width:       '100%',
    height:      '100%',
    opacity:     0.06,
    pointerEvents: 'none',
    zIndex:      0,
  },
  content: {
    position:    'relative',
    zIndex:      1,
  },
  header: {
    borderBottom:  `1px solid ${T.border}`,
    padding:       '0',
    background:    `linear-gradient(180deg, #020802 0%, ${T.bg} 100%)`,
    position:      'relative',
    overflow:      'hidden',
  },
  headerInner: {
    padding:       '28px 32px 24px',
    display:       'flex',
    flexDirection: 'column',
    alignItems:    'center',
    gap:           '10px',
    position:      'relative',
    zIndex:        2,
  },
  tagline: {
    fontSize:      '26px',
    fontWeight:    700,
    color:         T.green,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    textShadow:    `0 0 20px ${T.green}, 0 0 40px ${T.green}88, 0 0 80px ${T.green}44`,
    textAlign:     'center',
    animation:     'textPulse 3s ease-in-out infinite',
  },
  subtitle: {
    fontSize:      '11px',
    color:         T.cyan,
    letterSpacing: '0.2em',
    textTransform: 'uppercase',
    textShadow:    glow(T.cyan, 4),
  },
  statusBadge: (alive) => ({
    position:      'absolute',
    top:           '24px',
    right:         '32px',
    display:       'flex',
    alignItems:    'center',
    gap:           '8px',
    fontSize:      '11px',
    color:         alive ? T.green : T.red,
    letterSpacing: '0.1em',
    textShadow:    glow(alive ? T.green : T.red, 4),
    border:        `1px solid ${alive ? T.green : T.red}55`,
    padding:       '4px 12px',
    borderRadius:  '2px',
    background:    alive ? '#00ff4108' : '#ff003c08',
  }),
  statusDot: (alive) => ({
    width:         '6px',
    height:        '6px',
    borderRadius:  '50%',
    background:    alive ? T.green : T.red,
    boxShadow:     glow(alive ? T.green : T.red, 4),
    animation:     alive ? 'blink 1.2s step-end infinite' : 'none',
  }),
  grid: {
    display:       'grid',
    gridTemplateColumns: 'repeat(4, 1fr)',
    gap:           '16px',
    padding:       '24px 28px',
    maxWidth:      '1440px',
    margin:        '0 auto',
  },
  sectionLabel: {
    gridColumn:    '1 / -1',
    fontSize:      '10px',
    letterSpacing: '0.22em',
    textTransform: 'uppercase',
    color:         T.textDim,
    paddingBottom: '6px',
    borderBottom:  `1px solid ${T.border}`,
    marginTop:     '8px',
    display:       'flex',
    alignItems:    'center',
    gap:           '10px',
  },
  sectionTick: {
    width:         '6px',
    height:        '6px',
    background:    T.green,
    boxShadow:     glow(T.green, 3),
  },
  panel: (accent = T.green) => ({
    background:    T.surface,
    border:        `1px solid ${accent}33`,
    borderRadius:  '3px',
    padding:       '22px',
    position:      'relative',
    overflow:      'hidden',
    boxShadow:     panelShadow(accent),
    transition:    'border-color 0.4s, box-shadow 0.4s',
  }),
  panelBar: (accent) => ({
    position:      'absolute',
    top: 0, left: 0, right: 0,
    height:        '1px',
    background:    `linear-gradient(90deg, transparent, ${accent}, transparent)`,
    boxShadow:     `0 0 8px ${accent}`,
  }),
  panelCorner: (pos, accent) => ({
    position:      'absolute',
    [pos.includes('t') ? 'top' : 'bottom']: 0,
    [pos.includes('l') ? 'left' : 'right']: 0,
    width:         '12px',
    height:        '12px',
    borderTop:     pos.includes('t') ? `1px solid ${accent}` : 'none',
    borderBottom:  pos.includes('b') ? `1px solid ${accent}` : 'none',
    borderLeft:    pos.includes('l') ? `1px solid ${accent}` : 'none',
    borderRight:   pos.includes('r') ? `1px solid ${accent}` : 'none',
  }),
  panelTitle: {
    fontSize:      '10px',
    color:         T.textDim,
    letterSpacing: '0.2em',
    textTransform: 'uppercase',
    marginBottom:  '18px',
    display:       'flex',
    alignItems:    'center',
    gap:           '8px',
  },
  titleAccent: (c) => ({
    color:         c,
    textShadow:    glow(c, 3),
  }),
  bigNum: (c = T.green) => ({
    fontSize:      '56px',
    fontWeight:    700,
    color:         c,
    lineHeight:    1,
    textShadow:    `0 0 20px ${c}, 0 0 40px ${c}66`,
    marginBottom:  '6px',
    fontVariantNumeric: 'tabular-nums',
    letterSpacing: '-0.02em',
    animation:     'textPulse 4s ease-in-out infinite',
  }),
  label: {
    fontSize:      '11px',
    color:         T.textDim,
    letterSpacing: '0.06em',
  },
  kv: {
    display:       'flex',
    justifyContent:'space-between',
    alignItems:    'center',
    padding:       '8px 0',
    borderBottom:  `1px solid ${T.border}`,
    fontSize:      '12px',
  },
  kvLast: {
    display:       'flex',
    justifyContent:'space-between',
    alignItems:    'center',
    padding:       '8px 0',
    fontSize:      '12px',
  },
  globalRow: {
    display:       'flex',
    justifyContent:'space-between',
    alignItems:    'center',
    padding:       '12px 0',
    borderBottom:  `1px solid ${T.border}`,
  },
  progressTrack: {
    height:        '4px',
    background:    '#0a1a0a',
    border:        `1px solid ${T.border}`,
    borderRadius:  '0',
    overflow:      'hidden',
    position:      'relative',
    marginTop:     '6px',
  },
  terminalLine: {
    display:       'flex',
    gap:           '8px',
    alignItems:    'center',
    padding:       '5px 0',
    borderBottom:  `1px solid #0a150a`,
    fontSize:      '11px',
  },
  prompt: {
    color:         T.greenDim,
    flexShrink:    0,
    userSelect:    'none',
  },
  smiles: {
    color:         T.cyan,
    textShadow:    glow(T.cyan, 2),
    overflow:      'hidden',
    textOverflow:  'ellipsis',
    whiteSpace:    'nowrap',
    flex:          1,
    fontSize:      '10px',
    fontFamily:    T.mono,
  },
  pill: (c) => ({
    background:    c + '15',
    border:        `1px solid ${c}44`,
    color:         c,
    textShadow:    glow(c, 2),
    borderRadius:  '2px',
    padding:       '2px 7px',
    fontSize:      '10px',
    fontWeight:    700,
    letterSpacing: '0.1em',
    fontFamily:    T.mono,
  }),
  targetItem: {
    display:       'flex',
    alignItems:    'center',
    gap:           '12px',
    padding:       '10px 14px',
    background:    '#020902',
    border:        `1px solid ${T.border}`,
    borderRadius:  '2px',
    fontSize:      '12px',
    marginBottom:  '8px',
    transition:    'border-color 0.3s',
  },
  targetPip: {
    width:         '6px',
    height:        '6px',
    background:    T.green,
    boxShadow:     glow(T.green, 4),
    flexShrink:    0,
    animation:     'blink 2s step-end infinite',
  },
  footer: {
    borderTop:     `1px solid ${T.border}`,
    padding:       '12px 28px',
    display:       'flex',
    justifyContent:'space-between',
    alignItems:    'center',
    fontSize:      '10px',
    color:         T.textDim,
    letterSpacing: '0.1em',
  },
}

/* ─── Matrix rain canvas ────────────────────────────────────── */
function MatrixRain() {
  const ref = useRef()
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const chars = 'ATCGAUCGTAGCTAGCATCGAUCGATCG01アイウエオカキクケコ'
    let w, h, cols, drops
    function resize() {
      w = canvas.width  = window.innerWidth
      h = canvas.height = window.innerHeight
      cols = Math.floor(w / 16)
      drops = Array(cols).fill(0).map(() => Math.random() * -50)
    }
    resize()
    window.addEventListener('resize', resize)
    const tick = () => {
      ctx.fillStyle = 'rgba(5,10,5,0.12)'
      ctx.fillRect(0, 0, w, h)
      ctx.fillStyle = '#00ff41'
      ctx.font = '13px "Courier New", monospace'
      drops.forEach((y, i) => {
        const ch = chars[Math.floor(Math.random() * chars.length)]
        ctx.fillStyle = y * 16 < 80 ? '#00ffff' : '#00ff41'
        ctx.fillText(ch, i * 16, y * 16)
        if (y * 16 > h && Math.random() > 0.975) drops[i] = 0
        else drops[i] += 0.4
      })
    }
    const id = setInterval(tick, 50)
    return () => { clearInterval(id); window.removeEventListener('resize', resize) }
  }, [])
  return <canvas ref={ref} style={S.matrixCanvas} />
}

/* ─── DNA Helix SVG ─────────────────────────────────────────── */
function DNAHelix() {
  const pts = 14
  return (
    <svg width="320" height="64" viewBox="0 0 320 64" style={{ opacity: 0.7 }}>
      <defs>
        <filter id="gf">
          <feGaussianBlur stdDeviation="1.5" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      {/* top strand */}
      <path
        d={`M 0 32 ${Array.from({length:pts}, (_,i)=>{
          const x = (i/pts)*320
          const y = 32 + Math.sin((i/pts)*Math.PI*2)*20
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.green} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
      {/* bottom strand */}
      <path
        d={`M 0 32 ${Array.from({length:pts}, (_,i)=>{
          const x = (i/pts)*320
          const y = 32 - Math.sin((i/pts)*Math.PI*2)*20
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.cyan} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
      {/* rungs */}
      {Array.from({length: pts}, (_, i) => {
        const x = (i / pts) * 320 + 12
        const y1 = 32 + Math.sin((i / pts) * Math.PI * 2) * 20
        const y2 = 32 - Math.sin((i / pts) * Math.PI * 2) * 20
        const t = Math.abs(Math.sin((i / pts) * Math.PI * 2))
        return (
          <line key={i} x1={x} y1={y1} x2={x} y2={y2}
            stroke={t > 0.5 ? T.purple : T.green} strokeWidth="1" opacity={0.6 + t*0.4}
            filter="url(#gf)"
          />
        )
      })}
    </svg>
  )
}

/* ─── Uptime counter ────────────────────────────────────────── */
function useUptime(lastUpdated) {
  const [uptime, setUptime] = useState(0)
  useEffect(() => {
    const tick = () => {
      if (!lastUpdated) return
      const start = new Date(lastUpdated).getTime() - 60000 // approx daemon start
      setUptime(Math.max(0, Math.floor((Date.now() - start) / 1000)))
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [lastUpdated])
  const h = Math.floor(uptime / 3600), m = Math.floor((uptime % 3600) / 60), s = uptime % 60
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
}

/* ─── Animated counter ──────────────────────────────────────── */
function useAnimatedNumber(target, dur = 800) {
  const [disp, setDisp] = useState(target)
  const prev = useRef(target)
  const raf  = useRef(null)
  useEffect(() => {
    const from = prev.current, diff = target - from
    if (!diff) return
    const t0 = performance.now()
    const step = (now) => {
      const t = Math.min((now - t0) / dur, 1)
      const e = t < 0.5 ? 2*t*t : -1+(4-2*t)*t
      setDisp(Math.round(from + diff * e))
      if (t < 1) raf.current = requestAnimationFrame(step)
      else { prev.current = target; setDisp(target) }
    }
    raf.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf.current)
  }, [target, dur])
  return disp
}

/* ─── Panel wrapper ─────────────────────────────────────────── */
function Panel({ accent = T.green, style, children }) {
  return (
    <div style={{ ...S.panel(accent), ...style }}>
      <div style={S.panelBar(accent)} />
      <div style={S.panelCorner('tl', accent)} />
      <div style={S.panelCorner('br', accent)} />
      {children}
    </div>
  )
}

/* ─── SYSTEM STATUS panel ───────────────────────────────────── */
function MinerStatusPanel({ alive, currentTarget, minerId, lastUpdated }) {
  const uptime = useUptime(lastUpdated)
  const lines = [
    { label: 'SYS.STATUS', value: alive ? 'ONLINE' : 'OFFLINE', color: alive ? T.green : T.red },
    { label: 'PROC.TARGET', value: currentTarget || 'AWAITING_JOB', color: T.cyan },
    { label: 'NODE.UID', value: minerId && minerId !== '—' ? `${minerId.slice(0,8)}…${minerId.slice(-4)}` : 'UNREGISTERED', color: T.purple },
    { label: 'SESSION.UPTIME', value: alive ? uptime : '--:--:--', color: T.green },
  ]
  return (
    <Panel accent={alive ? T.green : T.red}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>◈</span>
        <span>SYSTEM STATUS</span>
        <span style={{ marginLeft: 'auto', ...S.pill(alive ? T.green : T.red) }}>
          {alive ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>
      {lines.map(({ label, value, color }) => (
        <div key={label} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '11px' }}>{label}</span>
          <span style={{ color, fontWeight: 700, textShadow: glow(color, 3), fontSize: '12px' }}>{value}</span>
        </div>
      ))}
      <div style={{ marginTop: '14px', padding: '10px 12px', background: '#020902',
                    border: `1px solid ${T.border}`, fontSize: '10px', color: T.textDim,
                    letterSpacing: '0.06em', lineHeight: 1.8 }}>
        <span style={{ color: T.green }}>{'>'}</span> LIFE-COMPUTE v2.0.0 INITIALIZED<br/>
        <span style={{ color: T.green }}>{'>'}</span> BOLTZ2 GPU INFERENCE{' '}
        <span style={{ color: T.cyan }}>ACTIVE</span><br/>
        <span style={{ color: T.green }}>{'>'}</span> DRUG DISCOVERY SUBSTRATE LOADED
      </div>
    </Panel>
  )
}

/* ─── Live scoring feed panel ───────────────────────────────── */
function LiveScoringFeedPanel({ feed }) {
  const rows = feed ?? []
  const srcColor = (s) =>
    s === 'ref'       ? T.purple :
    s === 'generated' ? T.cyan :
    T.greenDim

  return (
    <Panel accent={T.cyan}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>LIVE SCORING FEED</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.cyan) }}>LIVE · 10s</span>
      </div>

      {/* scrollable table */}
      <div style={{ overflowX: 'auto' }}>
        <div style={{ minWidth: '520px' }}>
          <div style={{ display: 'grid',
                        gridTemplateColumns: '70px 60px 160px 72px 72px 42px 52px',
                        gap: '8px', padding: '4px 0 6px',
                        borderBottom: `1px solid ${T.border}`,
                        fontSize: '9px', color: T.textDim, letterSpacing: '0.14em' }}>
            <span>TIME</span><span>TARGET</span><span>SMILES</span>
            <span style={{ textAlign:'right' }}>BOLTZ</span>
            <span style={{ textAlign:'right' }}>AFF(kcal)</span>
            <span style={{ textAlign:'center' }}>HIT?</span>
            <span style={{ textAlign:'center' }}>SOURCE</span>
          </div>

          {rows.length === 0 ? (
            <div style={{ color: T.textDim, fontSize: '11px', padding: '16px 0' }}>
              AWAITING FIRST BOLTZ2 EVALUATION…
              <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
            </div>
          ) : rows.map((r, i) => (
            <div key={i} style={{
              display:             'grid',
              gridTemplateColumns: '70px 60px 160px 72px 72px 42px 52px',
              gap:                 '8px',
              padding:             '6px 0',
              borderBottom:        `1px solid #0a150a`,
              fontSize:            '11px',
              alignItems:          'center',
              background:          i === 0 ? '#00ff4106' : 'transparent',
            }}>
              <span style={{ color: T.textDim, fontSize: '10px', fontVariantNumeric: 'tabular-nums' }}>
                {r.ts ? r.ts.slice(11, 19) : '—'}
              </span>
              <span style={{ color: T.cyan, fontWeight: 700, textShadow: glow(T.cyan, 2) }}>
                {r.target_id}
              </span>
              <span title={r.smiles || ''}
                    style={{ color: T.green, overflow: 'hidden', textOverflow: 'ellipsis',
                             whiteSpace: 'nowrap', fontSize: '10px', fontFamily: T.mono,
                             cursor: 'default' }}>
                {r.smiles ? (r.smiles.length > 20 ? r.smiles.slice(0, 20) + '…' : r.smiles) : '—'}
              </span>
              <span style={{ color: r.boltz_score != null ? T.cyan : T.textDim,
                             textAlign: 'right', fontVariantNumeric: 'tabular-nums',
                             textShadow: r.boltz_score != null ? glow(T.cyan, 2) : 'none' }}>
                {r.boltz_score != null ? r.boltz_score.toFixed(4) : '—'}
              </span>
              <span style={{ color: T.text, textAlign: 'right',
                             fontVariantNumeric: 'tabular-nums', fontSize: '10px' }}>
                {r.affinity != null ? r.affinity.toFixed(3) : '—'}
              </span>
              <span style={{ textAlign: 'center' }}>
                <span style={S.pill(r.hit ? T.green : T.red)}>{r.hit ? 'HIT' : 'MISS'}</span>
              </span>
              <span style={{ textAlign: 'center' }}>
                <span style={{ ...S.pill(srcColor(r.source)), fontSize: '9px' }}>
                  {(r.source || '?').slice(0, 7)}
                </span>
              </span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  )
}

/* ─── Current molecule panel (kept for private priv.generated feed) ─ */
function CurrentMoleculePanel({ pub, priv }) {
  const history = pub?.scoring_history ?? []
  // history is sorted oldest→newest; last element is the most recent bucket
  const latest  = history.length ? history[history.length - 1] : null
  const generated = priv?.generated ?? []
  const recent  = generated.slice(0, 5)
  return (
    <Panel accent={T.cyan}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>LIVE MOLECULAR ANALYSIS</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.cyan) }}>STREAMING</span>
      </div>

      {latest ? (
        <div style={{ marginBottom: '16px', padding: '12px 14px', background: '#020a0a',
                      border: `1px solid ${T.cyan}33`, borderRadius: '2px' }}>
          <div style={{ fontSize: '10px', color: T.textDim, marginBottom: '6px',
                        letterSpacing: '0.1em' }}>LAST SCORED — {latest.ts_iso?.slice(11,19) ?? '??:??:??'}</div>
          <div style={{ fontSize: '11px', color: T.cyan, textShadow: glow(T.cyan, 2),
                        wordBreak: 'break-all', fontFamily: T.mono, lineHeight: 1.6 }}>
            {latest.target_id ? `[TARGET:${latest.target_id}]` : ''}
          </div>
          <div style={{ marginTop: '8px', display: 'flex', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '22px', fontWeight: 700, color: T.cyan,
                            textShadow: glow(T.cyan, 4) }}>
                {latest.best_score !== null ? latest.best_score.toFixed(4) : '—'}
              </div>
              <div style={S.label}>affinity score</div>
            </div>
          </div>
        </div>
      ) : (
        <div style={{ padding: '12px 14px', background: '#020a0a', border: `1px solid ${T.border}`,
                      color: T.textDim, fontSize: '11px', marginBottom: '16px' }}>
          AWAITING FIRST BOLTZ2 EVALUATION…
          <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
        </div>
      )}

      <div style={{ fontSize: '10px', color: T.textDim, letterSpacing: '0.12em',
                    marginBottom: '8px' }}>RECENT MOLECULES EVALUATED</div>
      {recent.length > 0 ? recent.map((r, i) => (
        <div key={i} style={S.terminalLine}>
          <span style={S.prompt}>$</span>
          <span style={S.smiles}>{r.smiles ?? '—'}</span>
          <span style={{ color: r.boltz_score != null ? T.green : T.textDim, flexShrink: 0,
                         fontSize: '11px', textShadow: r.boltz_score != null ? glow(T.green,2) : 'none' }}>
            {r.boltz_score != null ? r.boltz_score.toFixed(4) : '—'}
          </span>
          <span style={{ ...S.pill(T.purple), flexShrink: 0 }}>
            {(r.method ?? 'scan').slice(0, 8)}
          </span>
        </div>
      )) : (
        <div style={{ color: T.textDim, fontSize: '11px' }}>
          INITIALIZING MOLECULAR SWEEP
          <span style={{ animation: 'blink 0.8s step-end infinite', color: T.green }}> █</span>
        </div>
      )}
    </Panel>
  )
}

/* ─── Molecules screened panel ──────────────────────────────── */
function MoleculesPanel({ count }) {
  const n = useAnimatedNumber(count)
  return (
    <Panel accent={T.green}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>◉</span>
        <span>MOLECULES SCREENED</span>
      </div>
      <div style={S.bigNum(T.green)}>{n.toLocaleString()}</div>
      <div style={{ ...S.label, marginBottom: '12px' }}>drug candidate evaluations completed</div>
      <div style={{ height: '2px', background: `linear-gradient(90deg, ${T.green}, ${T.cyan}, ${T.purple})`,
                    boxShadow: `0 0 8px ${T.green}`, borderRadius: '1px' }} />
    </Panel>
  )
}

/* ─── $LIFE earned panel ────────────────────────────────────── */
function LifeEarnedPanel({ earned }) {
  const n = useAnimatedNumber(Math.floor(earned))
  return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.purple)}>✦</span>
        <span>$LIFE EARNED</span>
      </div>
      <div style={S.bigNum(T.purple)}>{n.toLocaleString()}</div>
      <div style={{ fontSize: '18px', color: T.purpleDim, marginTop: '-4px', marginBottom: '8px',
                    textShadow: glow(T.purple, 3) }}>LIFE TOKENS</div>
      <div style={{ ...S.label, marginBottom: '12px' }}>minted on-chain for verified discoveries</div>
      <div style={{ height: '2px', background: `linear-gradient(90deg, ${T.purple}, ${T.cyan})`,
                    boxShadow: `0 0 8px ${T.purple}`, borderRadius: '1px' }} />
    </Panel>
  )
}

/* ─── GPU Power Monitor panel (amber) ──────────────────────── */
function GpuPowerPanel() {
  const [gpu, setGpu] = useState(null)
  useEffect(() => {
    async function poll() {
      try { const r = await fetch('/gpu?' + Date.now()); if (r.ok) setGpu(await r.json()) } catch {}
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  const amber  = '#ff8c00'
  const noData = !gpu || gpu.error
  const pct    = noData ? null : Math.min(100, (gpu.power_draw / 400) * 100)
  const tempColor = noData ? amber
    : gpu.temperature > 80 ? '#ff003c'
    : gpu.temperature > 70 ? amber
    : '#00ff41'
  const rows = [
    { label: 'POWER',    value: noData ? 'NO GPU DATA' : `${gpu.power_draw.toFixed(0)}W`,                                  color: amber },
    { label: 'GPU UTIL', value: noData ? '—'           : `${gpu.gpu_util.toFixed(0)}%`,                                    color: '#00ffff' },
    { label: 'TEMP',     value: noData ? '—'           : `${gpu.temperature}°C`,                                           color: tempColor },
    { label: 'VRAM',     value: noData ? '—'           : `${(gpu.memory_used/1024).toFixed(1)}GB / ${(gpu.memory_total/1024).toFixed(0)}GB`, color: '#00ff41' },
  ]
  return (
    <Panel accent={amber}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(amber)}>⚡</span>
        <span>GPU POWER MONITOR</span>
        <span style={{ marginLeft: 'auto', ...S.pill(amber) }}>AMBER</span>
      </div>
      {rows.map(({ label, value, color }) => (
        <div key={label} style={S.kv}>
          <span style={{ color: '#5a9a5a', fontSize: '11px' }}>{label}</span>
          <span style={{ color, fontWeight: 700, textShadow: glow(color, 3), fontSize: '12px' }}>{value}</span>
        </div>
      ))}
      {pct != null && (
        <div style={{ marginTop: '14px' }}>
          <div style={S.progressTrack}>
            <div style={{ position: 'absolute', top: 0, left: 0, height: '100%',
                          width: `${pct}%`,
                          background: `linear-gradient(90deg, ${amber}, #ff4400)`,
                          boxShadow: `0 0 8px ${amber}`, transition: 'width 1s ease' }} />
          </div>
          <div style={{ fontSize: '9px', color: '#5a9a5a', letterSpacing: '0.08em',
                        marginTop: '4px', textAlign: 'right' }}>
            {gpu.power_draw.toFixed(0)}W / 400W TDP
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ─── GPU Workers panel (multi-GPU) ────────────────────────────── */
function GpuWorkersPanel({ workers }) {
  const amber = '#ff8c00'
  if (!workers || workers.length === 0) return null
  const totalMols = workers.reduce((s, w) => s + (w.molecules || 0), 0)
  const totalLife = workers.reduce((s, w) => s + (w.life     || 0), 0)
  return (
    <Panel accent={amber}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(amber)}>⚡</span>
        <span>GPU WORKERS — {workers.length} GPUs ACTIVE</span>
        <span style={{ marginLeft: 'auto', ...S.pill(amber) }}>MULTI-GPU</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(workers.length, 4)}, 1fr)`, gap: '10px', marginTop: '10px' }}>
        {workers.map(w => {
          const tempColor = (w.power_w || 0) > 80 ? '#ff003c' : (w.power_w || 0) > 70 ? amber : '#00ff41'
          return (
            <div key={w.gpu} style={{ background: '#0a140a', border: `1px solid ${amber}44`, borderRadius: '2px', padding: '10px' }}>
              <div style={{ fontSize: '10px', color: amber, letterSpacing: '0.12em', marginBottom: '6px', fontWeight: 700 }}>
                GPU {w.gpu} — {(w.name || '').replace('NVIDIA GeForce ', '').replace('NVIDIA ', '')}
              </div>
              {[
                { label: 'TARGET',     value: w.target     || '—',                                           color: '#00ffff' },
                { label: 'LAST SCORE', value: w.last_score != null ? w.last_score.toFixed(3) : '—',          color: '#00ff41' },
                { label: 'POWER',      value: w.power_w    != null ? `${w.power_w.toFixed(0)}W` : '—',       color: amber },
                { label: 'MOLECULES',  value: w.molecules  != null ? w.molecules.toLocaleString() : '—',     color: T.text },
              ].map(({ label, value, color }) => (
                <div key={label} style={{ ...S.kv, marginBottom: '2px' }}>
                  <span style={{ color: T.textDim, fontSize: '10px' }}>{label}</span>
                  <span style={{ color, fontWeight: 700, fontSize: '11px', textShadow: glow(color, 2) }}>{value}</span>
                </div>
              ))}
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: '24px', marginTop: '10px', paddingTop: '8px', borderTop: `1px solid ${amber}22` }}>
        <span style={{ fontSize: '11px', color: T.textDim }}>
          COMBINED MOLECULES: <span style={{ color: T.green, fontWeight: 700 }}>{totalMols.toLocaleString()}</span>
        </span>
        <span style={{ fontSize: '11px', color: T.textDim }}>
          COMBINED $LIFE: <span style={{ color: amber, fontWeight: 700 }}>{totalLife.toFixed(2)}</span>
        </span>
      </div>
    </Panel>
  )
}

/* ─── Cancer targets panel ──────────────────────────────────── */
function TargetsPanel({ targets }) {
  const protein = targets.filter(t => !t.endsWith('_mRNA'))
  const mrna    = targets.filter(t => t.endsWith('_mRNA'))
  const PROTEIN_GENES = ['TP53', 'BRCA1', 'EGFR', 'HER2', 'KRAS', 'BCL2', 'CDK4', 'VEGFR2', 'PDL1', 'MDM2']
  const MRNA_GENES    = ['MYC_mRNA', 'KRAS_mRNA', 'BCL2_mRNA', 'TERT_mRNA', 'PDL1_mRNA']
  return (
    <Panel accent={T.cyan}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>ACTIVE TARGETS</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: '10px', alignItems: 'center' }}>
          <span style={{ color: T.cyan, fontSize: '10px', opacity: 0.8 }}>PROTEIN</span>
          <span style={{ color: T.cyan, fontWeight: 700, textShadow: glow(T.cyan, 3) }}>{protein.length}</span>
          <span style={{ color: '#ff9f43', fontSize: '10px', opacity: 0.8 }}>RNA</span>
          <span style={{ color: '#ff9f43', fontWeight: 700, textShadow: glow('#ff9f43', 3) }}>{mrna.length}</span>
        </span>
      </div>
      {targets.length === 0 ? (
        <div style={{ ...S.label, padding: '8px 0' }}>
          AWAITING FIRST TARGET ASSIGNMENT
          <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
        </div>
      ) : (
        <>
          {protein.map((t, i) => (
            <div key={i} style={S.targetItem}>
              <div style={S.targetPip} />
              <span style={{ color: T.cyan, fontWeight: 700, textShadow: glow(T.cyan, 2) }}>{t}</span>
              <span style={{ marginLeft: 'auto', ...S.pill(T.green) }}>ACTIVE</span>
            </div>
          ))}
          {mrna.map((t, i) => (
            <div key={'m'+i} style={{ ...S.targetItem, borderColor: '#ff9f4340' }}>
              <div style={{ ...S.targetPip, background: '#ff9f43', boxShadow: '0 0 6px #ff9f43' }} />
              <span style={{ color: '#ff9f43', fontWeight: 700, textShadow: glow('#ff9f43', 2) }}>{t}</span>
              <span style={{ marginLeft: 'auto', fontSize: '9px', color: '#ff9f43', opacity: 0.85,
                             border: '1px solid #ff9f4360', borderRadius: '3px', padding: '1px 5px',
                             letterSpacing: '0.05em', fontFamily: T.mono }}>RNA</span>
              <span style={{ ...S.pill(T.green) }}>ACTIVE</span>
            </div>
          ))}
        </>
      )}
      {PROTEIN_GENES.filter(g => !targets.includes(g)).slice(0, 2).map((g, i) => (
        <div key={g} style={{ ...S.targetItem, opacity: 0.3, border: `1px solid ${T.border}` }}>
          <div style={{ ...S.targetPip, background: T.textDim, boxShadow: 'none', animation: 'none' }} />
          <span style={{ color: T.textDim }}>{g}</span>
          <span style={{ marginLeft: 'auto', ...S.pill(T.textDim) }}>LOCKED</span>
        </div>
      ))}
      {MRNA_GENES.filter(g => !targets.includes(g)).slice(0, 2).map((g, i) => (
        <div key={g} style={{ ...S.targetItem, opacity: 0.25, border: `1px solid ${T.border}` }}>
          <div style={{ ...S.targetPip, background: T.textDim, boxShadow: 'none', animation: 'none' }} />
          <span style={{ color: T.textDim }}>{g}</span>
          <span style={{ marginLeft: '6px', fontSize: '9px', color: T.textDim,
                         border: '1px solid #ffffff20', borderRadius: '3px',
                         padding: '1px 4px', fontFamily: T.mono }}>RNA</span>
          <span style={{ marginLeft: 'auto', ...S.pill(T.textDim) }}>LOCKED</span>
        </div>
      ))}
    </Panel>
  )
}

/* ─── Scoring history panel ─────────────────────────────────── */
function ScoringHistoryPanel({ history }) {
  const canvasRef = useRef()
  const scores = (history ?? []).filter(r => r.best_score != null).map(r => r.best_score)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || scores.length < 2) return
    const ctx = canvas.getContext('2d')
    const w = canvas.width, h = canvas.height
    ctx.clearRect(0, 0, w, h)
    const mn = Math.min(...scores) * 0.95, mx = Math.max(...scores) * 1.05
    const toY = v => h - ((v - mn) / (mx - mn)) * (h - 8) - 4
    const toX = i => (i / (scores.length - 1)) * w

    // grid lines
    ctx.strokeStyle = '#00ff4110'; ctx.lineWidth = 1
    for (let i = 0; i <= 4; i++) {
      const y = (i / 4) * h
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }

    // glow fill
    const grad = ctx.createLinearGradient(0, 0, 0, h)
    grad.addColorStop(0, '#00ff4130'); grad.addColorStop(1, '#00ff4100')
    ctx.fillStyle = grad
    ctx.beginPath(); ctx.moveTo(toX(0), h)
    scores.forEach((s, i) => ctx.lineTo(toX(i), toY(s)))
    ctx.lineTo(toX(scores.length-1), h); ctx.closePath(); ctx.fill()

    // line
    ctx.strokeStyle = T.green; ctx.lineWidth = 2
    ctx.shadowColor = T.green; ctx.shadowBlur = 8
    ctx.beginPath()
    scores.forEach((s, i) => i === 0 ? ctx.moveTo(toX(i), toY(s)) : ctx.lineTo(toX(i), toY(s)))
    ctx.stroke()

    // dots
    scores.forEach((s, i) => {
      ctx.fillStyle = T.cyan; ctx.shadowColor = T.cyan; ctx.shadowBlur = 6
      ctx.beginPath(); ctx.arc(toX(i), toY(s), 2.5, 0, Math.PI*2); ctx.fill()
    })
  }, [scores])

  const best = scores.length ? Math.max(...scores) : null
  const avg  = scores.length ? scores.reduce((a,b) => a+b, 0) / scores.length : null

  return (
    <Panel accent={T.green}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>▲</span>
        <span>SCORING HISTORY — LAST 2H</span>
        <span style={{ marginLeft: 'auto', color: T.textDim, fontSize: '10px' }}>5m buckets · {scores.length} pts</span>
      </div>

      <div style={{ display: 'flex', gap: '28px', marginBottom: '14px' }}>
        <div>
          <div style={{ fontSize: '26px', fontWeight: 700, color: T.green, lineHeight: 1,
                        textShadow: glow(T.green, 5) }}>{best !== null ? best.toFixed(4) : '—'}</div>
          <div style={S.label}>peak score</div>
        </div>
        <div>
          <div style={{ fontSize: '26px', fontWeight: 700, color: T.cyan, lineHeight: 1,
                        textShadow: glow(T.cyan, 3) }}>{avg !== null ? avg.toFixed(4) : '—'}</div>
          <div style={S.label}>mean score</div>
        </div>
      </div>

      <canvas ref={canvasRef} width={340} height={100} style={{ width: '100%', height: '100px',
        border: `1px solid ${T.border}`, background: '#020902', display: 'block' }} />

      {history.length > 0 && (
        <div style={{ marginTop: '12px', maxHeight: '140px', overflowY: 'auto' }}>
          {history.slice().reverse().slice(0, 8).map((r, i) => (
            <div key={i} style={{ display: 'grid', gridTemplateColumns: '80px 1fr 60px',
                                   gap: '8px', padding: '5px 0', borderBottom: `1px solid #0a150a`,
                                   fontSize: '10px', alignItems: 'center' }}>
              <span style={{ color: T.textDim }}>{r.ts_iso?.slice(11,19) ?? '??:??:??'}</span>
              <span style={{ color: T.cyan }}>[{r.target_id ?? '?'}]</span>
              <span style={{ color: r.best_score != null ? T.green : T.textDim, fontWeight: 700,
                              textShadow: r.best_score != null ? glow(T.green, 2) : 'none',
                              textAlign: 'right' }}>
                {r.best_score != null ? r.best_score.toFixed(4) : '—'}
              </span>
            </div>
          ))}
        </div>
      )}

      {!history.length && (
        <div style={{ ...S.label, marginTop: '8px' }}>
          ACCUMULATING BOLTZ2 EVALUATIONS
          <span style={{ animation: 'blink 0.8s step-end infinite', color: T.green }}> █</span>
        </div>
      )}
    </Panel>
  )
}

/* ─── Global network panel ──────────────────────────────────── */
function NetworkPanel({ network }) {
  const fmt = v => v != null ? v.toLocaleString() : '—'
  const rows = [
    { label: 'MINERS_ONLINE',   value: fmt(network?.total_miners),      color: T.green },
    { label: 'GLOBAL_SCREENED', value: fmt(network?.molecules_screened), color: T.cyan },
    { label: 'CONFIRMED_HITS',  value: fmt(network?.targets_solved),     color: T.purple },
  ]
  return (
    <Panel accent={T.cyan}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>◈</span>
        <span>GLOBAL NETWORK MONITOR</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.cyan) }}>ON-CHAIN</span>
      </div>
      {rows.map(({ label, value, color }) => (
        <div key={label} style={S.globalRow}>
          <span style={{ color: T.textDim, fontSize: '11px', letterSpacing: '0.1em' }}>{label}</span>
          <span style={{ fontSize: '20px', fontWeight: 700, color,
                          textShadow: glow(color, 4), fontVariantNumeric: 'tabular-nums' }}>
            {value}
          </span>
        </div>
      ))}
      <div style={{ marginTop: '14px', padding: '10px 12px', background: '#020a09',
                    border: `1px solid ${T.cyan}22`, fontSize: '10px', lineHeight: 1.8,
                    color: T.textDim, letterSpacing: '0.06em' }}>
        <span style={{ color: T.cyan }}>{'>'}</span> SOLANA DEVNET — RPC CONNECTED<br/>
        <span style={{ color: T.cyan }}>{'>'}</span> PROGRAM:{' '}
        <span style={{ color: T.green, fontSize: '9px' }}>DzcQHhTPuiq…WsKvJ</span><br/>
        <span style={{ color: T.cyan }}>{'>'}</span> CONSENSUS: 2-OF-N VALIDATORS
      </div>
    </Panel>
  )
}

/* ─── Private panels ────────────────────────────────────────── */
function PrivateNote() {
  return (
    <div style={{ fontSize: '9px', color: T.purple, background: '#0d0020',
                  border: `1px solid ${T.purple}33`, padding: '3px 8px',
                  marginBottom: '14px', letterSpacing: '0.1em', display: 'inline-block',
                  textShadow: glow(T.purple, 2) }}>
      🔒 LOCAL DIAGNOSTICS — NOT BROADCAST
    </div>
  )
}

/* ─── LIFE PULSE panel (dedicated /pulse endpoint) ──────────── */
function LifePulsePanel({ pulse }) {
  if (!pulse) return (
    <Panel accent={T.green}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>⚡</span>
        <span>LIFE PULSE — MOLECULAR SWEEP ENGINE</span>
      </div>
      <div style={{ color: T.textDim, fontSize: '11px', padding: '8px 0' }}>
        AWAITING PULSE DATA…<span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
      </div>
    </Panel>
  )

  const {
    active = false,
    total_evaluated = 0,
    sobol_index = 0,
    current_batch_size = 200,
    top_molecules = [],
    mutant_attempted = 0,
    mutant_accepted = 0,
    tanimoto_attempts = 0,
    tanimoto_pass_rate = null,
  } = pulse

  const accent       = active ? T.green : T.textDim
  const statusLabel  = active ? 'ACTIVE' : 'IDLE'
  const statusColor  = active ? T.green  : T.textDim
  const FCOL = { kinase: T.purple, cytokine: '#ff69b4', protease: '#ff8c00', nuclear_receptor: T.cyan, general: T.green }

  return (
    <Panel accent={accent}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>⚡</span>
        <span>LIFE PULSE — MOLECULAR SWEEP ENGINE</span>
        <span style={{ marginLeft: 'auto', ...S.pill(statusColor) }}>
          {active && <span style={{ display:'inline-block', width:6, height:6, borderRadius:'50%',
            background:T.green, boxShadow:glow(T.green,3), marginRight:5,
            animation:'blink 1.2s step-end infinite' }} />}
          {statusLabel}
        </span>
      </div>

      {/* ── Top stats row ── */}
      <div style={{ display:'grid', gridTemplateColumns:'repeat(2,1fr)', gap:'8px', marginBottom:'12px' }}>
        {[
          { v: total_evaluated.toLocaleString(), l: 'EXPLORED', c: T.green },
          { v: sobol_index.toLocaleString(),     l: 'SOBOL IDX', c: T.cyan },
          { v: current_batch_size,               l: 'BATCH SIZE', c: T.purple },
          { v: tanimoto_pass_rate != null ? `${tanimoto_pass_rate.toFixed(1)}%` : '—',
            l: 'DIVERSITY', c: tanimoto_pass_rate != null && tanimoto_pass_rate < 30 ? T.red : T.green },
        ].map(({ v, l, c }) => (
          <div key={l} style={{ padding:'8px 10px', background:'#020902',
                                border:`1px solid ${c}22`, borderRadius:'2px' }}>
            <div style={{ fontSize:'18px', fontWeight:700, color:c, textShadow:glow(c,3),
                          fontVariantNumeric:'tabular-nums' }}>{v}</div>
            <div style={{ fontSize:'9px', color:T.textDim, letterSpacing:'0.12em',
                          marginTop:'2px' }}>{l}</div>
          </div>
        ))}
      </div>

      {/* ── Mutator stats ── */}
      <div style={{ fontSize:'10px', color:T.textDim, marginBottom:'10px', letterSpacing:'0.06em' }}>
        MUTATIONS:{' '}
        <span style={{ color:T.purple, fontWeight:700 }}>{mutant_attempted.toLocaleString()}</span>
        {' attempted · '}
        <span style={{ color: mutant_accepted > 0 ? T.green : T.textDim, fontWeight:700 }}>
          {mutant_accepted.toLocaleString()}
        </span>
        {' accepted'}
        {mutant_attempted > 0 && (
          <span style={{ ...S.pill(T.cyan), marginLeft:'8px', verticalAlign:'middle' }}>
            {Math.round(mutant_accepted / mutant_attempted * 100)}%
          </span>
        )}
      </div>
      {/* ── Top 3 elite molecules ── */}
      <div style={{ fontSize:'10px', color:T.textDim, letterSpacing:'0.14em',
                    marginBottom:'8px' }}>TOP CANDIDATES THIS SESSION</div>
      {top_molecules.length === 0 ? (
        <div style={{ color:T.textDim, fontSize:'11px', padding:'8px 0' }}>
          SWEEPING…<span style={{ animation:'blink 1s step-end infinite', color:T.green }}> █</span>
        </div>
      ) : top_molecules.map((m, i) => (
        <div key={i} style={{ ...S.terminalLine,
          background: i === 0 ? '#00ff4106' : 'transparent',
          padding: '7px 0',
          borderBottom: `1px solid ${i < 2 ? T.border : 'transparent'}` }}>
          <span style={{ color:T.greenDim, flexShrink:0, fontSize:'11px', minWidth:16 }}>
            #{i+1}
          </span>
          <span style={{ ...S.pill(FCOL[m.family] ?? T.textDim), flexShrink:0, fontSize:'9px' }}>
            {(m.family||'?').replace('_',' ').slice(0,10)}
          </span>
          <span style={S.smiles}>{m.smiles || '—'}</span>
          <span style={{ color:T.green, fontWeight:700, fontSize:'13px',
                         textShadow:glow(T.green,3), flexShrink:0, fontVariantNumeric:'tabular-nums' }}>
            {m.proxy_score?.toFixed(4)}
          </span>
          <span style={{ ...S.pill(m.source === 'mutant' ? T.purple : T.cyan),
                          fontSize:'9px', flexShrink:0 }}>
            {m.source === 'mutant' ? 'MUTANT' : 'SOBOL'}
          </span>
        </div>
      ))}
    </Panel>
  )
}

function PulsePanel({ pulse }) {
  if (!pulse) return null
  const { sobol_index = 0, total_evaluated = 0, top_proxy_score = 0, family_counts = {}, recent = [] } = pulse
  const FCOL = { kinase: T.purple, cytokine: '#ff69b4', protease: '#ff8c00', nuclear_receptor: T.cyan, general: T.green }
  return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.purple)}>⚡</span>
        <span>LIFE PULSE — SOBOL SWEEP</span>
      </div>
      <PrivateNote />
      <div style={{ display: 'flex', gap: '24px', marginBottom: '14px' }}>
        {[
          { v: sobol_index.toLocaleString(), l: 'exploration index', c: T.purple },
          { v: total_evaluated.toLocaleString(), l: 'swept', c: T.cyan },
          { v: top_proxy_score.toFixed(3), l: 'best proxy', c: T.green },
        ].map(({ v, l, c }) => (
          <div key={l}>
            <div style={{ fontSize: '24px', fontWeight: 700, color: c, textShadow: glow(c, 4) }}>{v}</div>
            <div style={S.label}>{l}</div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '12px' }}>
        {Object.entries(family_counts).map(([fam, cnt]) => (
          <span key={fam} style={S.pill(FCOL[fam] ?? T.textDim)}>{fam.replace('_',' ')} {cnt}</span>
        ))}
      </div>
      {recent.slice(0, 5).map((r, i) => (
        <div key={i} style={S.terminalLine}>
          <span style={S.prompt}>{'>'}</span>
          <span style={S.smiles}>{r.smiles}</span>
          <span style={{ color: T.green, flexShrink: 0, fontSize: '11px' }}>{r.proxy_score?.toFixed(3)}</span>
        </div>
      ))}
    </Panel>
  )
}

/* ── Public ART status panel (no private fields) ─────────────── */
/* ─── PROTEINNET panel ──────────────────────────────────────── */
function ProteinNetPanel({ proteinnet }) {
  if (!proteinnet) return null
  const { n_ready = 0, n_total = 0, top5 = [], generated_at = null } = proteinnet
  const lastUpdate = generated_at ? new Date(generated_at * 1000).toLocaleString() : '—'

  function ageFmt(ts) {
    if (!ts) return '—'
    const s = Math.floor((Date.now() / 1000) - ts)
    if (s < 60)   return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    return `${Math.floor(s / 3600)}h ago`
  }

  const colStyle = { color: T.textDim, fontSize: '10px', fontWeight: 700, letterSpacing: '0.05em' }
  const hdrRow   = { display: 'grid', gridTemplateColumns: '52px 52px 82px 48px 62px 1fr', gap: '4px', padding: '4px 0', borderBottom: `1px solid ${T.green}33` }

  return (
    <Panel accent={T.green} style={{ gridColumn: '1 / -1' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>🧬</span>
        <span>PROTEINNET — PER-TARGET MODELS</span>
        <span style={{ marginLeft: 'auto', ...S.pill(n_ready > 0 ? T.green : T.textDim) }}>
          {n_ready}/{n_total || 30} READY
        </span>
      </div>

      {/* Header row */}
      <div style={hdrRow}>
        {['TARGET','UNIPROT','TRAIN ROWS','R²','STATUS','LAST TRAINED'].map(h => (
          <span key={h} style={colStyle}>{h}</span>
        ))}
      </div>

      {top5.length === 0 ? (
        <div style={{ color: T.textDim, fontSize: '11px', padding: '8px 0' }}>
          Waiting for ≥30 Boltz2 scores per target to train models…
        </div>
      ) : top5.map(m => {
        const r2Color   = m.r2 != null && m.r2 >= 0.3 ? T.green : m.r2 != null && m.r2 >= 0.1 ? T.amber : T.textDim
        const statColor = m.status === 'ready' ? T.green : T.textDim
        return (
          <div key={m.target_id} style={{ display: 'grid', gridTemplateColumns: '52px 52px 82px 48px 62px 1fr', gap: '4px', padding: '3px 0', borderBottom: `1px solid ${T.green}18` }}>
            <span style={{ color: T.green, fontWeight: 700, fontSize: '11px', textShadow: glow(T.green, 1) }}>{m.target_id}</span>
            <span style={{ color: T.cyan,  fontSize: '11px' }}>{m.uniprot_id}</span>
            <span style={{ color: T.cyan,  fontSize: '11px', textAlign: 'right', paddingRight: '8px' }}>{m.n}</span>
            <span style={{ color: r2Color, fontWeight: 700, fontSize: '11px', textShadow: glow(r2Color, 1) }}>
              {m.r2 != null ? m.r2.toFixed(2) : '—'}
            </span>
            <span style={{ color: statColor, fontSize: '10px', fontWeight: 700, letterSpacing: '0.05em' }}>
              {m.status.toUpperCase()}
            </span>
            <span style={{ color: T.textDim, fontSize: '11px' }}>{ageFmt(m.last_trained)}</span>
          </div>
        )
      })}

      {/* Footer summary */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '8px', fontSize: '10px' }}>
        <span style={{ color: T.textDim }}>NETWORK: {n_ready}/{n_total || 30} models ready</span>
        <span style={{ color: T.textDim }}>LAST_SYNC: {lastUpdate}</span>
      </div>
    </Panel>
  )
}

function PublicArtPanel({ art }) {
  if (!art) return null
  const { ready=false, n_rows=0, r2=null, ts=null } = art
  const lastTrained = ts ? new Date(ts * 1000).toLocaleString() : '—'
  return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.purple)}>🧠</span>
        <span>ART MODEL STATUS</span>
        <span style={{ marginLeft:'auto', ...S.pill(ready ? T.green : T.red) }}>
          {ready ? 'READY' : 'NOT READY'}
        </span>
      </div>
      {[
        { k: 'MODEL_STATUS',  v: ready ? 'READY' : 'NOT READY', c: ready ? T.green : T.red },
        { k: 'TRAINING_ROWS', v: n_rows,                          c: T.cyan },
        { k: 'R²_SCORE',      v: r2 != null ? r2.toFixed(2) : '—', c: r2 != null && r2 >= 0.25 ? T.green : T.textDim },
        { k: 'LAST_TRAINED',  v: lastTrained,                     c: T.textDim },
      ].map(({ k, v, c }) => (
        <div key={k} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '11px' }}>{k}</span>
          <span style={{ color: c, fontWeight: 700, textShadow: glow(c, 2), fontSize: '11px' }}>{v}</span>
        </div>
      ))}
    </Panel>
  )
}

function ArtPanel({ art }) {
  if (!art) return null
  const { ready=false, n_rows=0, r2=null, reason='—', boltz_accumulated=0,
          retrain_progress=0, next_retrain_in=50, feature_importances={} } = art
  const top5 = Object.entries(feature_importances).sort((a,b)=>b[1]-a[1]).slice(0,5)
  return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.purple)}>🧠</span>
        <span>LIFE ART — ML SCORER</span>
        <span style={{ marginLeft:'auto', ...S.pill(ready ? T.green : T.red) }}>
          {ready ? 'DEPLOYED' : 'TRAINING'}
        </span>
      </div>
      <PrivateNote />
      {[
        { k: 'MODEL_READY', v: ready ? '✓ ACTIVE' : '✗ PENDING', c: T.green },
        { k: 'TRAINING_ROWS', v: `${n_rows} / 50`, c: T.cyan },
        { k: 'CV_R2_SCORE', v: r2 != null ? r2.toFixed(3) : '—', c: r2 != null && r2 >= 0.25 ? T.green : T.red },
        { k: 'BOLTZ2_SCORED', v: boltz_accumulated, c: T.text },
      ].map(({ k, v, c }) => (
        <div key={k} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '11px' }}>{k}</span>
          <span style={{ color: c, fontWeight: 700, textShadow: glow(c, 2) }}>{v}</span>
        </div>
      ))}
      <div style={{ marginTop: '8px' }}>
        <div style={{ display:'flex', justifyContent:'space-between', marginBottom:'4px', fontSize:'11px' }}>
          <span style={{ color: T.textDim }}>NEXT_RETRAIN</span>
          <span style={{ color: T.purple }}>{next_retrain_in} scores remaining</span>
        </div>
        <div style={S.progressTrack}>
          <div style={{ position:'absolute', top:0, left:0, height:'100%',
                        width:`${retrain_progress}%`, background: T.purple,
                        boxShadow: `0 0 6px ${T.purple}`, transition:'width 0.8s ease' }} />
        </div>
      </div>
      {top5.length > 0 && (
        <div style={{ marginTop:'12px' }}>
          <div style={{ ...S.label, marginBottom:'6px', letterSpacing:'0.12em' }}>TOP FEATURES</div>
          {top5.map(([f, imp]) => (
            <div key={f} style={{ ...S.kv, borderBottom:`1px solid #0a0a1a` }}>
              <span style={{ color: T.textDim, fontSize:'10px' }}>{f}</span>
              <span style={{ color: T.purple, fontSize:'10px' }}>{imp.toFixed(3)}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

function ScoutPanel({ scout, priv }) {
  if (!scout) return null
  const { last_family='—', last_phase='—', n_diverse=0, n_passed_filter=0, best_score=null, target_id='—' } = scout
  const PC = { explore: T.cyan, exploit: T.green, refine: T.purple }
  const pc = PC[last_phase] ?? T.textDim
  return (
    <Panel accent={T.cyan}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.cyan)}>🎯</span>
        <span>LIFE SCOUT — ROUTING</span>
        {last_phase !== '—' && <span style={{ marginLeft:'auto', ...S.pill(pc) }}>{last_phase.toUpperCase()}</span>}
      </div>
      <PrivateNote />
      {[
        { k: 'PROTEIN_FAMILY', v: last_family.replace('_',' '), c: T.purple },
        { k: 'ACTIVE_TARGET', v: target_id, c: T.cyan },
        { k: 'CANDIDATES_PASSED', v: n_passed_filter, c: T.text },
        { k: 'DIVERSE_RETURNED', v: n_diverse, c: T.green },
        { k: 'BEST_PRED_SCORE', v: best_score != null ? best_score.toFixed(4) : '—', c: T.green },
      ].map(({ k, v, c }) => (
        <div key={k} style={S.kv}>
          <span style={{ color: T.textDim, fontSize:'11px' }}>{k}</span>
          <span style={{ color: c, fontWeight: 700 }}>{v}</span>
        </div>
      ))}
      <div style={{ marginTop:'12px', display:'flex', gap:'6px' }}>
        {['explore','exploit','refine'].map(p => (
          <span key={p} style={{ ...S.pill(PC[p]), opacity: last_phase===p ? 1 : 0.3,
                                  fontWeight: last_phase===p ? 700 : 400 }}>{p}</span>
        ))}
      </div>
    </Panel>
  )
}

function GeneratedPanel({ generated }) {
  if (!generated?.length) return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}><span style={S.titleAccent(T.purple)}>🔮</span><span>GENERATED MOLECULES</span></div>
      <PrivateNote />
      <div style={S.label}>GENERATIVE PHASE PENDING — STARTING IN FINAL 15% OF EPOCH</div>
    </Panel>
  )
  return (
    <Panel accent={T.purple}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.purple)}>🔮</span>
        <span>GENERATED MOLECULES</span>
        <span style={{ marginLeft:'auto', color: T.purple }}>{generated.length} recent</span>
      </div>
      <PrivateNote />
      <div style={{ display:'grid', gridTemplateColumns:'1fr 80px 70px 70px', gap:'6px',
                    fontSize:'9px', color: T.textDim, letterSpacing:'0.1em',
                    padding:'4px 0', borderBottom:`1px solid ${T.border}`, marginBottom:'4px' }}>
        <span>SMILES</span><span>ART</span><span>BOLTZ</span><span>METHOD</span>
      </div>
      <div style={{ maxHeight:'220px', overflowY:'auto' }}>
        {generated.map((r, i) => (
          <div key={i} style={{ display:'grid', gridTemplateColumns:'1fr 80px 70px 70px',
                                 gap:'6px', padding:'5px 0', borderBottom:`1px solid #0a0a15`,
                                 fontSize:'10px', alignItems:'center' }}>
            <span style={{ ...S.smiles, fontSize:'9px' }}>{r.smiles ?? '—'}</span>
            <span style={{ color: r.art_score != null ? T.purple : T.textDim }}>
              {r.art_score != null ? r.art_score.toFixed(3) : '—'}
            </span>
            <span style={{ color: r.boltz_score != null ? T.green : T.textDim }}>
              {r.boltz_score != null ? r.boltz_score.toFixed(4) : '—'}
            </span>
            <span style={{ color: T.textDim, fontSize:'9px' }}>{(r.method ?? '').slice(0,10)}</span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/* ─── LIFE AGENT panel ──────────────────────────────────────── */

/** Minimal code-block syntax highlighter (no external deps). */
function highlightCode(code, lang) {
  if (!lang) return code
  const e = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  let s = e(code)
  // strings
  s = s.replace(/(&#39;|&quot;|`)(.*?)\1/g, '<span style="color:#ce9178">$1$2$1</span>')
  // keywords
  const kws = lang === 'python'
    ? ['def','class','return','import','from','as','if','elif','else','for','while',
       'in','not','and','or','True','False','None','with','try','except','raise',
       'yield','lambda','pass','break','continue','async','await']
    : ['const','let','var','function','return','if','else','for','while','in',
       'of','new','class','import','from','export','default','async','await',
       'try','catch','throw','true','false','null','undefined']
  kws.forEach(kw => {
    s = s.replace(new RegExp(`\\b(${kw})\\b`, 'g'), `<span style="color:#569cd6">$1</span>`)
  })
  // comments
  s = s.replace(/(#[^\n]*|\/\/[^\n]*)/g, '<span style="color:#6a9955">$1</span>')
  // numbers
  s = s.replace(/\b(\d+\.?\d*)\b/g, '<span style="color:#b5cea8">$1</span>')
  return s
}

/** Parse markdown-ish text into segments: text, code-block, inline-code. */
function parseContent(text) {
  const segs = []
  const codeBlockRe = /```(\w*)\n?([\s\S]*?)```/g
  let last = 0, m
  while ((m = codeBlockRe.exec(text)) !== null) {
    if (m.index > last) segs.push({ type: 'text', content: text.slice(last, m.index) })
    segs.push({ type: 'code', lang: m[1] || 'text', content: m[2] })
    last = m.index + m[0].length
  }
  if (last < text.length) segs.push({ type: 'text', content: text.slice(last) })
  return segs
}

/** Render a single message segment (text or code block). */
function MsgSegment({ seg }) {
  if (seg.type === 'code') {
    const html = highlightCode(seg.content, seg.lang)
    return (
      <div style={{
        background:    '#020a02',
        border:        `1px solid ${T.border}`,
        borderLeft:    `3px solid ${T.green}`,
        borderRadius:  '2px',
        padding:       '10px 14px',
        margin:        '8px 0',
        overflowX:     'auto',
        position:      'relative',
      }}>
        {seg.lang && (
          <div style={{ fontSize:'9px', color: T.textDim, letterSpacing:'0.14em',
                        marginBottom:'6px', textTransform:'uppercase' }}>{seg.lang}</div>
        )}
        <pre style={{ margin:0, fontFamily: T.mono, fontSize:'11px', lineHeight:1.7,
                      color: T.text, whiteSpace:'pre-wrap', wordBreak:'break-word' }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    )
  }
  // plain text — render bold, inline-code
  const parts = seg.content.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)
  return (
    <span style={{ lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
      {parts.map((p, i) => {
        if (p.startsWith('`') && p.endsWith('`'))
          return <code key={i} style={{ background:'#020a02', border:`1px solid ${T.border}`,
                                         color: T.cyan, padding:'1px 5px', fontSize:'10px',
                                         fontFamily: T.mono, borderRadius:'2px' }}>
            {p.slice(1,-1)}
          </code>
        if (p.startsWith('**') && p.endsWith('**'))
          return <strong key={i} style={{ color: T.textBright }}>{p.slice(2,-2)}</strong>
        return <span key={i}>{p}</span>
      })}
    </span>
  )
}

/** Save-to-adaptive button with state machine. */
function SaveFileButton({ filename, code }) {
  const [status, setStatus] = useState('idle') // idle | saving | saved | error
  const [errMsg, setErrMsg] = useState('')

  async function save() {
    if (status === 'saving') return
    setStatus('saving')
    try {
      const res = await fetch('/agent/write-file', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ filename, content: code }),
      })
      const data = await res.json()
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`)
      setStatus('saved')
    } catch (e) {
      setErrMsg(e.message)
      setStatus('error')
    }
  }

  const btnColor = status === 'saved'  ? T.green
                 : status === 'error'  ? T.red
                 : status === 'saving' ? T.textDim
                 : T.cyan
  const label    = status === 'saved'  ? `✓ Saved to adaptive/${filename}`
                 : status === 'error'  ? `✗ Error: ${errMsg}`
                 : status === 'saving' ? 'SAVING…'
                 : `SAVE TO ADAPTIVE/${filename}`

  return (
    <div style={{ marginTop: '4px' }}>
      <button
        onClick={save}
        disabled={status === 'saving' || status === 'saved'}
        style={{
          background:   status === 'saved' ? btnColor + '18' : '#000a0a',
          border:       `1px solid ${btnColor}66`,
          color:        btnColor,
          fontFamily:   T.mono,
          fontSize:     '10px',
          letterSpacing:'0.1em',
          padding:      '5px 14px',
          cursor:       status === 'saving' || status === 'saved' ? 'default' : 'pointer',
          borderRadius: '2px',
          textShadow:   status === 'idle' || status === 'error' ? `0 0 6px ${btnColor}` : 'none',
          transition:   'all 0.2s',
        }}
      >
        {label}
      </button>
      {status === 'saved' && <RestartMinerButton />}
    </div>
  )
}

/** Restart-miner button, shown after a successful save. */
function RestartMinerButton() {
  const [status, setStatus] = useState('idle') // idle | restarting | done | error
  const [errMsg, setErrMsg] = useState('')

  async function restart() {
    if (status === 'restarting') return
    setStatus('restarting')
    try {
      const res = await fetch('/agent/restart-miner', { method: 'POST' })
      const data = await res.json()
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`)
      setStatus('done')
    } catch (e) {
      setErrMsg(e.message)
      setStatus('error')
    }
  }

  const btnColor = status === 'done'       ? T.green
                 : status === 'error'      ? T.red
                 : status === 'restarting' ? T.textDim
                 : T.purple
  const label    = status === 'done'       ? '✓ MINER RESTARTED'
                 : status === 'error'      ? `✗ ${errMsg}`
                 : status === 'restarting' ? 'RESTARTING…'
                 : '↺ RESTART MINER'

  return (
    <button
      onClick={restart}
      disabled={status === 'restarting' || status === 'done'}
      style={{
        marginLeft:   '8px',
        background:   '#0a000a',
        border:       `1px solid ${btnColor}66`,
        color:        btnColor,
        fontFamily:   T.mono,
        fontSize:     '10px',
        letterSpacing:'0.1em',
        padding:      '5px 14px',
        cursor:       status === 'restarting' || status === 'done' ? 'default' : 'pointer',
        borderRadius: '2px',
        textShadow:   status === 'idle' ? `0 0 6px ${btnColor}` : 'none',
        transition:   'all 0.2s',
      }}
    >
      {label}
    </button>
  )
}

/** Single chat bubble. */
function ChatBubble({ role, content }) {
  const isUser = role === 'user'

  // For assistant messages: detect SAVE_FILE: filename.py marker and strip it from display
  const saveFileMatch = !isUser && content.match(/SAVE_FILE:\s*(\S+\.py)\s*$/m)
  const saveFilename  = saveFileMatch ? saveFileMatch[1] : null
  const displayContent = saveFilename
    ? content.replace(/\nSAVE_FILE:\s*\S+\.py\s*$/, '').replace(/SAVE_FILE:\s*\S+\.py\s*$/, '')
    : content

  const segs = parseContent(displayContent)

  // Find the last Python code block (to attach the save button to)
  const lastPyIdx = saveFilename
    ? segs.reduce((acc, s, i) => (s.type === 'code' && (s.lang === 'python' || s.lang === 'py') ? i : acc), -1)
    : -1

  return (
    <div style={{
      display:       'flex',
      flexDirection: isUser ? 'row-reverse' : 'row',
      gap:           '10px',
      marginBottom:  '14px',
      alignItems:    'flex-start',
    }}>
      {/* avatar pip */}
      <div style={{
        flexShrink:    0,
        width:         '22px',
        height:        '22px',
        border:        `1px solid ${isUser ? T.cyan : T.green}`,
        display:       'flex',
        alignItems:    'center',
        justifyContent:'center',
        fontSize:      '9px',
        color:         isUser ? T.cyan : T.green,
        textShadow:    glow(isUser ? T.cyan : T.green, 2),
        background:    isUser ? '#000a0a' : '#000a00',
        letterSpacing: '0.08em',
        marginTop:     '2px',
      }}>
        {isUser ? 'YOU' : 'AI'}
      </div>
      <div style={{
        flex:          1,
        background:    isUser ? '#000a0a' : '#020a02',
        border:        `1px solid ${isUser ? T.cyan + '33' : T.green + '33'}`,
        borderRadius:  '2px',
        padding:       '10px 14px',
        fontSize:      '12px',
        color:         T.text,
        maxWidth:      '90%',
      }}>
        {segs.map((seg, i) => (
          <div key={i}>
            <MsgSegment seg={seg} />
            {saveFilename && i === lastPyIdx && (
              <SaveFileButton filename={saveFilename} code={seg.content} />
            )}
          </div>
        ))}
        {/* Fallback: SAVE_FILE declared but no python block found — button at bottom */}
        {saveFilename && lastPyIdx === -1 && (
          <SaveFileButton
            filename={saveFilename}
            code={segs.filter(s => s.type === 'code').map(s => s.content).join('\n')}
          />
        )}
      </div>
    </div>
  )
}

function LifeAgentPanel() {
  const [configured, setConfigured] = useState(null)  // null=loading, bool
  const [messages,   setMessages]   = useState([])     // {role, content}[]
  const [input,      setInput]      = useState('')
  const [loading,    setLoading]    = useState(false)
  const [error,      setError]      = useState(null)
  const bottomRef = useRef()
  const inputRef  = useRef()

  // Check API key presence once
  useEffect(() => {
    fetch('/agent/status')
      .then(r => r.json())
      .then(d => setConfigured(d.configured))
      .catch(() => setConfigured(false))
  }, [])

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  async function send() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setError(null)
    const newMessages = [...messages, { role: 'user', content: text }]
    setMessages(newMessages)
    setLoading(true)
    try {
      const res = await fetch('/agent/chat', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ messages: newMessages }),
      })
      const data = await res.json()
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`)
      setMessages([...newMessages, { role: 'assistant', content: data.content }])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  // Loading state
  if (configured === null) return null

  return (
    <Panel accent={T.green} style={{ gridColumn: '1 / -1', border: '3px solid #ff69b4', boxShadow: '0 0 15px #ff69b4, 0 0 30px #ff69b4' }}>
      <div style={S.panelTitle}>
        <span style={S.titleAccent(T.green)}>◈</span>
        <span>LIFE AGENT</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.green) }}>CLAUDE&nbsp;SONNET&nbsp;4&nbsp;·&nbsp;AI&nbsp;ASSISTANT</span>
      </div>

      {!configured ? (
        /* ── No API key configured ── */
        <div style={{
          padding:      '28px',
          textAlign:    'center',
          border:       `1px dashed ${T.border}`,
          borderRadius: '2px',
          background:   '#020902',
        }}>
          <div style={{ fontSize:'28px', marginBottom:'12px', opacity:0.5 }}>🤖</div>
          <div style={{ fontSize:'13px', color: T.textDim, letterSpacing:'0.06em',
                        lineHeight: 1.8 }}>
            Add <code style={{ color: T.cyan, background:'#020a0a', padding:'2px 7px',
                                border:`1px solid ${T.border}`, fontFamily: T.mono,
                                fontSize:'11px' }}>ANTHROPIC_API_KEY</code> to{' '}
            <code style={{ color: T.green, background:'#020902', padding:'2px 7px',
                            border:`1px solid ${T.border}`, fontFamily: T.mono,
                            fontSize:'11px' }}>.env</code>{' '}
            to activate LIFE AGENT
          </div>
          <div style={{ marginTop:'14px', fontSize:'10px', color: T.textDim,
                        letterSpacing:'0.12em' }}>
            Then restart: <code style={{ color: T.greenDim, fontFamily: T.mono }}>pm2 restart life-dashboard</code>
          </div>
        </div>
      ) : (
        /* ── Chat interface ── */
        <>
          {/* message history */}
          <div style={{
            height:     '420px',
            overflowY:  'auto',
            padding:    '8px 4px',
            marginBottom:'14px',
            border:     `1px solid ${T.border}`,
            background: '#010701',
            borderRadius:'2px',
          }}>
            {messages.length === 0 && !loading && (
              <div style={{ padding:'24px 16px', color: T.textDim, fontSize:'11px',
                            lineHeight: 2, letterSpacing:'0.06em' }}>
                <div style={{ color: T.green, marginBottom:'8px', textShadow: glow(T.green, 3) }}>
                  LIFE AGENT ONLINE
                </div>
                I can help you build a better mining stack. Try asking:<br/>
                <span style={{ color: T.cyan }}>→ "How do I build life_pulse.py?"</span><br/>
                <span style={{ color: T.cyan }}>→ "Write a Sobol sweep for molecule search"</span><br/>
                <span style={{ color: T.cyan }}>→ "Why is my Boltz2 score low on KRAS?"</span><br/>
                <span style={{ color: T.cyan }}>→ "What chemical features bind TP53?"</span>
              </div>
            )}

            {messages.map((msg, i) => (
              <ChatBubble key={i} role={msg.role} content={msg.content} />
            ))}

            {loading && (
              <div style={{ display:'flex', gap:'10px', alignItems:'flex-start', marginBottom:'14px' }}>
                <div style={{ flexShrink:0, width:'22px', height:'22px',
                              border:`1px solid ${T.green}`, display:'flex',
                              alignItems:'center', justifyContent:'center',
                              fontSize:'9px', color: T.green, background:'#000a00',
                              letterSpacing:'0.08em' }}>AI</div>
                <div style={{ padding:'10px 14px', background:'#020a02',
                              border:`1px solid ${T.green}33`, borderRadius:'2px',
                              fontSize:'12px', color: T.textDim }}>
                  <span style={{ animation:'blink 0.8s step-end infinite', color: T.green }}>█</span>
                  {' '}THINKING…
                </div>
              </div>
            )}

            {error && (
              <div style={{ padding:'8px 14px', background:'#0a0006',
                            border:`1px solid ${T.red}44`, color: T.red,
                            fontSize:'11px', marginBottom:'8px', borderRadius:'2px' }}>
                ERROR: {error}
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* input row */}
          <div style={{ display:'flex', gap:'8px', alignItems:'flex-end' }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask LIFE AGENT anything… (Enter to send, Shift+Enter for newline)"
              rows={2}
              style={{
                flex:        1,
                background:  '#010701',
                border:      `1px solid ${T.border}`,
                borderRadius:'2px',
                color:       T.text,
                fontFamily:  T.mono,
                fontSize:    '12px',
                padding:     '10px 12px',
                resize:      'vertical',
                outline:     'none',
                lineHeight:  1.6,
                letterSpacing:'0.04em',
              }}
              onFocus={e => { e.target.style.borderColor = T.green + '88' }}
              onBlur={e  => { e.target.style.borderColor = T.border }}
            />
            <button
              onClick={send}
              disabled={loading || !input.trim()}
              style={{
                background:   loading || !input.trim() ? '#0a150a' : T.green + '18',
                border:       `1px solid ${loading || !input.trim() ? T.border : T.green + '88'}`,
                color:        loading || !input.trim() ? T.textDim : T.green,
                fontFamily:   T.mono,
                fontSize:     '11px',
                letterSpacing:'0.12em',
                padding:      '10px 18px',
                cursor:       loading || !input.trim() ? 'not-allowed' : 'pointer',
                borderRadius: '2px',
                textShadow:   loading || !input.trim() ? 'none' : glow(T.green, 3),
                transition:   'all 0.2s',
                height:       '100%',
                whiteSpace:   'nowrap',
              }}
            >
              {loading ? 'WAIT…' : 'SEND ▶'}
            </button>
            {messages.length > 0 && (
              <button
                onClick={() => { setMessages([]); setError(null) }}
                disabled={loading}
                style={{
                  background:   '#0a0008',
                  border:       `1px solid ${T.purple}44`,
                  color:        T.textDim,
                  fontFamily:   T.mono,
                  fontSize:     '10px',
                  letterSpacing:'0.1em',
                  padding:      '10px 12px',
                  cursor:       loading ? 'not-allowed' : 'pointer',
                  borderRadius: '2px',
                  height:       '100%',
                }}
              >
                CLEAR
              </button>
            )}
          </div>
          <div style={{ marginTop:'8px', fontSize:'9px', color: T.textDim,
                        letterSpacing:'0.1em' }}>
            ENTER = SEND · SHIFT+ENTER = NEWLINE · HISTORY PRESERVED IN SESSION
          </div>
        </>
      )}
    </Panel>
  )
}

/* ─── CSS ───────────────────────────────────────────────────── */
const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');
  * { box-sizing: border-box; }
  body { margin: 0; background: #050a05; font-family: 'Courier New', monospace; }
  ::selection { background: #00ff4133; color: #00ff41; }
  @keyframes textPulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.85; }
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0; }
  }
  @keyframes helix1 {
    from { stroke-dashoffset: 0; }
    to   { stroke-dashoffset: -100; }
  }
  @keyframes scanMove {
    from { background-position: 0 0; }
    to   { background-position: 0 4px; }
  }
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: #050a05; }
  ::-webkit-scrollbar-thumb { background: #00ff4133; border-radius: 0; }
  ::-webkit-scrollbar-thumb:hover { background: #00ff4166; }
`

/* ─── App ───────────────────────────────────────────────────── */
export default function App() {
  const [pub,  setPub]  = useState(null)
  const [priv, setPriv] = useState(null)
  const [feed, setFeed] = useState([])
  const [pulse, setPulse] = useState(null)
  const [tick, setTick] = useState(null)
  const [local, setLocal] = useState(false)

  // Stats poll — every 5s
  useEffect(() => {
    async function poll() {
      try { const r = await fetch('/stats?' + Date.now()); if (r.ok) setPub(await r.json()) } catch {}
      try {
        const r = await fetch('/private/stats?' + Date.now())
        if (r.ok) { setPriv(await r.json()); setLocal(true) }
        else       { setPriv(null); setLocal(false) }
      } catch { setPriv(null); setLocal(false) }
      setTick(new Date())
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  // Feed poll — every 10s (independent; faster than full stats)
  useEffect(() => {
    async function pollFeed() {
      try {
        const r = await fetch('/feed?' + Date.now())
        if (r.ok) { const d = await r.json(); setFeed(d.rows ?? []) }
      } catch {}
    }
    pollFeed()
    const id = setInterval(pollFeed, 10000)
    return () => clearInterval(id)
  }, [])

  // Pulse poll — every 5s
  useEffect(() => {
    async function pollPulse() {
      try {
        const r = await fetch('/pulse?' + Date.now())
        if (r.ok) setPulse(await r.json())
      } catch {}
    }
    pollPulse()
    const id = setInterval(pollPulse, 5000)
    return () => clearInterval(id)
  }, [])

  const alive     = pub?.alive              ?? false
  const mols      = pub?.molecules_screened ?? 0
  const life      = pub?.life_earned        ?? 0
  const tgts      = pub?.targets_contributed ?? []
  const network   = pub?.network            ?? {}
  const scoring   = pub?.scoring_history    ?? []
  const target    = pub?.current_target    ?? '—'
  const minerId   = pub?.miner_id          ?? '—'
  const lastUpd   = pub?.last_updated      ?? null
  const gpuWorkers = pub?.gpu_workers      ?? []

  return (
    <>
      <style>{CSS}</style>
      <div style={S.wrap}>
        <MatrixRain />
        <div style={S.scanlines} />
        <div style={S.content}>

          {/* ── Header ── */}
          <header style={S.header}>
            <div style={S.headerInner}>
              <div style={{ fontSize: '26px', fontWeight: 700, color: '#00ff41', fontFamily: T.mono, letterSpacing: '0.06em', textTransform: 'uppercase', textAlign: 'center' }}>
                LIFE COM<span style={{ color: '#ff69b4' }}>PUTE</span> — DECENTRALIZED DRUG DISCOVERY NETWORK
              </div>
              <div style={{ fontSize: '13px', color: '#00aa28', fontFamily: T.mono, letterSpacing: '0.1em', textTransform: 'uppercase', textAlign: 'center' }}>
                POWERED BY BOLTZ2 MOLECULAR DOCKING · SOLANA BLOCKCHAIN
              </div>
            </div>
            <div style={S.statusBadge(alive)}>
              <div style={S.statusDot(alive)} />
              {alive ? 'SYS:ONLINE' : 'SYS:OFFLINE'}
            </div>
          </header>

          {/* ── Grid ── */}
          <div style={S.grid}>

            {/* Public */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              PUBLIC // MINER TELEMETRY
            </div>

            <MinerStatusPanel alive={alive} currentTarget={target}
                              minerId={minerId} lastUpdated={lastUpd} />
            <MoleculesPanel   count={mols} />
            <LifeEarnedPanel  earned={life} />
            <GpuPowerPanel />
            {gpuWorkers.length > 1 && <GpuWorkersPanel workers={gpuWorkers} />}
            <TargetsPanel     targets={tgts} />
            <div style={{ gridColumn: 'span 3' }}>
              <LiveScoringFeedPanel feed={feed} />
            </div>

            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              PUBLIC // PERFORMANCE &amp; NETWORK
            </div>

            <ScoringHistoryPanel history={scoring} />
            <NetworkPanel        network={network} />
            <PublicArtPanel      art={pub?.art} />
            <ProteinNetPanel     proteinnet={pub?.proteinnet} />

            {/* LIFE PULSE — always visible (public endpoint) */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              PUBLIC // LIFE PULSE — SOBOL MOLECULAR SWEEP
            </div>
            <LifePulsePanel pulse={pulse} />

            {/* Private */}
            {local && (
              <>
                <div style={{ ...S.sectionLabel, color: T.purple, textShadow: glow(T.purple, 2) }}>
                  <div style={{ ...S.sectionTick, background: T.purple, boxShadow: glow(T.purple, 3) }} />
                  🔒 PRIVATE // LOCAL DIAGNOSTICS — NOT BROADCAST TO NETWORK
                </div>
                <PulsePanel     pulse={priv?.pulse} />
                <ArtPanel       art={priv?.art} />
                <ScoutPanel     scout={priv?.scout} priv={priv} />
                <GeneratedPanel generated={priv?.generated} />
              </>
            )}

            {/* LIFE AGENT — full-width AI assistant */}
            <div style={S.sectionLabel}>
              <div style={S.sectionTick} />
              AI // LIFE AGENT — MINING ASSISTANT
            </div>
            <LifeAgentPanel />

          </div>

          {/* ── Footer ── */}
          <footer style={S.footer}>
            <span>LIFE-COMPUTE MINER v2.0.0 // BIOPUNK EDITION</span>
            <span style={{ color: T.green, textShadow: glow(T.green, 2) }}>
              {tick ? `LAST_SYNC: ${tick.toLocaleTimeString()}` : 'CONNECTING…'}
            </span>
            <span style={{ color: local ? T.purple : T.textDim }}>
              {local ? '🔒 LOCALHOST — PRIVATE PANELS VISIBLE' : '🌐 PUBLIC VIEW'}
            </span>
          </footer>
        </div>
      </div>
    </>
  )
}
