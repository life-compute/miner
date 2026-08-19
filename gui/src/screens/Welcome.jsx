import { useEffect, useState } from 'react'

export default function Welcome({ onNext }) {
  const [typed, setTyped] = useState('')
  const tagline = 'Your GPU could help cure cancer. Earn $LIFE tokens.'

  useEffect(() => {
    let i = 0
    const iv = setInterval(() => {
      if (i < tagline.length) {
        setTyped(tagline.slice(0, ++i))
      } else {
        clearInterval(iv)
      }
    }, 35)
    return () => clearInterval(iv)
  }, [])

  return (
    <div
      style={{
        position: 'relative', zIndex: 10,
        display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        height: '100vh', gap: '32px', padding: '40px',
      }}
    >
      {/* ── Logo ── */}
      <div className="float" style={{ textAlign: 'center' }}>
        <img
          src="/dna-helix.svg"
          alt="LIFE Compute DNA Helix"
          style={{ width: 100, height: 167, filter: 'drop-shadow(0 0 12px #00ff41) drop-shadow(0 0 30px #00ff4166)' }}
        />
      </div>

      {/* ── Title ── */}
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 11, letterSpacing: '0.4em', color: '#5a9a5a', marginBottom: 8, textTransform: 'uppercase' }}>
          LIFE COMPUTE
        </div>
        <h1
          className="glow-green"
          style={{ fontSize: 36, fontWeight: 700, letterSpacing: '0.05em', lineHeight: 1.1 }}
        >
          MINER
        </h1>
        <div style={{ fontSize: 10, letterSpacing: '0.3em', color: '#ff69b4', marginTop: 4, textShadow: '0 0 8px #ff69b4' }}>
          v1.0.0
        </div>
      </div>

      {/* ── Tagline ── */}
      <div
        style={{
          maxWidth: 460, textAlign: 'center', fontSize: 15,
          color: '#aaddaa', lineHeight: 1.7, minHeight: 48,
        }}
      >
        {typed}
        <span className="blink" style={{ color: '#00ff41' }}>█</span>
      </div>

      {/* ── Stats bar ── */}
      <div
        className="panel"
        style={{
          display: 'flex', gap: 48, padding: '12px 32px',
          fontSize: 11, letterSpacing: '0.08em',
        }}
      >
        <div style={{ textAlign: 'center' }}>
          <div className="glow-green" style={{ fontSize: 18, fontWeight: 700 }}>20</div>
          <div style={{ color: '#5a9a5a', marginTop: 2 }}>CANCER TARGETS</div>
        </div>
        <div style={{ width: 1, background: '#00ff4122' }} />
        <div style={{ textAlign: 'center' }}>
          <div className="glow-pink" style={{ fontSize: 18, fontWeight: 700 }}>412</div>
          <div style={{ color: '#5a9a5a', marginTop: 2 }}>ACTIVE MINERS</div>
        </div>
        <div style={{ width: 1, background: '#00ff4122' }} />
        <div style={{ textAlign: 'center' }}>
          <div className="glow-cyan" style={{ fontSize: 18, fontWeight: 700 }}>1.8M</div>
          <div style={{ color: '#5a9a5a', marginTop: 2 }}>MOLECULES SCREENED</div>
        </div>
      </div>

      {/* ── CTA button ── */}
      <button
        className="btn-green"
        onClick={onNext}
        style={{ padding: '14px 60px', fontSize: 14, letterSpacing: '0.2em', borderRadius: 4 }}
      >
        GET STARTED
      </button>

      {/* ── Footer ── */}
      <div style={{ position: 'absolute', bottom: 20, fontSize: 10, color: '#1a4a1a', letterSpacing: '0.2em' }}>
        POWERED BY SOLANA · BOLTZ2 · LIFE COMPUTE NETWORK
      </div>
    </div>
  )
}
