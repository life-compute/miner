import { useState, useEffect, useRef, useCallback } from 'react'
import GridLayout from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'

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
  amber:         '#ffb300',
  amberDim:      '#cc8800',
  amberGlow:     'rgba(255,179,0,0.15)',
  red:           '#ff003c',
  redGlow:       'rgba(255,0,60,0.2)',
  muted:         '#1a3a1a',
  textDim:       '#5a9a5a',
  text:          '#aaddaa',
  textBright:    '#ccffcc',
  mono:          "'Courier New', 'Source Code Pro', 'Lucida Console', monospace",
}

/* ─── Grid config ───────────────────────────────────────────── */
const COLS        = 12
const ROW_HEIGHT  = 30
const MARGIN      = [12, 12]
const LS_KEY      = 'life-dashboard-layout-v3'

/* ─── Default layout ────────────────────────────────────────── */
const DEFAULT_LAYOUT = [
  { i: 'system-status',    x: 0,  y: 0,  w: 3, h: 7,  minW: 2, minH: 5  },
  { i: 'molecules',        x: 3,  y: 0,  w: 3, h: 7,  minW: 2, minH: 4  },
  { i: 'life-earned',      x: 6,  y: 0,  w: 3, h: 7,  minW: 2, minH: 4  },
  { i: 'gpu-power',        x: 9,  y: 0,  w: 3, h: 7,  minW: 2, minH: 5  },
  { i: 'live-feed',        x: 0,  y: 7,  w: 6, h: 14, minW: 3, minH: 8  },
  { i: 'targets',          x: 6,  y: 7,  w: 3, h: 14, minW: 2, minH: 6  },
  { i: 'network',          x: 9,  y: 7,  w: 3, h: 14, minW: 2, minH: 6  },
  { i: 'life-pulse',       x: 0,  y: 21, w: 6, h: 16, minW: 3, minH: 8  },
  { i: 'scoring-history',  x: 6,  y: 21, w: 6, h: 16, minW: 3, minH: 8  },
  { i: 'life-agent',       x: 0,  y: 37, w: 12, h: 22, minW: 4, minH: 12 },
]

/* ─── Load / save layout ────────────────────────────────────── */
function loadLayout() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return DEFAULT_LAYOUT
    const saved = JSON.parse(raw)
    // merge: preserve minW/minH from defaults, overlay saved positions
    const defMap = Object.fromEntries(DEFAULT_LAYOUT.map(d => [d.i, d]))
    return saved.map(s => ({ ...defMap[s.i], ...s })).filter(s => defMap[s.i])
  } catch { return DEFAULT_LAYOUT }
}

function saveLayout(layout) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(layout)) } catch {}
}

/* ─── Reusable glow box-shadow ──────────────────────────────── */
const glow        = (c, s = 8)  => `0 0 ${s}px ${c}, 0 0 ${s*2}px ${c}44`
const panelShadow = (c)         => `0 0 1px ${c}, 0 0 20px ${c}22, inset 0 0 40px ${c}05`

/* ─── Styles ─────────────────────────────────────────────────── */
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
    marginBottom:  '14px',
    display:       'flex',
    alignItems:    'center',
    gap:           '8px',
    cursor:        'grab',
    userSelect:    'none',
  },
  dragHandle: {
    fontSize:      '14px',
    color:         T.textDim,
    opacity:       0.5,
    marginRight:   '2px',
    flexShrink:    0,
    lineHeight:    1,
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
      <path
        d={`M 0 32 ${Array.from({length:pts}, (_,i)=>{
          const x = (i/pts)*320
          const y = 32 + Math.sin((i/pts)*Math.PI*2)*20
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.green} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
      <path
        d={`M 0 32 ${Array.from({length:pts}, (_,i)=>{
          const x = (i/pts)*320
          const y = 32 - Math.sin((i/pts)*Math.PI*2)*20
          return `${i===0?'':'L'} ${x} ${y}`
        }).join(' ')}`}
        fill="none" stroke={T.cyan} strokeWidth="1.5" filter="url(#gf)"
        style={{animation:'helix1 3s linear infinite'}}
      />
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
      const start = new Date(lastUpdated).getTime() - 60000
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

/* ─── useWindowWidth ────────────────────────────────────────── */
function useWindowWidth() {
  const [w, setW] = useState(window.innerWidth)
  useEffect(() => {
    const fn = () => setW(window.innerWidth)
    window.addEventListener('resize', fn)
    return () => window.removeEventListener('resize', fn)
  }, [])
  return w
}

/* ─── Panel wrapper ─────────────────────────────────────────── */
// titleContent: what to put after the drag handle in the title bar
// isAgent: enables the special LIFE AGENT glow treatment
function Panel({ accent = T.green, style, titleContent, isAgent = false, children }) {
  const agentStyle = isAgent ? {
    background:  '#0a1a0a',
    border:      `2px solid #00ff41`,
    boxShadow:   `0 0 20px #00ff41, 0 0 40px #00ff41, inset 0 0 40px #00ff4108`,
  } : {}

  return (
    <div style={{
      background:   T.surface,
      border:       `1px solid ${accent}33`,
      borderRadius: '3px',
      padding:      '16px 18px 18px',
      position:     'relative',
      overflow:     'hidden',
      boxShadow:    panelShadow(accent),
      transition:   'border-color 0.4s, box-shadow 0.4s',
      height:       '100%',
      display:      'flex',
      flexDirection:'column',
      ...agentStyle,
      ...style,
    }}>
      <div style={S.panelBar(accent)} />
      <div style={S.panelCorner('tl', accent)} />
      <div style={S.panelCorner('br', accent)} />

      {/* Drag handle title bar */}
      <div className="drag-handle" style={S.panelTitle}>
        <span style={S.dragHandle} title="Drag to move">⠿</span>
        {titleContent}
      </div>

      <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        {children}
      </div>
    </div>
  )
}

