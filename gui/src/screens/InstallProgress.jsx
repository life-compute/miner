import { useState, useEffect, useRef } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import GlowPanel from '../components/GlowPanel.jsx'

const STEPS = [
  { id: 'docker',    label: 'Installing Docker',                    weight: 15 },
  { id: 'pull',      label: 'Pulling life-compute-miner image',     weight: 35 },
  { id: 'msa',       label: 'Downloading MSA files',                weight: 30 },
  { id: 'register',  label: 'Registering miner on Solana',          weight: 15 },
  { id: 'start',     label: 'Starting mining daemon',               weight: 5  },
]

const TOTAL_WEIGHT = STEPS.reduce((s, st) => s + st.weight, 0)

export default function InstallProgress({ wallet, onDone }) {
  const [stepStatus, setStepStatus] = useState(
    Object.fromEntries(STEPS.map(s => [s.id, { status: 'pending', progress: 0, message: '' }]))
  )
  const [currentStep, setCurrentStep] = useState(null)
  const [done,        setDone]        = useState(false)
  const [error,       setError]       = useState(null)
  const [logs,        setLogs]        = useState([])
  const logsRef = useRef(null)

  // Compute overall progress %
  const overallPct = (() => {
    let acc = 0
    for (const s of STEPS) {
      const st = stepStatus[s.id]
      if (st.status === 'done') acc += s.weight
      else if (st.status === 'running') acc += s.weight * (st.progress / 100)
    }
    return Math.round((acc / TOTAL_WEIGHT) * 100)
  })()

  useEffect(() => {
    let unlisten = []

    const run = async () => {
      // Listen for progress events from Tauri backend
      unlisten.push(await listen('install_progress', (event) => {
        const { step, status, progress, message } = event.payload
        setStepStatus(prev => ({
          ...prev,
          [step]: { status, progress: progress ?? 0, message: message ?? '' },
        }))
        if (status === 'running') setCurrentStep(step)
        if (message) setLogs(prev => [...prev.slice(-40), message])
      }))

      unlisten.push(await listen('install_done', () => {
        setDone(true)
      }))

      unlisten.push(await listen('install_error', (event) => {
        setError(event.payload)
      }))

      try {
        await invoke('run_install', { wallet })
      } catch (e) {
        setError(String(e))
      }
    }

    run()
    return () => { unlisten.forEach(fn => fn && fn()) }
  }, [wallet])

  // Auto-scroll logs
  useEffect(() => {
    if (logsRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight
    }
  }, [logs])

  const getStepColor = (id) => {
    const s = stepStatus[id]
    if (s.status === 'done')    return '#00ff41'
    if (s.status === 'running') return '#ff8c00'
    if (s.status === 'error')   return '#ff003c'
    return '#1a4a1a'
  }

  const getStepIcon = (id) => {
    const s = stepStatus[id]
    if (s.status === 'done')    return '✔'
    if (s.status === 'running') return '◐'
    if (s.status === 'error')   return '✖'
    return '○'
  }

  return (
    <div
      style={{
        position: 'relative', zIndex: 10,
        display: 'flex', flexDirection: 'column', alignItems: 'center',
        justifyContent: 'center', height: '100vh', padding: '32px',
      }}
    >
      <GlowPanel style={{ width: '100%', maxWidth: 560, padding: '32px' }}>
        {/* Header */}
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div style={{ fontSize: 10, letterSpacing: '0.4em', color: '#5a9a5a', marginBottom: 6 }}>
            STEP 3 OF 4
          </div>
          <h2 className="glow-green" style={{ fontSize: 22, letterSpacing: '0.08em' }}>
            {done ? 'MINING STARTED!' : 'INSTALLING'}
          </h2>
          {done && (
            <div className="glow-pink" style={{ fontSize: 14, marginTop: 8 }}>
              🧬 Your GPU is now fighting cancer.
            </div>
          )}
        </div>

        {/* Overall progress bar */}
        <div style={{ marginBottom: 24 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#5a9a5a', marginBottom: 6 }}>
            <span>OVERALL PROGRESS</span>
            <span className="glow-green">{overallPct}%</span>
          </div>
          <div className="progress-track" style={{ height: 8 }}>
            <div className="progress-fill" style={{ width: `${overallPct}%` }} />
          </div>
        </div>

        {/* Steps list */}
        <div style={{ marginBottom: 20 }}>
          {STEPS.map(s => {
            const col  = getStepColor(s.id)
            const icon = getStepIcon(s.id)
            const st   = stepStatus[s.id]
            return (
              <div key={s.id} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '8px 0', borderBottom: '1px solid #00ff4108',
              }}>
                <span style={{ color: col, width: 16, textAlign: 'center', fontSize: 13,
                               textShadow: `0 0 6px ${col}66`,
                               animation: st.status === 'running' ? 'blink 0.8s infinite' : 'none' }}>
                  {icon}
                </span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 12, color: st.status === 'pending' ? '#1a4a1a' : '#aaddaa' }}>
                    {s.label}
                  </div>
                  {st.status === 'running' && (
                    <div style={{ marginTop: 4 }}>
                      <div className="progress-track" style={{ height: 3 }}>
                        <div className="progress-fill" style={{ width: `${st.progress}%` }} />
                      </div>
                    </div>
                  )}
                  {st.message && st.status === 'error' && (
                    <div style={{ color: '#ff003c', fontSize: 10, marginTop: 2 }}>{st.message}</div>
                  )}
                </div>
                {st.status === 'running' && (
                  <span style={{ fontSize: 10, color: '#ff8c00' }}>{st.progress}%</span>
                )}
              </div>
            )
          })}
        </div>

        {/* Log tail */}
        <div
          ref={logsRef}
          style={{
            height: 80, overflowY: 'auto', background: '#020805',
            border: '1px solid #00ff4111', borderRadius: 3, padding: '8px',
            fontSize: 10, color: '#3a6a3a', fontFamily: 'monospace',
          }}
          className="scroll-area"
        >
          {logs.length === 0 ? (
            <span style={{ color: '#1a3a1a' }}>Waiting for installer output...</span>
          ) : logs.map((l, i) => (
            <div key={i}>{l}</div>
          ))}
        </div>

        {/* Error */}
        {error && (
          <div style={{ marginTop: 12, color: '#ff003c', fontSize: 11 }}>✖ {error}</div>
        )}

        {/* Done button */}
        {done && (
          <button
            className="btn-green"
            onClick={onDone}
            style={{ marginTop: 20, width: '100%', padding: '14px', fontSize: 14, letterSpacing: '0.15em', borderRadius: 4 }}
          >
            VIEW MINING DASHBOARD →
          </button>
        )}
      </GlowPanel>
    </div>
  )
}
