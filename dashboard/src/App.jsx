import { useState, useEffect, useRef, useCallback } from 'react'
import GridLayout from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'

/* ─── Theme tokens ──────────────────────────────────────────── */
const T = {
  bg:       '#050a05', surface:  '#080f08',
  border:   '#00ff4133',
  green:    '#00ff41', greenDim: '#00cc33',
  cyan:     '#00ffff', cyanDim:  '#00cccc',
  purple:   '#9d00ff', purpleDim:'#7700cc',
  amber:    '#ff8c00', amberDim: '#cc6600',
  red:      '#ff003c',
  textDim:  '#5a9a5a', text:     '#aaddaa', textBright: '#ccffcc',
  mono:     "'Courier New', 'Source Code Pro', monospace",
}

/* ─── Grid constants ────────────────────────────────────────── */
const COLS       = 12
const ROW_HEIGHT = 30
const MARGIN     = [8, 8]
const LS_KEY     = 'life-dashboard-layout-v4'

/*
 * Compact default — fits 1920×1080 without scrolling.
 * Total grid rows = 21 → height = 21×30 + 20×8 = 790px
 * + compact header ~50px + footer ~40px + padding 16px = ~896px  ✓
 *
 * Row 1 (y=0, h=5):  stats strip — 5 panels
 * Row 2-3 (y=5):     live-feed (5col,h=8) | targets (3col,h=4) | pulse (4col,h=4)
 * Row 3 cont (y=9):  live-feed still running | scoring-history (7col,h=4)
 * Row 4 (y=13, h=8): life-agent full width
 */
const DEFAULT_LAYOUT = [
  { i: 'system-status',   x: 0,  y: 0,  w: 2,  h: 5,  minW: 1, minH: 3 },
  { i: 'molecules',       x: 2,  y: 0,  w: 2,  h: 5,  minW: 1, minH: 3 },
  { i: 'life-earned',     x: 4,  y: 0,  w: 2,  h: 5,  minW: 1, minH: 3 },
  { i: 'gpu-power',       x: 6,  y: 0,  w: 2,  h: 5,  minW: 1, minH: 3 },
  { i: 'network',         x: 8,  y: 0,  w: 4,  h: 5,  minW: 2, minH: 3 },
  { i: 'live-feed',       x: 0,  y: 5,  w: 5,  h: 8,  minW: 2, minH: 3 },
  { i: 'targets',         x: 5,  y: 5,  w: 3,  h: 4,  minW: 1, minH: 3 },
  { i: 'life-pulse',      x: 8,  y: 5,  w: 4,  h: 4,  minW: 2, minH: 3 },
  { i: 'scoring-history', x: 5,  y: 9,  w: 7,  h: 4,  minW: 2, minH: 3 },
  { i: 'life-agent',      x: 0,  y: 13, w: 12, h: 8,  minW: 4, minH: 4 },
]

function loadLayout() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return DEFAULT_LAYOUT
    const saved = JSON.parse(raw)
    const def = Object.fromEntries(DEFAULT_LAYOUT.map(d => [d.i, d]))
    return saved.map(s => ({ ...def[s.i], ...s })).filter(s => def[s.i])
  } catch { return DEFAULT_LAYOUT }
}
function saveLayout(l) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(l)) } catch {}
}

/* ─── Helpers ───────────────────────────────────────────────── */
const glow = (c, s = 8) => `0 0 ${s}px ${c}, 0 0 ${s*2}px ${c}44`

/* ─── Styles ────────────────────────────────────────────────── */
const S = {
  wrap: { minHeight:'100vh', background:T.bg, color:T.text, fontFamily:T.mono,
          position:'relative', overflow:'hidden' },
  scanlines: { position:'fixed', top:0, left:0, right:0, bottom:0, pointerEvents:'none',
               zIndex:1000,
               background:'repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.08) 2px,rgba(0,0,0,0.08) 4px)' },
  matrixCanvas: { position:'fixed', top:0, left:0, width:'100%', height:'100%',
                  opacity:0.05, pointerEvents:'none', zIndex:0 },
  content: { position:'relative', zIndex:1 },

  panelTitle: { fontSize:'10px', color:T.textDim, letterSpacing:'0.18em',
                textTransform:'uppercase', marginBottom:'8px', display:'flex',
                alignItems:'center', gap:'6px', cursor:'grab', userSelect:'none',
                flexShrink:0 },
  dragHandle: { fontSize:'13px', color:T.textDim, opacity:0.6, flexShrink:0, lineHeight:1 },
  titleAccent: c => ({ color:c, textShadow:glow(c,3) }),

  bigNum: c => ({ fontSize:'44px', fontWeight:700, color:c, lineHeight:1,
                  textShadow:`0 0 20px ${c}, 0 0 40px ${c}66`, marginBottom:'4px',
                  fontVariantNumeric:'tabular-nums', letterSpacing:'-0.02em',
                  animation:'textPulse 4s ease-in-out infinite' }),
  label: { fontSize:'10px', color:T.textDim, letterSpacing:'0.06em' },
  kv: { display:'flex', justifyContent:'space-between', alignItems:'center',
        padding:'5px 0', borderBottom:`1px solid ${T.border}`, fontSize:'11px' },
  pill: c => ({ background:c+'15', border:`1px solid ${c}44`, color:c,
                textShadow:glow(c,2), borderRadius:'2px', padding:'1px 6px',
                fontSize:'9px', fontWeight:700, letterSpacing:'0.1em', fontFamily:T.mono }),
  terminalLine: { display:'flex', gap:'6px', alignItems:'center', padding:'4px 0',
                  borderBottom:`1px solid #0a150a`, fontSize:'10px' },
  smiles: { color:T.cyan, textShadow:glow(T.cyan,2), overflow:'hidden',
            textOverflow:'ellipsis', whiteSpace:'nowrap', flex:1,
            fontSize:'9px', fontFamily:T.mono },
  targetItem: { display:'flex', alignItems:'center', gap:'8px', padding:'6px 10px',
                background:'#020902', border:`1px solid ${T.border}`, borderRadius:'2px',
                fontSize:'11px', marginBottom:'6px' },
  progressTrack: { height:'3px', background:'#0a1a0a', border:`1px solid ${T.border}`,
                   overflow:'hidden', position:'relative', marginTop:'4px' },
  globalRow: { display:'flex', justifyContent:'space-between', alignItems:'center',
               padding:'8px 0', borderBottom:`1px solid ${T.border}` },
  statusBadge: alive => ({
    display:'flex', alignItems:'center', gap:'6px', fontSize:'10px',
    color: alive ? T.green : T.red, letterSpacing:'0.1em',
    textShadow: glow(alive ? T.green : T.red, 3),
    border:`1px solid ${alive ? T.green : T.red}55`, padding:'3px 10px',
    borderRadius:'2px', background: alive ? '#00ff4108' : '#ff003c08',
  }),
  statusDot: alive => ({
    width:'5px', height:'5px', borderRadius:'50%',
    background: alive ? T.green : T.red,
    boxShadow: glow(alive ? T.green : T.red, 4),
    animation: alive ? 'blink 1.2s step-end infinite' : 'none',
  }),
}

