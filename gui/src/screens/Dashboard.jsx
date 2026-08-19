import { useState, useEffect, useCallback } from 'react'
import { invoke } from '@tauri-apps/api/core'
import GlowPanel from '../components/GlowPanel.jsx'

function StatCard({ label, value, unit, color = '#00ff41', variant = 'green', pulse = false }) {
  return (
    <GlowPanel variant={variant} style={{ padding: '16px 20px', textAlign: 'center' }}>
      <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a9a5a', marginBottom: 6, textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        className={pulse ? 'stat-pulse' : ''}
        style={{ fontSize: 28, fontWeight: 700, color, fontFamily: 'monospace', lineHeight: 1 }}
      >
        {value ?? '—'}
      </div>
      {unit && (
        <div style={{ fontSize: 10, color: '#5a9a5a', marginTop: 4 }}>{unit}</div>
      )}
    </GlowPanel>
  )
}

function GpuBar({ pct }) {
  const color = pct > 85 ? '#ff003c' : pct > 60 ? '#ff8c00' : '#00ff41'
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#5a9a5a', marginBottom: 4 }}>
        <span>GPU UTILIZATION</span>
        <span style={{ color }}>{pct ?? 0}%</span>
      </div>
      <div className="progress-track" style={{ height: 6 }}>
        <div style={{ height: '100%', width: `${pct ?? 0}%`, background: `linear-gradient(90deg, ${color}88, ${color})`, boxShadow: `0 0 10px ${color}88`, transition: 'width 1s ease, background 0.5s' }} />
      </div>
    </div>
  )
}

