import { useState, useEffect } from 'react'
import { invoke } from '@tauri-apps/api/core'
import GlowPanel from '../components/GlowPanel.jsx'

function StatusRow({ label, value, ok, loading }) {
  const icon = loading ? '◌' : ok ? '✔' : '✖'
  const color = loading ? '#5a9a5a' : ok ? '#00ff41' : '#ff003c'

  return (
    <div
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 0', borderBottom: '1px solid #00ff4111',
      }}
    >
      <div style={{ color: '#aaddaa', fontSize: 13, letterSpacing: '0.05em' }}>
        {label}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ color: '#5a9a5a', fontSize: 12 }}>{value || '—'}</span>
        <span style={{ color, fontSize: 16, width: 20, textAlign: 'center',
                       textShadow: loading ? 'none' : `0 0 8px ${color}`,
                       animation: loading ? 'blink 1s infinite' : 'none' }}>
          {icon}
        </span>
      </div>
    </div>
  )
}

export default function SystemCheck({ onNext, onBack }) {
  const [checks, setChecks] = useState({
    gpu:   { label: 'NVIDIA GPU',              value: null, ok: null, loading: true },
    vram:  { label: 'GPU VRAM (min 8 GB)',      value: null, ok: null, loading: true },
    ram:   { label: 'System RAM (min 16 GB)',   value: null, ok: null, loading: true },
    disk:  { label: 'Free Disk (min 20 GB)',    value: null, ok: null, loading: true },
    docker:{ label: 'Docker installed',         value: null, ok: null, loading: true },
    cuda:  { label: 'CUDA available',           value: null, ok: null, loading: true },
  })
  const [done, setDone] = useState(false)

  useEffect(() => {
    const run = async () => {
      try {
        const result = await invoke('check_system')
        setChecks({
          gpu: {
            label: 'NVIDIA GPU', loading: false,
            value: result.gpu_name || 'Not found',
            ok: !!result.gpu_name,
          },
          vram: {
            label: 'GPU VRAM (min 8 GB)', loading: false,
            value: result.vram_gb != null ? `${result.vram_gb} GB` : 'Unknown',
            ok: result.vram_gb != null && result.vram_gb >= 8,
          },
          ram: {
            label: 'System RAM (min 16 GB)', loading: false,
            value: result.ram_gb != null ? `${result.ram_gb} GB` : 'Unknown',
            ok: result.ram_gb != null && result.ram_gb >= 16,
          },
          disk: {
            label: 'Free Disk (min 20 GB)', loading: false,
            value: result.disk_gb != null ? `${result.disk_gb} GB free` : 'Unknown',
            ok: result.disk_gb != null && result.disk_gb >= 20,
          },
          docker: {
            label: 'Docker installed', loading: false,
            value: result.docker_version || 'Not found',
            ok: !!result.docker_version,
          },
          cuda: {
            label: 'CUDA available', loading: false,
            value: result.cuda_version || 'Not found',
            ok: !!result.cuda_version,
          },
        })
        setDone(true)
      } catch (e) {
        console.error('check_system failed:', e)
        // Mark all as failed
        setChecks(prev => Object.fromEntries(
          Object.entries(prev).map(([k, v]) => [k, { ...v, loading: false, ok: false, value: 'Error' }])
        ))
        setDone(true)
      }
    }
    run()
  }, [])

  const allOk   = done && Object.values(checks).every(c => c.ok)
  const critical = done && ['gpu', 'vram', 'ram', 'disk'].every(k => checks[k].ok)

  return (
    <div
      style={{
        position: 'relative', zIndex: 10,
        display: 'flex', flexDirection: 'column', alignItems: 'center',
        justifyContent: 'center', height: '100vh', padding: '32px',
      }}
    >
      <GlowPanel style={{ width: '100%', maxWidth: 520, padding: '32px' }}>
        {/* Header */}
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div style={{ fontSize: 10, letterSpacing: '0.4em', color: '#5a9a5a', marginBottom: 6 }}>
            STEP 1 OF 4
          </div>
          <h2 className="glow-green" style={{ fontSize: 22, letterSpacing: '0.08em' }}>
            SYSTEM CHECK
          </h2>
          <div style={{ fontSize: 11, color: '#5a9a5a', marginTop: 4 }}>
            Verifying hardware requirements
          </div>
        </div>

        {/* Rows */}
        <div style={{ marginBottom: 20 }}>
          {Object.entries(checks).map(([key, c]) => (
            <StatusRow key={key} label={c.label} value={c.value} ok={c.ok} loading={c.loading} />
          ))}
        </div>

        {/* Verdict */}
        {done && (
          <div
            className={allOk ? 'glow-green' : (critical ? 'glow-amber' : 'glow-red')}
            style={{
              textAlign: 'center', fontSize: 13, letterSpacing: '0.08em',
              padding: '10px', marginBottom: 20,
              background: allOk ? '#00ff4108' : '#ff8c0008',
              border: `1px solid ${allOk ? '#00ff4144' : '#ff8c0044'}`,
              borderRadius: 4,
            }}
          >
            {allOk
              ? '✔  Your system is ready to mine!'
              : critical
                ? '⚠  Docker/CUDA optional — you can continue'
                : '✖  Missing critical hardware requirements'}
          </div>
        )}

        {/* Nav */}
        <div style={{ display: 'flex', gap: 12, justifyContent: 'space-between' }}>
          <button className="btn-green" onClick={onBack} style={{ padding: '10px 24px', fontSize: 12, flex: 1, borderRadius: 4 }}>
            ← BACK
          </button>
          <button
            className="btn-green"
            onClick={() => onNext(Object.fromEntries(Object.entries(checks).map(([k, v]) => [k, v])))}
            disabled={!done || !critical}
            style={{ padding: '10px 24px', fontSize: 12, flex: 2, borderRadius: 4 }}
          >
            CONTINUE →
          </button>
        </div>
      </GlowPanel>
    </div>
  )
}