/* ─── Matrix rain ───────────────────────────────────────────── */
function MatrixRain() {
  const ref = useRef()
  useEffect(() => {
    const canvas = ref.current; if (!canvas) return
    const ctx = canvas.getContext('2d')
    const chars = 'ATCGAUCG01アイウエオカキ'
    let w, h, cols, drops
    const resize = () => {
      w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight
      cols = Math.floor(w/16); drops = Array(cols).fill(0).map(() => Math.random()*-50)
    }
    resize(); window.addEventListener('resize', resize)
    const tick = () => {
      ctx.fillStyle='rgba(5,10,5,0.12)'; ctx.fillRect(0,0,w,h)
      ctx.font='13px "Courier New",monospace'
      drops.forEach((y,i) => {
        const ch = chars[Math.floor(Math.random()*chars.length)]
        ctx.fillStyle = y*16 < 80 ? '#00ffff' : '#00ff41'
        ctx.fillText(ch, i*16, y*16)
        if (y*16 > h && Math.random() > 0.975) drops[i] = 0; else drops[i] += 0.4
      })
    }
    const id = setInterval(tick, 50)
    return () => { clearInterval(id); window.removeEventListener('resize', resize) }
  }, [])
  return <canvas ref={ref} style={S.matrixCanvas} />
}

/* ─── Hooks ─────────────────────────────────────────────────── */
function useUptime(lastUpdated) {
  const [uptime, setUptime] = useState(0)
  useEffect(() => {
    const tick = () => {
      if (!lastUpdated) return
      setUptime(Math.max(0, Math.floor((Date.now() - new Date(lastUpdated).getTime() + 60000) / 1000)))
    }
    tick(); const id = setInterval(tick, 1000); return () => clearInterval(id)
  }, [lastUpdated])
  const h = Math.floor(uptime/3600), m = Math.floor((uptime%3600)/60), s = uptime%60
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
}

function useAnimatedNumber(target, dur=800) {
  const [disp, setDisp] = useState(target)
  const prev = useRef(target), raf = useRef(null)
  useEffect(() => {
    const from = prev.current, diff = target - from; if (!diff) return
    const t0 = performance.now()
    const step = now => {
      const t = Math.min((now-t0)/dur, 1), e = t<.5 ? 2*t*t : -1+(4-2*t)*t
      setDisp(Math.round(from + diff*e))
      if (t<1) raf.current = requestAnimationFrame(step)
      else { prev.current = target; setDisp(target) }
    }
    raf.current = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf.current)
  }, [target, dur])
  return disp
}

function useWindowWidth() {
  const [w, setW] = useState(window.innerWidth)
  useEffect(() => {
    const fn = () => setW(window.innerWidth)
    window.addEventListener('resize', fn); return () => window.removeEventListener('resize', fn)
  }, [])
  return w
}

/* ─── Panel — electric border + circuit corner dots ─────────── */
function Panel({ titleContent, isAgent=false, isGpu=false, children }) {
  const accent = isGpu ? T.amber : T.green
  const cls    = isAgent ? 'panel-agent' : isGpu ? 'panel-amber' : 'panel-electric'
  return (
    <div className={cls} style={{
      background:    isAgent ? '#0a1a0a' : T.surface,
      border:        `2px solid ${accent}`,
      borderRadius:  '3px',
      padding:       '10px 12px 10px',
      position:      'relative',
      overflow:      'hidden',
      height:        '100%',
      display:       'flex',
      flexDirection: 'column',
    }}>
      {/* Circuit-board corner dots */}
      {['tl','tr','bl','br'].map(p => (
        <div key={p} style={{
          position:  'absolute',
          [p[0]==='t'?'top':'bottom']: '2px',
          [p[1]==='l'?'left':'right']: '2px',
          width:     '5px', height: '5px',
          background: accent,
          boxShadow:  `0 0 6px ${accent}, 0 0 12px ${accent}`,
          zIndex:     2, pointerEvents: 'none',
        }} />
      ))}

      {/* Drag-handle title */}
      <div className="drag-handle" style={S.panelTitle}>
        <span style={S.dragHandle}>⠿</span>
        {titleContent}
      </div>

      {/* Content */}
      <div style={{ flex:1, overflow:'hidden', display:'flex', flexDirection:'column', minHeight:0 }}>
        {children}
      </div>
    </div>
  )
}