export default function Dashboard({ wallet }) {
  const [stats,   setStats]   = useState(null)
  const [running, setRunning] = useState(false)
  const [busy,    setBusy]    = useState(false)
  const [error,   setError]   = useState(null)

  const fetchStats = useCallback(async () => {
    try {
      const s = await invoke('get_stats')
      setStats(s)
      setRunning(s?.alive ?? false)
      setError(null)
    } catch {
      setError('Stats server offline — is the miner running?')
    }
  }, [])

  useEffect(() => {
    fetchStats()
    const iv = setInterval(fetchStats, 2000)
    return () => clearInterval(iv)
  }, [fetchStats])

  const toggleMiner = async () => {
    setBusy(true)
    try {
      if (running) {
        await invoke('stop_miner')
        setRunning(false)
      } else {
        await invoke('start_miner', { wallet })
        setRunning(true)
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const fmt = (n, decimals = 2) => n != null ? Number(n).toFixed(decimals) : '—'
  const fmtInt = (n) => n != null ? Number(n).toLocaleString() : '—'
  const gpuPct = stats?.gpu_utilization_pct ?? stats?.gpu_usage ?? 0

  return (
    <div
      style={{
        position: 'relative', zIndex: 10, height: '100vh',
        display: 'flex', flexDirection: 'column',
        padding: '20px 24px', gap: 16, overflow: 'hidden',
      }}
    >
      {/* ── Header bar ── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <img src="/dna-helix.svg" alt="LIFE" style={{ height: 32, filter: 'drop-shadow(0 0 6px #00ff41)' }} />
          <div>
            <div className="glow-green" style={{ fontSize: 16, fontWeight: 700, letterSpacing: '0.1em' }}>
              LIFE COMPUTE
            </div>
            <div style={{ fontSize: 9, color: '#5a9a5a', letterSpacing: '0.2em' }}>MINING DASHBOARD</div>
          </div>
        </div>

        {/* Status pill */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '4px 12px', borderRadius: 20, fontSize: 11,
          background: running ? '#00ff4110' : '#ff003c10',
          border: `1px solid ${running ? '#00ff4144' : '#ff003c44'}`,
          color: running ? '#00ff41' : '#ff003c',
        }}>
          <span className={running ? 'blink' : ''} style={{ fontSize: 8 }}>●</span>
          {running ? 'MINING ACTIVE' : 'STOPPED'}
        </div>
      </div>

      {/* ── $LIFE earned — hero stat ── */}
      <GlowPanel style={{ padding: '20px', textAlign: 'center', flexShrink: 0 }}>
        <div style={{ fontSize: 10, letterSpacing: '0.3em', color: '#5a9a5a', marginBottom: 4 }}>
          $LIFE EARNED
        </div>
        <div
          className="stat-pulse"
          style={{ fontSize: 52, fontWeight: 700, color: '#00ff41', fontFamily: 'monospace', lineHeight: 1 }}
        >
          {fmt(stats?.life_earned, 4)}
        </div>
        <div style={{ fontSize: 11, color: '#5a9a5a', marginTop: 4 }}>
          WALLET: {wallet ? `${wallet.slice(0, 8)}...${wallet.slice(-8)}` : '—'}
        </div>
      </GlowPanel>

      {/* ── Main stat grid ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, flexShrink: 0 }}>
        <StatCard
          label="Molecules Screened"
          value={fmtInt(stats?.molecules_screened)}
          color="#00ff41"
          variant="green"
        />
        <StatCard
          label="Active Cancer Target"
          value={stats?.current_target ?? stats?.targets_contributed?.[0] ?? '—'}
          unit={stats?.target_protein ?? ''}
          color="#ff69b4"
          variant="pink"
        />
        <StatCard
          label="Boltz2 Score"
          value={stats?.boltz2_score != null ? fmt(stats.boltz2_score, 3) : '—'}
          unit="kcal/mol"
          color="#00ffff"
          variant="cyan"
        />
      </div>

      {/* ── GPU panel ── */}
      <GlowPanel variant="amber" style={{ padding: '16px 20px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ fontSize: 10, letterSpacing: '0.2em', color: '#5a9a5a' }}>GPU STATUS</div>
          <div style={{ display: 'flex', gap: 20, fontSize: 11 }}>
            <span style={{ color: '#ff8c00' }}>
              POWER: <strong>{stats?.gpu_power_w ?? '—'}W</strong>
            </span>
            <span style={{ color: '#ff8c00' }}>
              VRAM: <strong>{stats?.vram_used_gb ?? '—'} / {stats?.vram_total_gb ?? '—'} GB</strong>
            </span>
            <span style={{ color: '#ff8c00' }}>
              TEMP: <strong>{stats?.gpu_temp_c ?? '—'}°C</strong>
            </span>
          </div>
        </div>
        <GpuBar pct={gpuPct} />
      </GlowPanel>

      {/* ── Global network stats ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, flexShrink: 0 }}>
        <GlowPanel style={{ padding: '10px 14px', textAlign: 'center' }}>
          <div style={{ fontSize: 9, color: '#5a9a5a', letterSpacing: '0.15em' }}>GLOBAL MINERS</div>
          <div style={{ color: '#aaddaa', fontSize: 18, marginTop: 2 }}>{fmtInt(stats?.global?.total_miners)}</div>
        </GlowPanel>
        <GlowPanel style={{ padding: '10px 14px', textAlign: 'center' }}>
          <div style={{ fontSize: 9, color: '#5a9a5a', letterSpacing: '0.15em' }}>GLOBAL MOLECULES</div>
          <div style={{ color: '#aaddaa', fontSize: 18, marginTop: 2 }}>{fmtInt(stats?.global?.molecules_global)}</div>
        </GlowPanel>
        <GlowPanel style={{ padding: '10px 14px', textAlign: 'center' }}>
          <div style={{ fontSize: 9, color: '#5a9a5a', letterSpacing: '0.15em' }}>TARGETS SOLVED</div>
          <div className="glow-pink" style={{ fontSize: 18, marginTop: 2 }}>{stats?.global?.targets_solved ?? '—'}</div>
        </GlowPanel>
      </div>

      {/* ── Error ── */}
      {error && (
        <div style={{ fontSize: 11, color: '#ff003c', textAlign: 'center', flexShrink: 0 }}>
          ⚠ {error}
        </div>
      )}

      {/* ── Start/Stop button ── */}
      <div style={{ flexShrink: 0 }}>
        <button
          className="btn-green"
          onClick={toggleMiner}
          disabled={busy}
          style={{
            width: '100%', padding: '14px', fontSize: 14, letterSpacing: '0.2em',
            borderRadius: 4, marginBottom: 0,
            borderColor: running ? '#ff003c' : '#00ff41',
            color: running ? '#ff003c' : '#00ff41',
            boxShadow: running ? '0 0 8px #ff003c44' : '0 0 8px #00ff4144',
          }}
        >
          {busy ? 'PLEASE WAIT...' : running ? '⏹  STOP MINING' : '▶  START MINING'}
        </button>
      </div>
    </div>
  )
}