/* ─── GPU Power Monitor panel (amber) ───────────────────────── */
function GpuPowerPanel({ alive }) {
  const [gpuData, setGpuData] = useState(null)
  useEffect(() => {
    let mounted = true
    async function poll() {
      try {
        const r = await fetch('/stats?' + Date.now())
        if (r.ok) {
          const d = await r.json()
          if (mounted) setGpuData(d?.gpu ?? null)
        }
      } catch {}
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => { mounted = false; clearInterval(id) }
  }, [])

  const accent = T.amber
  const power  = gpuData?.power_draw_w ?? null
  const util   = gpuData?.utilization_pct ?? null
  const vram   = gpuData?.memory_used_mb ?? null
  const vramT  = gpuData?.memory_total_mb ?? null
  const temp   = gpuData?.temperature_c ?? null

  const rows = [
    { label: 'GPU.POWER',    value: power != null ? `${power.toFixed(0)}W` : 'N/A', color: accent },
    { label: 'GPU.UTIL',     value: util  != null ? `${util.toFixed(0)}%`  : 'N/A', color: T.cyan },
    { label: 'VRAM.USED',    value: vram  != null ? `${(vram/1024).toFixed(1)}GB` : 'N/A', color: T.green },
    { label: 'GPU.TEMP',     value: temp  != null ? `${temp}°C` : 'N/A',
      color: temp != null && temp > 80 ? T.red : temp != null && temp > 70 ? T.amber : T.green },
  ]

  return (
    <Panel accent={accent} titleContent={
      <>
        <span style={S.titleAccent(accent)}>⚡</span>
        <span>GPU POWER MONITOR</span>
        <span style={{ marginLeft: 'auto', ...S.pill(accent) }}>AMBER</span>
      </>
    }>
      {rows.map(({ label, value, color }) => (
        <div key={label} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '11px' }}>{label}</span>
          <span style={{ color, fontWeight: 700, textShadow: glow(color, 3), fontSize: '12px' }}>{value}</span>
        </div>
      ))}
      {power != null && (
        <div style={{ marginTop: '10px' }}>
          <div style={{ ...S.progressTrack }}>
            <div style={{ position:'absolute', top:0, left:0, height:'100%',
                          width:`${Math.min(100,(power/400)*100)}%`,
                          background: `linear-gradient(90deg, ${T.amber}, #ff6600)`,
                          boxShadow: `0 0 8px ${T.amber}`, transition:'width 1s ease' }} />
          </div>
          <div style={{ fontSize:'9px', color: T.textDim, letterSpacing:'0.1em',
                        marginTop:'4px', textAlign:'right' }}>
            {power.toFixed(0)}W / 400W TDP
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ─── SYSTEM STATUS panel ───────────────────────────────────── */
function MinerStatusPanel({ alive, currentTarget, minerId, lastUpdated }) {
  const uptime = useUptime(lastUpdated)
  const lines = [
    { label: 'SYS.STATUS',    value: alive ? 'ONLINE' : 'OFFLINE', color: alive ? T.green : T.red },
    { label: 'PROC.TARGET',   value: currentTarget || 'AWAITING_JOB', color: T.cyan },
    { label: 'NODE.UID',      value: minerId && minerId !== '—' ? `${minerId.slice(0,8)}…${minerId.slice(-4)}` : 'UNREGISTERED', color: T.purple },
    { label: 'SESSION.UPTIME',value: alive ? uptime : '--:--:--', color: T.green },
  ]
  return (
    <Panel accent={alive ? T.green : T.red} titleContent={
      <>
        <span style={S.titleAccent(T.green)}>◈</span>
        <span>SYSTEM STATUS</span>
        <span style={{ marginLeft: 'auto', ...S.pill(alive ? T.green : T.red) }}>
          {alive ? 'ONLINE' : 'OFFLINE'}
        </span>
      </>
    }>
      {lines.map(({ label, value, color }) => (
        <div key={label} style={S.kv}>
          <span style={{ color: T.textDim, fontSize: '11px' }}>{label}</span>
          <span style={{ color, fontWeight: 700, textShadow: glow(color, 3), fontSize: '12px' }}>{value}</span>
        </div>
      ))}
      <div style={{ marginTop: '10px', padding: '8px 10px', background: '#020902',
                    border: `1px solid ${T.border}`, fontSize: '10px', color: T.textDim,
                    letterSpacing: '0.06em', lineHeight: 1.8, flex: 1 }}>
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
    <Panel accent={T.cyan} titleContent={
      <>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>LIVE SCORING FEED</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.cyan) }}>LIVE · 10s</span>
      </>
    }>
      <div style={{ display: 'grid',
                    gridTemplateColumns: '70px 60px 1fr 72px 72px 42px 52px',
                    gap: '8px', padding: '4px 0 6px',
                    borderBottom: `1px solid ${T.border}`,
                    fontSize: '9px', color: T.textDim, letterSpacing: '0.14em',
                    flexShrink: 0 }}>
        <span>TIME</span><span>TARGET</span><span>SMILES</span>
        <span style={{ textAlign:'right' }}>BOLTZ</span>
        <span style={{ textAlign:'right' }}>AFF(kcal)</span>
        <span style={{ textAlign:'center' }}>HIT?</span>
        <span style={{ textAlign:'center' }}>SOURCE</span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {rows.length === 0 ? (
          <div style={{ color: T.textDim, fontSize: '11px', padding: '16px 0' }}>
            AWAITING FIRST BOLTZ2 EVALUATION…
            <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
          </div>
        ) : rows.map((r, i) => (
          <div key={i} style={{
            display:             'grid',
            gridTemplateColumns: '70px 60px 1fr 72px 72px 42px 52px',
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
            <span style={{ color: T.green, overflow: 'hidden', textOverflow: 'ellipsis',
                           whiteSpace: 'nowrap', fontSize: '10px', fontFamily: T.mono }}>
              {r.smiles || '—'}
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
    </Panel>
  )
}

/* ─── Molecules screened panel ──────────────────────────────── */
function MoleculesPanel({ count }) {
  const n = useAnimatedNumber(count)
  return (
    <Panel accent={T.green} titleContent={
      <>
        <span style={S.titleAccent(T.green)}>◉</span>
        <span>MOLECULES SCREENED</span>
      </>
    }>
      <div style={S.bigNum(T.green)}>{n.toLocaleString()}</div>
      <div style={{ ...S.label, marginBottom: '12px' }}>drug candidate evaluations completed</div>
      <div style={{ height: '2px', background: `linear-gradient(90deg, ${T.green}, ${T.cyan}, ${T.purple})`,
                    boxShadow: `0 0 8px ${T.green}`, borderRadius: '1px', flexShrink: 0 }} />
    </Panel>
  )
}

/* ─── $LIFE earned panel ────────────────────────────────────── */
function LifeEarnedPanel({ earned }) {
  const n = useAnimatedNumber(Math.floor(earned))
  return (
    <Panel accent={T.purple} titleContent={
      <>
        <span style={S.titleAccent(T.purple)}>✦</span>
        <span>$LIFE EARNED</span>
      </>
    }>
      <div style={S.bigNum(T.purple)}>{n.toLocaleString()}</div>
      <div style={{ fontSize: '18px', color: T.purpleDim, marginTop: '-4px', marginBottom: '8px',
                    textShadow: glow(T.purple, 3) }}>LIFE TOKENS</div>
      <div style={{ ...S.label, marginBottom: '12px' }}>minted on-chain for verified discoveries</div>
      <div style={{ height: '2px', background: `linear-gradient(90deg, ${T.purple}, ${T.cyan})`,
                    boxShadow: `0 0 8px ${T.purple}`, borderRadius: '1px', flexShrink: 0 }} />
    </Panel>
  )
}

/* ─── Cancer targets panel ──────────────────────────────────── */
function TargetsPanel({ targets }) {
  const GENES = ['TP53', 'BRCA1', 'EGFR', 'HER2', 'KRAS', 'BCL2', 'CDK4', 'VEGFR2', 'PDL1', 'MDM2']
  return (
    <Panel accent={T.cyan} titleContent={
      <>
        <span style={S.titleAccent(T.cyan)}>⬡</span>
        <span>ACTIVE PROTEIN TARGETS</span>
        <span style={{ marginLeft: 'auto', color: T.cyan, textShadow: glow(T.cyan, 3), fontWeight: 700 }}>
          {targets.length}
        </span>
      </>
    }>
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {targets.length === 0 ? (
          <div style={{ ...S.label, padding: '8px 0' }}>
            AWAITING FIRST TARGET ASSIGNMENT
            <span style={{ animation: 'blink 1s step-end infinite', color: T.green }}> █</span>
          </div>
        ) : (
          targets.map((t, i) => (
            <div key={i} style={S.targetItem}>
              <div style={S.targetPip} />
              <span style={{ color: T.cyan, fontWeight: 700, textShadow: glow(T.cyan, 2) }}>{t}</span>
              <span style={{ marginLeft: 'auto', ...S.pill(T.green) }}>ACTIVE</span>
            </div>
          ))
        )}
        {GENES.filter(g => !targets.includes(g)).slice(0, 3).map((g) => (
          <div key={g} style={{ ...S.targetItem, opacity: 0.3, border: `1px solid ${T.border}` }}>
            <div style={{ ...S.targetPip, background: T.textDim, boxShadow: 'none', animation: 'none' }} />
            <span style={{ color: T.textDim }}>{g}</span>
            <span style={{ marginLeft: 'auto', ...S.pill(T.textDim) }}>LOCKED</span>
          </div>
        ))}
      </div>
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

    ctx.strokeStyle = '#00ff4110'; ctx.lineWidth = 1
    for (let i = 0; i <= 4; i++) {
      const y = (i / 4) * h
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }

    const grad = ctx.createLinearGradient(0, 0, 0, h)
    grad.addColorStop(0, '#00ff4130'); grad.addColorStop(1, '#00ff4100')
    ctx.fillStyle = grad
    ctx.beginPath(); ctx.moveTo(toX(0), h)
    scores.forEach((s, i) => ctx.lineTo(toX(i), toY(s)))
    ctx.lineTo(toX(scores.length-1), h); ctx.closePath(); ctx.fill()

    ctx.strokeStyle = T.green; ctx.lineWidth = 2
    ctx.shadowColor = T.green; ctx.shadowBlur = 8
    ctx.beginPath()
    scores.forEach((s, i) => i === 0 ? ctx.moveTo(toX(i), toY(s)) : ctx.lineTo(toX(i), toY(s)))
    ctx.stroke()

    scores.forEach((s, i) => {
      ctx.fillStyle = T.cyan; ctx.shadowColor = T.cyan; ctx.shadowBlur = 6
      ctx.beginPath(); ctx.arc(toX(i), toY(s), 2.5, 0, Math.PI*2); ctx.fill()
    })
  }, [scores])

  const best = scores.length ? Math.max(...scores) : null
  const avg  = scores.length ? scores.reduce((a,b) => a+b, 0) / scores.length : null

  return (
    <Panel accent={T.green} titleContent={
      <>
        <span style={S.titleAccent(T.green)}>▲</span>
        <span>SCORING HISTORY — LAST 2H</span>
        <span style={{ marginLeft: 'auto', color: T.textDim, fontSize: '10px' }}>
          5m buckets · {scores.length} pts
        </span>
      </>
    }>
      <div style={{ display: 'flex', gap: '28px', marginBottom: '14px', flexShrink: 0 }}>
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

      <canvas ref={canvasRef} width={340} height={80} style={{ width: '100%', height: '80px',
        border: `1px solid ${T.border}`, background: '#020902', display: 'block', flexShrink: 0 }} />

      {history.length > 0 && (
        <div style={{ marginTop: '10px', flex: 1, overflowY: 'auto' }}>
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
    <Panel accent={T.cyan} titleContent={
      <>
        <span style={S.titleAccent(T.cyan)}>◈</span>
        <span>GLOBAL NETWORK MONITOR</span>
        <span style={{ marginLeft: 'auto', ...S.pill(T.cyan) }}>ON-CHAIN</span>
      </>
    }>
      {rows.map(({ label, value, color }) => (
        <div key={label} style={S.globalRow}>
          <span style={{ color: T.textDim, fontSize: '11px', letterSpacing: '0.1em' }}>{label}</span>
          <span style={{ fontSize: '20px', fontWeight: 700, color,
                          textShadow: glow(color, 4), fontVariantNumeric: 'tabular-nums' }}>
            {value}
          </span>
        </div>
      ))}
      <div style={{ marginTop: '10px', padding: '8px 10px', background: '#020a09',
                    border: `1px solid ${T.cyan}22`, fontSize: '10px', lineHeight: 1.8,
                    color: T.textDim, letterSpacing: '0.06em', flex: 1 }}>
        <span style={{ color: T.cyan }}>{'>'}</span> SOLANA DEVNET — RPC CONNECTED<br/>
        <span style={{ color: T.cyan }}>{'>'}</span> PROGRAM:{' '}
        <span style={{ color: T.green, fontSize: '9px' }}>DzcQHhTPuiq…WsKvJ</span><br/>
        <span style={{ color: T.cyan }}>{'>'}</span> CONSENSUS: 2-OF-N VALIDATORS
      </div>
    </Panel>
  )
}

/* ─── Private note ──────────────────────────────────────────── */
function PrivateNote() {
  return (
    <div style={{ fontSize: '9px', color: T.purple, background: '#0d0020',
                  border: `1px solid ${T.purple}33`, padding: '3px 8px',
                  marginBottom: '10px', letterSpacing: '0.1em', display: 'inline-block',
                  textShadow: glow(T.purple, 2), flexShrink: 0 }}>
      🔒 LOCAL DIAGNOSTICS — NOT BROADCAST
    </div>
  )
}

/* ─── LIFE PULSE panel ──────────────────────────────────────── */
function LifePulsePanel({ pulse }) {
  if (!pulse) return (
    <Panel accent={T.green} titleContent={
      <>
        <span style={S.titleAccent(T.green)}>⚡</span>
        <span>LIFE PULSE — MOLECULAR SWEEP ENGINE</span>
      </>
    }>
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
    <Panel accent={accent} titleContent={
      <>
        <span style={S.titleAccent(T.green)}>⚡</span>
        <span>LIFE PULSE — MOLECULAR SWEEP ENGINE</span>
        <span style={{ marginLeft: 'auto', ...S.pill(statusColor) }}>
          {active && <span style={{ display:'inline-block', width:6, height:6, borderRadius:'50%',
            background:T.green, boxShadow:glow(T.green,3), marginRight:5,
            animation:'blink 1.2s step-end infinite' }} />}
          {statusLabel}
        </span>
      </>
    }>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(2,1fr)', gap:'8px', marginBottom:'12px', flexShrink: 0 }}>
        {[
          { v: total_evaluated.toLocaleString(), l: 'MOLECULES EXPLORED', c: T.green },
          { v: sobol_index.toLocaleString(),     l: 'SOBOL INDEX',         c: T.cyan },
          { v: current_batch_size,               l: 'BATCH SIZE',          c: T.purple },
          { v: tanimoto_pass_rate != null ? `${tanimoto_pass_rate.toFixed(1)}%` : '—',
            l: 'DIVERSITY PASS', c: tanimoto_pass_rate != null && tanimoto_pass_rate < 30 ? T.red : T.green },
        ].map(({ v, l, c }) => (
          <div key={l} style={{ padding:'8px 10px', background:'#020902',
                                border:`1px solid ${c}22`, borderRadius:'2px' }}>
            <div style={{ fontSize:'20px', fontWeight:700, color:c, textShadow:glow(c,3),
                          fontVariantNumeric:'tabular-nums' }}>{v}</div>
            <div style={{ fontSize:'9px', color:T.textDim, letterSpacing:'0.12em',
                          marginTop:'2px' }}>{l}</div>
          </div>
        ))}
      </div>

      <div style={{ display:'flex', gap:'12px', marginBottom:'10px', fontSize:'11px',
                    flexShrink:0, flexWrap:'wrap', alignItems:'center' }}>
        <span style={{ color:T.textDim, fontSize:'10px' }}>MUTATIONS:</span>
        <span style={{ color:T.purple, fontWeight:700 }}>{mutant_attempted.toLocaleString()} attempted</span>
        <span style={{ color: mutant_accepted > 0 ? T.green : T.textDim, fontWeight:700 }}>
          {mutant_accepted.toLocaleString()} accepted
        </span>
        {mutant_attempted > 0 && (
          <span style={{ ...S.pill(T.cyan), fontSize:'9px' }}>
            {Math.round(mutant_accepted / mutant_attempted * 100)}% ACCEPT
          </span>
        )}
      </div>

      <div style={{ fontSize:'10px', color:T.textDim, letterSpacing:'0.14em',
                    marginBottom:'6px', flexShrink:0 }}>TOP CANDIDATES THIS SESSION</div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {top_molecules.length === 0 ? (
          <div style={{ color:T.textDim, fontSize:'11px', padding:'8px 0' }}>
            SWEEPING…<span style={{ animation:'blink 1s step-end infinite', color:T.green }}> █</span>
          </div>
        ) : top_molecules.map((m, i) => (
          <div key={i} style={{ ...S.terminalLine,
            background: i === 0 ? '#00ff4106' : 'transparent',
            padding: '6px 0',
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
      </div>
    </Panel>
  )
}

/* ─── LIFE AGENT helpers ────────────────────────────────────── */
function highlightCode(code, lang) {
  if (!lang) return code
  const e = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  let s = e(code)
  s = s.replace(/(&#39;|&quot;|`)(.*?)\1/g, '<span style="color:#ce9178">$1$2$1</span>')
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
  s = s.replace(/(#[^\n]*|\/\/[^\n]*)/g, '<span style="color:#6a9955">$1</span>')
  s = s.replace(/\b(\d+\.?\d*)\b/g, '<span style="color:#b5cea8">$1</span>')
  return s
}

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

function SaveFileButton({ filename, code }) {
  const [status, setStatus] = useState('idle')
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

function RestartMinerButton() {
  const [status, setStatus] = useState('idle')
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

function ChatBubble({ role, content }) {
  const isUser = role === 'user'
  const saveFileMatch = !isUser && content.match(/SAVE_FILE:\s*(\S+\.py)\s*$/m)
  const saveFilename  = saveFileMatch ? saveFileMatch[1] : null
  const displayContent = saveFilename
    ? content.replace(/\nSAVE_FILE:\s*\S+\.py\s*$/, '').replace(/SAVE_FILE:\s*\S+\.py\s*$/, '')
    : content

  const segs = parseContent(displayContent)
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

/* ─── LIFE AGENT panel ──────────────────────────────────────── */
function LifeAgentPanel() {
  const [configured, setConfigured] = useState(null)
  const [messages,   setMessages]   = useState([])
  const [input,      setInput]      = useState('')
  const [loading,    setLoading]    = useState(false)
  const [error,      setError]      = useState(null)
  const bottomRef = useRef()
  const inputRef  = useRef()

  useEffect(() => {
    fetch('/agent/status')
      .then(r => r.json())
      .then(d => setConfigured(d.configured))
      .catch(() => setConfigured(false))
  }, [])

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

  if (configured === null) return null

  return (
    <Panel
      accent={T.green}
      isAgent={true}
      titleContent={
        <>
          <span style={{ ...S.titleAccent(T.green), fontSize: '14px', textShadow: `0 0 12px ${T.green}, 0 0 24px ${T.green}` }}>◈</span>
          <span style={{ color: T.green, fontSize: '12px', fontWeight: 700, letterSpacing: '0.25em',
                         textShadow: `0 0 10px ${T.green}, 0 0 20px ${T.green}66` }}>
            LIFE AGENT
          </span>
          <span style={{ marginLeft: 'auto', ...S.pill(T.green),
                         boxShadow: `0 0 8px ${T.green}`, fontSize: '11px' }}>
            CLAUDE&nbsp;SONNET&nbsp;4&nbsp;·&nbsp;AI&nbsp;ASSISTANT
          </span>
        </>
      }
    >
      {!configured ? (
        <div style={{
          padding:      '28px',
          textAlign:    'center',
          border:       `1px dashed ${T.border}`,
          borderRadius: '2px',
          background:   '#020902',
        }}>
          <div style={{ fontSize:'28px', marginBottom:'12px', opacity:0.5 }}>🤖</div>
          <div style={{ fontSize:'13px', color: T.textDim, letterSpacing:'0.06em', lineHeight: 1.8 }}>
            Add <code style={{ color: T.cyan, background:'#020a0a', padding:'2px 7px',
                                border:`1px solid ${T.border}`, fontFamily: T.mono,
                                fontSize:'11px' }}>ANTHROPIC_API_KEY</code> to{' '}
            <code style={{ color: T.green, background:'#020902', padding:'2px 7px',
                            border:`1px solid ${T.border}`, fontFamily: T.mono,
                            fontSize:'11px' }}>.env</code>{' '}
            to activate LIFE AGENT
          </div>
          <div style={{ marginTop:'14px', fontSize:'10px', color: T.textDim, letterSpacing:'0.12em' }}>
            Then restart: <code style={{ color: T.greenDim, fontFamily: T.mono }}>pm2 restart life-dashboard</code>
          </div>
        </div>
      ) : (
        <>
          <div style={{
            flex:        1,
            overflowY:   'auto',
            padding:     '8px 4px',
            marginBottom:'12px',
            border:      `1px solid ${T.green}44`,
            background:  '#010d01',
            borderRadius:'2px',
            minHeight:   '200px',
          }}>
            {messages.length === 0 && !loading && (
              <div style={{ padding:'24px 16px', color: T.textDim, fontSize:'11px',
                            lineHeight: 2, letterSpacing:'0.06em' }}>
                <div style={{ color: T.green, marginBottom:'8px',
                              textShadow: `0 0 10px ${T.green}, 0 0 20px ${T.green}66`,
                              fontSize: '13px', fontWeight: 700 }}>
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

          <div style={{ display:'flex', gap:'8px', alignItems:'flex-end', flexShrink: 0 }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask LIFE AGENT anything… (Enter to send, Shift+Enter for newline)"
              rows={2}
              style={{
                flex:        1,
                background:  '#010d01',
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
              onFocus={e => { e.target.style.borderColor = T.green + 'aa' }}
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
          <div style={{ marginTop:'6px', fontSize:'9px', color: T.textDim,
                        letterSpacing:'0.1em', flexShrink: 0 }}>
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

  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: #050a05; }
  ::-webkit-scrollbar-thumb { background: #00ff4133; border-radius: 0; }
  ::-webkit-scrollbar-thumb:hover { background: #00ff4166; }

  /* ── react-grid-layout biopunk overrides ── */
  .react-grid-item {
    transition: none !important;
  }
  .react-grid-item.react-grid-placeholder {
    background: #00ff4112 !important;
    border: 1px dashed #00ff4166 !important;
    border-radius: 3px !important;
    opacity: 1 !important;
  }
  /* resize handle — biopunk corner tick */
  .react-resizable-handle {
    opacity: 0;
    transition: opacity 0.2s;
  }
  .react-grid-item:hover .react-resizable-handle {
    opacity: 1;
  }
  .react-resizable-handle::after {
    border-color: #00ff4188 !important;
    width: 8px !important;
    height: 8px !important;
  }
  /* drag handle cursor */
  .drag-handle { cursor: grab !important; }
  .drag-handle:active { cursor: grabbing !important; }
  .react-draggable-dragging .drag-handle { cursor: grabbing !important; }

  /* reset layout button */
  .reset-btn {
    background: transparent;
    border: 1px solid #00ff4144;
    color: #5a9a5a;
    font-family: 'Courier New', monospace;
    font-size: 10px;
    letter-spacing: 0.14em;
    padding: 4px 14px;
    cursor: pointer;
    border-radius: 2px;
    transition: all 0.2s;
    text-transform: uppercase;
  }
  .reset-btn:hover {
    border-color: #00ff41aa;
    color: #00ff41;
    text-shadow: 0 0 8px #00ff41;
    background: #00ff4108;
  }

  /* mobile stack */
  @media (max-width: 640px) {
    .react-grid-layout {
      height: auto !important;
    }
    .react-grid-item {
      position: relative !important;
      transform: none !important;
      width: 100% !important;
      height: auto !important;
      min-height: 200px;
      margin-bottom: 12px;
    }
    .react-resizable-handle { display: none; }
  }
`

/* ─── App ───────────────────────────────────────────────────── */
export default function App() {
  const [pub,   setPub]   = useState(null)
  const [feed,  setFeed]  = useState([])
  const [pulse, setPulse] = useState(null)
  const [tick,  setTick]  = useState(null)
  const [layout, setLayout] = useState(loadLayout)
  const winWidth = useWindowWidth()

  // Persist layout changes
  const onLayoutChange = useCallback((newLayout) => {
    setLayout(newLayout)
    saveLayout(newLayout)
  }, [])

  const resetLayout = useCallback(() => {
    setLayout(DEFAULT_LAYOUT)
    saveLayout(DEFAULT_LAYOUT)
  }, [])

  // Stats poll — every 5s
  useEffect(() => {
    async function poll() {
      try { const r = await fetch('/stats?' + Date.now()); if (r.ok) setPub(await r.json()) } catch {}
      setTick(new Date())
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [])

  // Feed poll — every 10s
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

  const alive   = pub?.alive              ?? false
  const mols    = pub?.molecules_screened ?? 0
  const life    = pub?.life_earned        ?? 0
  const tgts    = pub?.targets_contributed ?? []
  const network = pub?.network            ?? {}
  const scoring = pub?.scoring_history    ?? []
  const target  = pub?.current_target     ?? '—'
  const minerId = pub?.miner_id           ?? '—'
  const lastUpd = pub?.last_updated       ?? null

  // Grid width = full window minus outer padding
  const gridWidth = Math.max(winWidth - 32, 320)

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
              <DNAHelix />
              <div style={{
                fontSize:      '26px',
                fontWeight:    700,
                color:         T.green,
                letterSpacing: '0.06em',
                textTransform: 'uppercase',
                textShadow:    `0 0 20px ${T.green}, 0 0 40px ${T.green}88, 0 0 80px ${T.green}44`,
                textAlign:     'center',
                animation:     'textPulse 3s ease-in-out infinite',
              }}>
                ▓▒░ YOUR GPU IS FIGHTING CANCER ░▒▓
              </div>
              <div style={{
                fontSize:      '11px',
                color:         T.cyan,
                letterSpacing: '0.2em',
                textTransform: 'uppercase',
                textShadow:    glow(T.cyan, 4),
              }}>
                LIFE COMPUTE — DECENTRALIZED DRUG DISCOVERY NETWORK
              </div>
              <div style={{ fontSize: '10px', color: T.textDim, letterSpacing: '0.16em' }}>
                POWERED BY BOLTZ2 MOLECULAR DOCKING · SOLANA BLOCKCHAIN
              </div>
              {/* Reset Layout button */}
              <button className="reset-btn" onClick={resetLayout}>
                ⊞ RESET LAYOUT
              </button>
            </div>
            <div style={{
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
            }}>
              <div style={{
                width:     '6px',
                height:    '6px',
                borderRadius: '50%',
                background: alive ? T.green : T.red,
                boxShadow:  glow(alive ? T.green : T.red, 4),
                animation:  alive ? 'blink 1.2s step-end infinite' : 'none',
              }} />
              {alive ? 'SYS:ONLINE' : 'SYS:OFFLINE'}
            </div>
          </header>

          {/* ── Drag-and-drop grid ── */}
          <div style={{ padding: '16px' }}>
            <GridLayout
              layout={layout}
              cols={COLS}
              rowHeight={ROW_HEIGHT}
              width={gridWidth}
              margin={MARGIN}
              containerPadding={[0, 0]}
              onLayoutChange={onLayoutChange}
              draggableHandle=".drag-handle"
              compactType={null}
              preventCollision={false}
              isResizable={true}
              isDraggable={true}
              useCSSTransforms={true}
              resizeHandles={['se', 's', 'e']}
            >
              <div key="system-status">
                <MinerStatusPanel
                  alive={alive} currentTarget={target}
                  minerId={minerId} lastUpdated={lastUpd}
                />
              </div>

              <div key="molecules">
                <MoleculesPanel count={mols} />
              </div>

              <div key="life-earned">
                <LifeEarnedPanel earned={life} />
              </div>

              <div key="gpu-power">
                <GpuPowerPanel alive={alive} />
              </div>

              <div key="live-feed">
                <LiveScoringFeedPanel feed={feed} />
              </div>

              <div key="targets">
                <TargetsPanel targets={tgts} />
              </div>

              <div key="network">
                <NetworkPanel network={network} />
              </div>

              <div key="life-pulse">
                <LifePulsePanel pulse={pulse} />
              </div>

              <div key="scoring-history">
                <ScoringHistoryPanel history={scoring} />
              </div>

              <div key="life-agent">
                <LifeAgentPanel />
              </div>
            </GridLayout>
          </div>

          {/* ── Footer ── */}
          <footer style={{
            borderTop:     `1px solid ${T.border}`,
            padding:       '12px 28px',
            display:       'flex',
            justifyContent:'space-between',
            alignItems:    'center',
            fontSize:      '10px',
            color:         T.textDim,
            letterSpacing: '0.1em',
          }}>
            <span>LIFE-COMPUTE MINER v2.0.0 // BIOPUNK EDITION</span>
            <span style={{ color: T.green, textShadow: glow(T.green, 2) }}>
              {tick ? `LAST_SYNC: ${tick.toLocaleTimeString()}` : 'CONNECTING…'}
            </span>
            <span style={{ color: T.textDim, fontSize: '9px' }}>
              DRAG ⠿ TO MOVE · CORNER TO RESIZE · LAYOUT AUTO-SAVED
            </span>
          </footer>
        </div>
      </div>
    </>
  )
}