/* ─── GPU Power Monitor (amber) ─────────────────────────────── */
function GpuPowerPanel() {
  const [gpu, setGpu] = useState(null)
  useEffect(() => {
    const poll = async () => {
      try { const r = await fetch('/stats?'+Date.now()); if (r.ok) { const d = await r.json(); setGpu(d?.gpu??null) } } catch {}
    }
    poll(); const id = setInterval(poll,5000); return ()=>clearInterval(id)
  }, [])
  const pct  = gpu?.power_draw_w != null ? Math.min(100,(gpu.power_draw_w/400)*100) : null
  const rows = [
    { l:'GPU.POWER', v: gpu?.power_draw_w != null ? `${gpu.power_draw_w.toFixed(0)}W` : 'N/A', c:T.amber },
    { l:'GPU.UTIL',  v: gpu?.utilization_pct != null ? `${gpu.utilization_pct.toFixed(0)}%` : 'N/A', c:T.cyan },
    { l:'VRAM',      v: gpu?.memory_used_mb != null ? `${(gpu.memory_used_mb/1024).toFixed(1)}GB` : 'N/A', c:T.green },
    { l:'TEMP',      v: gpu?.temperature_c != null ? `${gpu.temperature_c}°C` : 'N/A',
      c: gpu?.temperature_c > 80 ? T.red : gpu?.temperature_c > 70 ? T.amber : T.green },
  ]
  return (
    <Panel isGpu titleContent={
      <><span style={S.titleAccent(T.amber)}>⚡</span><span>GPU POWER</span>
        <span style={{marginLeft:'auto',...S.pill(T.amber)}}>AMBER</span></>
    }>
      {rows.map(({l,v,c}) => (
        <div key={l} style={S.kv}>
          <span style={{color:T.textDim,fontSize:'10px'}}>{l}</span>
          <span style={{color:c,fontWeight:700,textShadow:glow(c,3),fontSize:'11px'}}>{v}</span>
        </div>
      ))}
      {pct != null && (
        <div style={{marginTop:'6px'}}>
          <div style={S.progressTrack}>
            <div style={{position:'absolute',top:0,left:0,height:'100%',width:`${pct}%`,
                         background:`linear-gradient(90deg,${T.amber},#ff4400)`,
                         boxShadow:`0 0 6px ${T.amber}`,transition:'width 1s ease'}} />
          </div>
          <div style={{fontSize:'9px',color:T.textDim,textAlign:'right',marginTop:'2px',letterSpacing:'0.08em'}}>
            {gpu.power_draw_w.toFixed(0)}W / 400W
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ─── System Status ─────────────────────────────────────────── */
function MinerStatusPanel({ alive, currentTarget, minerId, lastUpdated }) {
  const uptime = useUptime(lastUpdated)
  const rows = [
    { l:'STATUS',  v: alive?'ONLINE':'OFFLINE', c: alive?T.green:T.red },
    { l:'TARGET',  v: currentTarget||'AWAITING', c:T.cyan },
    { l:'NODE',    v: minerId&&minerId!=='—'?`${minerId.slice(0,6)}…${minerId.slice(-4)}`:'UNREG', c:T.purple },
    { l:'UPTIME',  v: alive?uptime:'--:--:--', c:T.green },
  ]
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.green)}>◈</span><span>SYSTEM STATUS</span>
        <span style={{marginLeft:'auto',...S.pill(alive?T.green:T.red)}}>{alive?'ON':'OFF'}</span></>
    }>
      {rows.map(({l,v,c}) => (
        <div key={l} style={S.kv}>
          <span style={{color:T.textDim,fontSize:'10px'}}>{l}</span>
          <span style={{color:c,fontWeight:700,textShadow:glow(c,2),fontSize:'11px'}}>{v}</span>
        </div>
      ))}
      <div style={{marginTop:'8px',padding:'6px 8px',background:'#020902',border:`1px solid ${T.border}`,
                   fontSize:'9px',color:T.textDim,letterSpacing:'0.06em',lineHeight:1.7,flex:1}}>
        <span style={{color:T.green}}>{'>'}</span> LIFE-COMPUTE v2.0.0<br/>
        <span style={{color:T.green}}>{'>'}</span> BOLTZ2 <span style={{color:T.cyan}}>ACTIVE</span>
      </div>
    </Panel>
  )
}

/* ─── Live Scoring Feed ─────────────────────────────────────── */
function LiveScoringFeedPanel({ feed }) {
  const rows = feed ?? []
  const srcColor = s => s==='ref'?T.purple : s==='generated'?T.cyan : T.greenDim
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.cyan)}>⬡</span><span>LIVE SCORING FEED</span>
        <span style={{marginLeft:'auto',...S.pill(T.cyan)}}>LIVE</span></>
    }>
      <div style={{display:'grid',gridTemplateColumns:'55px 50px 1fr 60px 42px 46px',gap:'6px',
                   padding:'3px 0 5px',borderBottom:`1px solid ${T.border}`,
                   fontSize:'8px',color:T.textDim,letterSpacing:'0.12em',flexShrink:0}}>
        <span>TIME</span><span>TARGET</span><span>SMILES</span>
        <span style={{textAlign:'right'}}>BOLTZ</span>
        <span style={{textAlign:'center'}}>HIT?</span>
        <span style={{textAlign:'center'}}>SRC</span>
      </div>
      <div style={{flex:1,overflowY:'auto'}}>
        {rows.length===0
          ? <div style={{color:T.textDim,fontSize:'10px',padding:'10px 0'}}>
              AWAITING…<span style={{animation:'blink 1s step-end infinite',color:T.green}}> █</span>
            </div>
          : rows.map((r,i) => (
            <div key={i} style={{display:'grid',gridTemplateColumns:'55px 50px 1fr 60px 42px 46px',
                                  gap:'6px',padding:'4px 0',borderBottom:`1px solid #0a150a`,
                                  fontSize:'10px',alignItems:'center',
                                  background:i===0?'#00ff4106':'transparent'}}>
              <span style={{color:T.textDim,fontSize:'9px'}}>{r.ts?r.ts.slice(11,19):'—'}</span>
              <span style={{color:T.cyan,fontWeight:700}}>{r.target_id}</span>
              <span style={{color:T.green,overflow:'hidden',textOverflow:'ellipsis',
                             whiteSpace:'nowrap',fontSize:'9px'}}>{r.smiles||'—'}</span>
              <span style={{color:r.boltz_score!=null?T.cyan:T.textDim,textAlign:'right',
                             fontVariantNumeric:'tabular-nums'}}>
                {r.boltz_score!=null?r.boltz_score.toFixed(4):'—'}
              </span>
              <span style={{textAlign:'center'}}>
                <span style={S.pill(r.hit?T.green:T.red)}>{r.hit?'HIT':'MISS'}</span>
              </span>
              <span style={{textAlign:'center'}}>
                <span style={{...S.pill(srcColor(r.source)),fontSize:'8px'}}>
                  {(r.source||'?').slice(0,5)}
                </span>
              </span>
            </div>
          ))
        }
      </div>
    </Panel>
  )
}

/* ─── Molecules Screened ────────────────────────────────────── */
function MoleculesPanel({ count }) {
  const n = useAnimatedNumber(count)
  return (
    <Panel titleContent={<><span style={S.titleAccent(T.green)}>◉</span><span>MOLECULES SCREENED</span></>}>
      <div style={S.bigNum(T.green)}>{n.toLocaleString()}</div>
      <div style={{...S.label,marginBottom:'8px'}}>drug candidate evaluations</div>
      <div style={{height:'2px',background:`linear-gradient(90deg,${T.green},${T.cyan},${T.purple})`,
                   boxShadow:`0 0 8px ${T.green}`,flexShrink:0}} />
    </Panel>
  )
}

/* ─── $LIFE Earned ──────────────────────────────────────────── */
function LifeEarnedPanel({ earned }) {
  const n = useAnimatedNumber(Math.floor(earned))
  return (
    <Panel titleContent={<><span style={S.titleAccent(T.purple)}>✦</span><span>$LIFE EARNED</span></>}>
      <div style={S.bigNum(T.purple)}>{n.toLocaleString()}</div>
      <div style={{fontSize:'14px',color:'#7700cc',marginTop:'-2px',marginBottom:'4px',
                   textShadow:glow(T.purple,3)}}>LIFE TOKENS</div>
      <div style={{...S.label,marginBottom:'8px'}}>minted on-chain</div>
      <div style={{height:'2px',background:`linear-gradient(90deg,${T.purple},${T.cyan})`,
                   boxShadow:`0 0 8px ${T.purple}`,flexShrink:0}} />
    </Panel>
  )
}

/* ─── Active Targets ────────────────────────────────────────── */
function TargetsPanel({ targets }) {
  const LOCKED = ['TP53','BRCA1','EGFR','HER2','KRAS'].filter(g => !targets.includes(g))
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.cyan)}>⬡</span><span>PROTEIN TARGETS</span>
        <span style={{marginLeft:'auto',color:T.cyan,textShadow:glow(T.cyan,3),fontWeight:700}}>
          {targets.length}
        </span></>
    }>
      <div style={{flex:1,overflowY:'auto'}}>
        {targets.length===0
          ? <div style={{...S.label,padding:'6px 0'}}>
              AWAITING<span style={{animation:'blink 1s step-end infinite',color:T.green}}> █</span>
            </div>
          : targets.map((t,i) => (
            <div key={i} style={S.targetItem}>
              <div style={{width:'5px',height:'5px',background:T.green,
                           boxShadow:glow(T.green,4),flexShrink:0,animation:'blink 2s step-end infinite'}} />
              <span style={{color:T.cyan,fontWeight:700,fontSize:'11px'}}>{t}</span>
              <span style={{marginLeft:'auto',...S.pill(T.green)}}>ACTIVE</span>
            </div>
          ))
        }
        {LOCKED.slice(0,2).map(g => (
          <div key={g} style={{...S.targetItem,opacity:0.3}}>
            <div style={{width:'5px',height:'5px',background:T.textDim,flexShrink:0}} />
            <span style={{color:T.textDim,fontSize:'11px'}}>{g}</span>
            <span style={{marginLeft:'auto',...S.pill(T.textDim)}}>LOCKED</span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/* ─── Scoring History ───────────────────────────────────────── */
function ScoringHistoryPanel({ history }) {
  const canvasRef = useRef()
  const scores = (history??[]).filter(r=>r.best_score!=null).map(r=>r.best_score)

  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas||scores.length<2) return
    const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height
    ctx.clearRect(0,0,w,h)
    const mn = Math.min(...scores)*.95, mx = Math.max(...scores)*1.05
    const toY = v => h - ((v-mn)/(mx-mn))*(h-6) - 3
    const toX = i => (i/(scores.length-1))*w
    ctx.strokeStyle='#00ff4110'; ctx.lineWidth=1
    for (let i=0;i<=4;i++){const y=(i/4)*h;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
    const grad = ctx.createLinearGradient(0,0,0,h)
    grad.addColorStop(0,'#00ff4130'); grad.addColorStop(1,'#00ff4100')
    ctx.fillStyle=grad; ctx.beginPath(); ctx.moveTo(toX(0),h)
    scores.forEach((s,i) => ctx.lineTo(toX(i),toY(s)))
    ctx.lineTo(toX(scores.length-1),h); ctx.closePath(); ctx.fill()
    ctx.strokeStyle=T.green; ctx.lineWidth=2; ctx.shadowColor=T.green; ctx.shadowBlur=8
    ctx.beginPath(); scores.forEach((s,i)=>i===0?ctx.moveTo(toX(i),toY(s)):ctx.lineTo(toX(i),toY(s))); ctx.stroke()
    scores.forEach((s,i)=>{ctx.fillStyle=T.cyan;ctx.shadowColor=T.cyan;ctx.shadowBlur=6;ctx.beginPath();ctx.arc(toX(i),toY(s),2,0,Math.PI*2);ctx.fill()})
  }, [scores])

  const best = scores.length ? Math.max(...scores) : null
  const avg  = scores.length ? scores.reduce((a,b)=>a+b,0)/scores.length : null
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.green)}>▲</span><span>SCORING HISTORY</span>
        <span style={{marginLeft:'auto',color:T.textDim,fontSize:'9px'}}>{scores.length} pts</span></>
    }>
      <div style={{display:'flex',gap:'20px',marginBottom:'8px',flexShrink:0}}>
        <div>
          <div style={{fontSize:'20px',fontWeight:700,color:T.green,lineHeight:1,textShadow:glow(T.green,4)}}>
            {best!=null?best.toFixed(4):'—'}
          </div>
          <div style={S.label}>peak</div>
        </div>
        <div>
          <div style={{fontSize:'20px',fontWeight:700,color:T.cyan,lineHeight:1,textShadow:glow(T.cyan,3)}}>
            {avg!=null?avg.toFixed(4):'—'}
          </div>
          <div style={S.label}>mean</div>
        </div>
      </div>
      <canvas ref={canvasRef} width={340} height={60} style={{width:'100%',height:'60px',
        border:`1px solid ${T.border}`,background:'#020902',display:'block',flexShrink:0}} />
      <div style={{flex:1,overflowY:'auto',marginTop:'6px'}}>
        {history.slice().reverse().slice(0,5).map((r,i) => (
          <div key={i} style={{display:'grid',gridTemplateColumns:'70px 1fr 60px',gap:'6px',
                                padding:'3px 0',borderBottom:`1px solid #0a150a`,
                                fontSize:'10px',alignItems:'center'}}>
            <span style={{color:T.textDim}}>{r.ts_iso?.slice(11,19)??'?'}</span>
            <span style={{color:T.cyan,fontSize:'10px'}}>[{r.target_id??'?'}]</span>
            <span style={{color:r.best_score!=null?T.green:T.textDim,fontWeight:700,textAlign:'right'}}>
              {r.best_score!=null?r.best_score.toFixed(4):'—'}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/* ─── Global Network ────────────────────────────────────────── */
function NetworkPanel({ network }) {
  const fmt = v => v!=null?v.toLocaleString():'—'
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.cyan)}>◈</span><span>GLOBAL NETWORK</span>
        <span style={{marginLeft:'auto',...S.pill(T.cyan)}}>ON-CHAIN</span></>
    }>
      {[
        {l:'MINERS_ONLINE',   v:fmt(network?.total_miners),      c:T.green},
        {l:'GLOBAL_SCREENED', v:fmt(network?.molecules_screened), c:T.cyan},
        {l:'CONFIRMED_HITS',  v:fmt(network?.targets_solved),     c:T.purple},
      ].map(({l,v,c}) => (
        <div key={l} style={S.globalRow}>
          <span style={{color:T.textDim,fontSize:'10px',letterSpacing:'0.08em'}}>{l}</span>
          <span style={{fontSize:'18px',fontWeight:700,color:c,textShadow:glow(c,4),
                         fontVariantNumeric:'tabular-nums'}}>{v}</span>
        </div>
      ))}
      <div style={{marginTop:'8px',padding:'6px 8px',background:'#020a09',
                   border:`1px solid ${T.cyan}22`,fontSize:'9px',lineHeight:1.8,
                   color:T.textDim,letterSpacing:'0.06em',flex:1}}>
        <span style={{color:T.cyan}}>{'>'}</span> SOLANA DEVNET · RPC OK<br/>
        <span style={{color:T.cyan}}>{'>'}</span> PROGRAM:{' '}
        <span style={{color:T.green,fontSize:'8px'}}>DzcQH…WsKvJ</span>
      </div>
    </Panel>
  )
}

/* ─── LIFE PULSE ────────────────────────────────────────────── */
function LifePulsePanel({ pulse }) {
  if (!pulse) return (
    <Panel titleContent={<><span style={S.titleAccent(T.green)}>⚡</span><span>LIFE PULSE</span></>}>
      <div style={{color:T.textDim,fontSize:'10px'}}>
        AWAITING…<span style={{animation:'blink 1s step-end infinite',color:T.green}}> █</span>
      </div>
    </Panel>
  )
  const { active=false, total_evaluated=0, sobol_index=0, current_batch_size=200,
          top_molecules=[], mutant_accepted=0, mutant_attempted=0, tanimoto_pass_rate=null } = pulse
  const accent = active ? T.green : T.textDim
  const FCOL = { kinase:T.purple, cytokine:'#ff69b4', protease:'#ff8c00', nuclear_receptor:T.cyan, general:T.green }
  return (
    <Panel titleContent={
      <><span style={S.titleAccent(T.green)}>⚡</span><span>LIFE PULSE</span>
        <span style={{marginLeft:'auto',...S.pill(accent)}}>
          {active&&<span style={{display:'inline-block',width:5,height:5,borderRadius:'50%',
            background:T.green,boxShadow:glow(T.green,3),marginRight:4,
            animation:'blink 1.2s step-end infinite'}}/>}
          {active?'ACTIVE':'IDLE'}
        </span></>
    }>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:'6px',marginBottom:'8px',flexShrink:0}}>
        {[
          {v:total_evaluated.toLocaleString(), l:'EXPLORED',   c:T.green},
          {v:sobol_index.toLocaleString(),     l:'SOBOL IDX',  c:T.cyan},
          {v:current_batch_size,               l:'BATCH',      c:T.purple},
          {v:tanimoto_pass_rate!=null?`${tanimoto_pass_rate.toFixed(1)}%`:'—',
           l:'DIVERSITY',c:tanimoto_pass_rate!=null&&tanimoto_pass_rate<30?T.red:T.green},
        ].map(({v,l,c}) => (
          <div key={l} style={{padding:'6px 8px',background:'#020902',border:`1px solid ${c}22`}}>
            <div style={{fontSize:'18px',fontWeight:700,color:c,textShadow:glow(c,3),
                          fontVariantNumeric:'tabular-nums'}}>{v}</div>
            <div style={{fontSize:'8px',color:T.textDim,letterSpacing:'0.1em'}}>{l}</div>
          </div>
        ))}
      </div>
      {mutant_attempted>0 && (
        <div style={{fontSize:'10px',color:T.textDim,marginBottom:'6px',flexShrink:0}}>
          MUTATIONS:{' '}
          <span style={{color:T.purple}}>{mutant_attempted.toLocaleString()} tried</span>
          {' · '}
          <span style={{color:T.green}}>{mutant_accepted.toLocaleString()} accepted</span>
        </div>
      )}
      <div style={{flex:1,overflowY:'auto'}}>
        {top_molecules.slice(0,4).map((m,i) => (
          <div key={i} style={{...S.terminalLine,background:i===0?'#00ff4106':'transparent'}}>
            <span style={{color:T.greenDim,flexShrink:0,minWidth:14}}>#{i+1}</span>
            <span style={{...S.pill(FCOL[m.family]??T.textDim),flexShrink:0,fontSize:'8px'}}>
              {(m.family||'?').slice(0,6)}
            </span>
            <span style={S.smiles}>{m.smiles||'—'}</span>
            <span style={{color:T.green,fontWeight:700,fontSize:'11px',flexShrink:0}}>
              {m.proxy_score?.toFixed(4)}
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
  const e = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  let s = e(code)
  s = s.replace(/(&#39;|&quot;|`)(.*?)\1/g,'<span style="color:#ce9178">$1$2$1</span>')
  const kws = lang==='python'
    ? ['def','class','return','import','from','as','if','elif','else','for','while','in','not','and','or','True','False','None','with','try','except','raise','yield','lambda','async','await']
    : ['const','let','var','function','return','if','else','for','while','in','of','new','class','import','from','export','default','async','await','try','catch','throw','true','false','null']
  kws.forEach(kw => { s = s.replace(new RegExp(`\\b(${kw})\\b`,'g'),`<span style="color:#569cd6">$1</span>`) })
  s = s.replace(/(#[^\n]*|\/\/[^\n]*)/g,'<span style="color:#6a9955">$1</span>')
  s = s.replace(/\b(\d+\.?\d*)\b/g,'<span style="color:#b5cea8">$1</span>')
  return s
}
function parseContent(text) {
  const segs=[], re=/```(\w*)\n?([\s\S]*?)```/g
  let last=0, m
  while ((m=re.exec(text))!==null) {
    if (m.index>last) segs.push({type:'text',content:text.slice(last,m.index)})
    segs.push({type:'code',lang:m[1]||'text',content:m[2]}); last=m.index+m[0].length
  }
  if (last<text.length) segs.push({type:'text',content:text.slice(last)})
  return segs
}
function MsgSegment({ seg }) {
  if (seg.type==='code') {
    return (
      <div style={{background:'#020a02',border:`1px solid ${T.border}`,borderLeft:`3px solid ${T.green}`,
                   borderRadius:'2px',padding:'8px 12px',margin:'6px 0',overflowX:'auto'}}>
        {seg.lang&&<div style={{fontSize:'8px',color:T.textDim,letterSpacing:'0.14em',marginBottom:'4px',textTransform:'uppercase'}}>{seg.lang}</div>}
        <pre style={{margin:0,fontFamily:T.mono,fontSize:'10px',lineHeight:1.7,color:T.text,
                     whiteSpace:'pre-wrap',wordBreak:'break-word'}}
          dangerouslySetInnerHTML={{__html:highlightCode(seg.content,seg.lang)}} />
      </div>
    )
  }
  return (
    <span style={{lineHeight:1.7,whiteSpace:'pre-wrap'}}>
      {seg.content.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((p,i) => {
        if (p.startsWith('`')&&p.endsWith('`'))
          return <code key={i} style={{background:'#020a02',border:`1px solid ${T.border}`,color:T.cyan,
                                        padding:'1px 4px',fontSize:'10px',fontFamily:T.mono}}>{p.slice(1,-1)}</code>
        if (p.startsWith('**')&&p.endsWith('**'))
          return <strong key={i} style={{color:T.textBright}}>{p.slice(2,-2)}</strong>
        return <span key={i}>{p}</span>
      })}
    </span>
  )
}
function SaveFileButton({ filename, code }) {
  const [st,setSt]=useState('idle'),[err,setErr]=useState('')
  async function save() {
    if (st==='saving') return; setSt('saving')
    try {
      const r=await fetch('/agent/write-file',{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify({filename,content:code})})
      const d=await r.json(); if (!r.ok||d.error) throw new Error(d.error||`HTTP ${r.status}`)
      setSt('saved')
    } catch(e) { setErr(e.message); setSt('error') }
  }
  const c=st==='saved'?T.green:st==='error'?T.red:st==='saving'?T.textDim:T.cyan
  return (
    <div style={{marginTop:'4px'}}>
      <button onClick={save} disabled={st==='saving'||st==='saved'}
        style={{background:st==='saved'?c+'18':'#000a0a',border:`1px solid ${c}66`,color:c,
                fontFamily:T.mono,fontSize:'10px',letterSpacing:'0.1em',padding:'4px 12px',
                cursor:st==='saving'||st==='saved'?'default':'pointer',
                borderRadius:'2px',transition:'all 0.2s'}}>
        {st==='saved'?`✓ Saved to adaptive/${filename}`:st==='error'?`✗ ${err}`:st==='saving'?'SAVING…':`SAVE TO ADAPTIVE/${filename}`}
      </button>
      {st==='saved'&&<RestartMinerButton/>}
    </div>
  )
}
function RestartMinerButton() {
  const [st,setSt]=useState('idle'),[err,setErr]=useState('')
  async function restart() {
    if (st==='restarting') return; setSt('restarting')
    try {
      const r=await fetch('/agent/restart-miner',{method:'POST'})
      const d=await r.json(); if (!r.ok||d.error) throw new Error(d.error||`HTTP ${r.status}`)
      setSt('done')
    } catch(e) { setErr(e.message); setSt('error') }
  }
  const c=st==='done'?T.green:st==='error'?T.red:st==='restarting'?T.textDim:T.purple
  return (
    <button onClick={restart} disabled={st==='restarting'||st==='done'}
      style={{marginLeft:'8px',background:'#0a000a',border:`1px solid ${c}66`,color:c,
              fontFamily:T.mono,fontSize:'10px',letterSpacing:'0.1em',padding:'4px 12px',
              cursor:st==='restarting'||st==='done'?'default':'pointer',borderRadius:'2px'}}>
      {st==='done'?'✓ RESTARTED':st==='error'?`✗ ${err}`:st==='restarting'?'RESTARTING…':'↺ RESTART MINER'}
    </button>
  )
}
function ChatBubble({ role, content }) {
  const isUser = role==='user'
  const sfm = !isUser&&content.match(/SAVE_FILE:\s*(\S+\.py)\s*$/m)
  const sfn = sfm?sfm[1]:null
  const disp = sfn?content.replace(/\nSAVE_FILE:\s*\S+\.py\s*$/,'').replace(/SAVE_FILE:\s*\S+\.py\s*$/,''):content
  const segs = parseContent(disp)
  const lastPy = sfn?segs.reduce((a,s,i)=>(s.type==='code'&&(s.lang==='python'||s.lang==='py')?i:a),-1):-1
  return (
    <div style={{display:'flex',flexDirection:isUser?'row-reverse':'row',gap:'8px',marginBottom:'10px',alignItems:'flex-start'}}>
      <div style={{flexShrink:0,width:'20px',height:'20px',border:`1px solid ${isUser?T.cyan:T.green}`,
                   display:'flex',alignItems:'center',justifyContent:'center',fontSize:'8px',
                   color:isUser?T.cyan:T.green,background:isUser?'#000a0a':'#000a00',marginTop:'2px'}}>
        {isUser?'YOU':'AI'}
      </div>
      <div style={{flex:1,background:isUser?'#000a0a':'#020a02',
                   border:`1px solid ${isUser?T.cyan+'33':T.green+'33'}`,
                   borderRadius:'2px',padding:'8px 12px',fontSize:'11px',color:T.text,maxWidth:'90%'}}>
        {segs.map((seg,i)=>(
          <div key={i}>
            <MsgSegment seg={seg}/>
            {sfn&&i===lastPy&&<SaveFileButton filename={sfn} code={seg.content}/>}
          </div>
        ))}
        {sfn&&lastPy===-1&&<SaveFileButton filename={sfn} code={segs.filter(s=>s.type==='code').map(s=>s.content).join('\n')}/>}
      </div>
    </div>
  )
}

/* ─── LIFE AGENT ────────────────────────────────────────────── */
function LifeAgentPanel() {
  const [configured,setConfigured]=useState(null)
  const [messages,setMessages]=useState([])
  const [input,setInput]=useState('')
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState(null)
  const bottomRef=useRef(), inputRef=useRef()

  useEffect(()=>{
    fetch('/agent/status').then(r=>r.json()).then(d=>setConfigured(d.configured)).catch(()=>setConfigured(false))
  },[])
  useEffect(()=>{ bottomRef.current?.scrollIntoView({behavior:'smooth'}) },[messages,loading])

  async function send() {
    const text=input.trim(); if (!text||loading) return
    setInput(''); setError(null)
    const msgs=[...messages,{role:'user',content:text}]; setMessages(msgs); setLoading(true)
    try {
      const r=await fetch('/agent/chat',{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify({messages:msgs})})
      const d=await r.json(); if (!r.ok||d.error) throw new Error(d.error||`HTTP ${r.status}`)
      setMessages([...msgs,{role:'assistant',content:d.content}])
    } catch(e) { setError(e.message) } finally { setLoading(false); inputRef.current?.focus() }
  }

  if (configured===null) return null
  return (
    <Panel isAgent titleContent={
      <>
        <span style={{color:T.green,fontSize:'13px',fontWeight:700,letterSpacing:'0.25em',
                       textShadow:`0 0 12px ${T.green}, 0 0 24px ${T.green}, 0 0 40px ${T.green}`}}>
          ◈ LIFE AGENT
        </span>
        <span style={{marginLeft:'auto',...S.pill(T.green),fontSize:'10px',
                       boxShadow:`0 0 8px ${T.green}`}}>
          CLAUDE SONNET 4 · AI ASSISTANT
        </span>
      </>
    }>
      {!configured ? (
        <div style={{padding:'20px',textAlign:'center',border:`1px dashed ${T.border}`,background:'#020902'}}>
          <div style={{fontSize:'24px',marginBottom:'10px',opacity:0.5}}>🤖</div>
          <div style={{fontSize:'12px',color:T.textDim,lineHeight:1.8}}>
            Add <code style={{color:T.cyan,background:'#020a0a',padding:'1px 6px',
                              border:`1px solid ${T.border}`,fontFamily:T.mono,fontSize:'10px'}}>ANTHROPIC_API_KEY</code>
            {' '}to <code style={{color:T.green,background:'#020902',padding:'1px 6px',
                                   border:`1px solid ${T.border}`,fontFamily:T.mono,fontSize:'10px'}}>.env</code>
            {' '}to activate LIFE AGENT
          </div>
          <div style={{marginTop:'10px',fontSize:'9px',color:T.textDim,letterSpacing:'0.12em'}}>
            Then: <code style={{color:T.greenDim,fontFamily:T.mono}}>pm2 restart life-dashboard</code>
          </div>
        </div>
      ) : (
        <>
          <div style={{flex:1,overflowY:'auto',padding:'6px 4px',marginBottom:'8px',
                       border:`1px solid ${T.green}44`,background:'#010d01',
                       borderRadius:'2px',minHeight:'80px'}}>
            {messages.length===0&&!loading&&(
              <div style={{padding:'16px',color:T.textDim,fontSize:'11px',lineHeight:2}}>
                <div style={{color:T.green,marginBottom:'6px',
                              textShadow:`0 0 10px ${T.green},0 0 20px ${T.green}`,
                              fontSize:'12px',fontWeight:700}}>LIFE AGENT ONLINE</div>
                <span style={{color:T.cyan}}>→ "How do I build life_pulse.py?"</span><br/>
                <span style={{color:T.cyan}}>→ "Write a Sobol sweep for molecule search"</span><br/>
                <span style={{color:T.cyan}}>→ "Why is my Boltz2 score low on KRAS?"</span>
              </div>
            )}
            {messages.map((msg,i)=><ChatBubble key={i} role={msg.role} content={msg.content}/>)}
            {loading&&(
              <div style={{display:'flex',gap:'8px',alignItems:'flex-start',marginBottom:'10px'}}>
                <div style={{flexShrink:0,width:'20px',height:'20px',border:`1px solid ${T.green}`,
                              display:'flex',alignItems:'center',justifyContent:'center',
                              fontSize:'8px',color:T.green,background:'#000a00'}}>AI</div>
                <div style={{padding:'8px 12px',background:'#020a02',border:`1px solid ${T.green}33`,
                              borderRadius:'2px',fontSize:'11px',color:T.textDim}}>
                  <span style={{animation:'blink 0.8s step-end infinite',color:T.green}}>█</span> THINKING…
                </div>
              </div>
            )}
            {error&&<div style={{padding:'6px 12px',background:'#0a0006',border:`1px solid ${T.red}44`,
                                  color:T.red,fontSize:'10px',marginBottom:'6px'}}>ERROR: {error}</div>}
            <div ref={bottomRef}/>
          </div>
          <div style={{display:'flex',gap:'6px',alignItems:'flex-end',flexShrink:0}}>
            <textarea ref={inputRef} value={input} onChange={e=>setInput(e.target.value)}
              onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}}
              placeholder="Ask LIFE AGENT… (Enter send, Shift+Enter newline)" rows={2}
              style={{flex:1,background:'#010d01',border:`1px solid ${T.border}`,borderRadius:'2px',
                      color:T.text,fontFamily:T.mono,fontSize:'11px',padding:'8px 10px',
                      resize:'vertical',outline:'none',lineHeight:1.5}}
              onFocus={e=>{e.target.style.borderColor=T.green+'aa'}}
              onBlur={e=>{e.target.style.borderColor=T.border}}
            />
            <button onClick={send} disabled={loading||!input.trim()}
              style={{background:loading||!input.trim()?'#0a150a':T.green+'18',
                      border:`1px solid ${loading||!input.trim()?T.border:T.green+'88'}`,
                      color:loading||!input.trim()?T.textDim:T.green,fontFamily:T.mono,fontSize:'10px',
                      letterSpacing:'0.12em',padding:'8px 14px',cursor:loading||!input.trim()?'not-allowed':'pointer',
                      borderRadius:'2px',height:'100%',whiteSpace:'nowrap'}}>
              {loading?'WAIT…':'SEND ▶'}
            </button>
            {messages.length>0&&(
              <button onClick={()=>{setMessages([]);setError(null)}} disabled={loading}
                style={{background:'#0a0008',border:`1px solid ${T.purple}44`,color:T.textDim,
                        fontFamily:T.mono,fontSize:'10px',padding:'8px 10px',cursor:loading?'not-allowed':'pointer',
                        borderRadius:'2px',height:'100%'}}>
                CLEAR
              </button>
            )}
          </div>
          <div style={{marginTop:'4px',fontSize:'8px',color:T.textDim,letterSpacing:'0.1em',flexShrink:0}}>
            ENTER = SEND · SHIFT+ENTER = NEWLINE
          </div>
        </>
      )}
    </Panel>
  )
}

/* ─── CSS ───────────────────────────────────────────────────── */
const CSS = `
  * { box-sizing: border-box; }
  body { margin: 0; background: #050a05; font-family: 'Courier New', monospace; }
  ::selection { background: #00ff4133; color: #00ff41; }

  /* ── Electric glow animations ── */
  @keyframes electric {
    from { box-shadow: 0 0 5px #00ff41, 0 0 10px #00ff41, 0 0 20px #00ff41; }
    to   { box-shadow: 0 0 8px #00ff41, 0 0 20px #00ff41, 0 0 40px #00ff41, 0 0 60px #00ff4166; }
  }
  @keyframes electricAmber {
    from { box-shadow: 0 0 5px #ff8c00, 0 0 10px #ff8c00, 0 0 20px #ff8c00; }
    to   { box-shadow: 0 0 8px #ff8c00, 0 0 20px #ff8c00, 0 0 40px #ff8c00, 0 0 60px #ff8c0066; }
  }
  @keyframes electricAgent {
    from { box-shadow: 0 0 10px #00ff41, 0 0 30px #00ff41, 0 0 60px #00ff41; }
    to   { box-shadow: 0 0 15px #00ff41, 0 0 40px #00ff41, 0 0 80px #00ff41, 0 0 120px #00ff4188; }
  }

  .panel-electric { animation: electric 2s ease-in-out infinite alternate; }
  .panel-amber    { animation: electricAmber 2s ease-in-out infinite alternate; }
  .panel-agent    { animation: electricAgent 1s ease-in-out infinite alternate; }

  @keyframes textPulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.85; }
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0; }
  }

  ::-webkit-scrollbar { width: 3px; height: 3px; }
  ::-webkit-scrollbar-track { background: #050a05; }
  ::-webkit-scrollbar-thumb { background: #00ff4133; }
  ::-webkit-scrollbar-thumb:hover { background: #00ff4166; }

  /* ── Grid layout overrides ── */
  .react-grid-item { transition: none !important; }
  .react-grid-item.react-grid-placeholder {
    background: #00ff4110 !important;
    border: 1px dashed #00ff4155 !important;
    border-radius: 3px !important;
    opacity: 1 !important;
  }
  .react-resizable-handle { opacity: 0; transition: opacity 0.2s; }
  .react-grid-item:hover .react-resizable-handle { opacity: 1; }
  .react-resizable-handle::after {
    border-color: #00ff4199 !important;
    width: 7px !important; height: 7px !important;
  }
  .drag-handle { cursor: grab !important; }
  .drag-handle:active,
  .react-draggable-dragging .drag-handle { cursor: grabbing !important; }

  /* ── Reset button ── */
  .reset-btn {
    background: transparent; border: 1px solid #00ff4144;
    color: #5a9a5a; font-family: 'Courier New', monospace;
    font-size: 9px; letter-spacing: 0.14em; padding: 3px 12px;
    cursor: pointer; border-radius: 2px; text-transform: uppercase; transition: all 0.2s;
  }
  .reset-btn:hover {
    border-color: #00ff41aa; color: #00ff41;
    text-shadow: 0 0 8px #00ff41; background: #00ff4108;
  }

  /* ── Mobile stack ── */
  @media (max-width: 640px) {
    .react-grid-item {
      position: relative !important; transform: none !important;
      width: 100% !important; height: auto !important;
      min-height: 160px; margin-bottom: 10px;
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

  const onLayoutChange = useCallback(l => { setLayout(l); saveLayout(l) }, [])
  const resetLayout    = useCallback(() => { setLayout(DEFAULT_LAYOUT); saveLayout(DEFAULT_LAYOUT) }, [])

  useEffect(() => {
    const poll = async () => {
      try { const r=await fetch('/stats?'+Date.now()); if (r.ok) setPub(await r.json()) } catch {}
      setTick(new Date())
    }
    poll(); const id=setInterval(poll,5000); return ()=>clearInterval(id)
  }, [])
  useEffect(() => {
    const poll = async () => {
      try { const r=await fetch('/feed?'+Date.now()); if (r.ok) { const d=await r.json(); setFeed(d.rows??[]) } } catch {}
    }
    poll(); const id=setInterval(poll,10000); return ()=>clearInterval(id)
  }, [])
  useEffect(() => {
    const poll = async () => {
      try { const r=await fetch('/pulse?'+Date.now()); if (r.ok) setPulse(await r.json()) } catch {}
    }
    poll(); const id=setInterval(poll,5000); return ()=>clearInterval(id)
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

  const gridWidth = Math.max(winWidth - 24, 320)

  return (
    <>
      <style>{CSS}</style>
      <div style={S.wrap}>
        <MatrixRain />
        <div style={S.scanlines} />
        <div style={S.content}>

          {/* ── Compact header ── */}
          <header style={{
            borderBottom: `2px solid ${T.green}`,
            padding: '6px 20px',
            background: `linear-gradient(180deg, #020802 0%, ${T.bg} 100%)`,
            display: 'flex', alignItems: 'center', gap: '16px',
            boxShadow: `0 0 20px ${T.green}44`,
          }}>
            <span style={{fontSize:'18px'}}>🧬</span>
            <div style={{color:T.green, fontSize:'15px', fontWeight:700, letterSpacing:'0.06em',
                          textTransform:'uppercase', animation:'textPulse 3s ease-in-out infinite',
                          textShadow:`0 0 10px ${T.green}, 0 0 20px ${T.green}`}}>
              ▓▒░ YOUR GPU IS FIGHTING CANCER ░▒▓
            </div>
            <div style={{fontSize:'9px',color:T.cyan,letterSpacing:'0.18em',
                          textShadow:glow(T.cyan,3),whiteSpace:'nowrap'}}>
              LIFE COMPUTE · BOLTZ2 · SOLANA
            </div>
            <div style={{marginLeft:'auto',display:'flex',gap:'10px',alignItems:'center'}}>
              <button className="reset-btn" onClick={resetLayout}>⊞ RESET LAYOUT</button>
              <div style={S.statusBadge(alive)}>
                <div style={S.statusDot(alive)} />
                {alive ? 'SYS:ONLINE' : 'SYS:OFFLINE'}
              </div>
            </div>
          </header>

          {/* ── Drag-and-drop grid ── */}
          <div style={{padding:'8px'}}>
            <GridLayout
              layout={layout}
              cols={COLS}
              rowHeight={ROW_HEIGHT}
              width={gridWidth}
              margin={MARGIN}
              containerPadding={[0,0]}
              onLayoutChange={onLayoutChange}
              draggableHandle=".drag-handle"
              compactType={null}
              preventCollision={false}
              isResizable={true}
              isDraggable={true}
              useCSSTransforms={true}
              resizeHandles={['se','s','e','sw','w']}
            >
              <div key="system-status">
                <MinerStatusPanel alive={alive} currentTarget={target} minerId={minerId} lastUpdated={lastUpd}/>
              </div>
              <div key="molecules">
                <MoleculesPanel count={mols}/>
              </div>
              <div key="life-earned">
                <LifeEarnedPanel earned={life}/>
              </div>
              <div key="gpu-power">
                <GpuPowerPanel/>
              </div>
              <div key="network">
                <NetworkPanel network={network}/>
              </div>
              <div key="live-feed">
                <LiveScoringFeedPanel feed={feed}/>
              </div>
              <div key="targets">
                <TargetsPanel targets={tgts}/>
              </div>
              <div key="life-pulse">
                <LifePulsePanel pulse={pulse}/>
              </div>
              <div key="scoring-history">
                <ScoringHistoryPanel history={scoring}/>
              </div>
              <div key="life-agent">
                <LifeAgentPanel/>
              </div>
            </GridLayout>
          </div>

          {/* ── Footer ── */}
          <footer style={{borderTop:`1px solid ${T.border}`,padding:'8px 20px',
                           display:'flex',justifyContent:'space-between',alignItems:'center',
                           fontSize:'9px',color:T.textDim,letterSpacing:'0.1em'}}>
            <span>LIFE-COMPUTE MINER v2.0.0 // BIOPUNK EDITION</span>
            <span style={{color:T.green,textShadow:glow(T.green,2)}}>
              {tick?`LAST_SYNC: ${tick.toLocaleTimeString()}`:'CONNECTING…'}
            </span>
            <span>DRAG ⠿ TO MOVE · CORNER TO RESIZE · LAYOUT AUTO-SAVED</span>
          </footer>
        </div>
      </div>
    </>
  )
}
