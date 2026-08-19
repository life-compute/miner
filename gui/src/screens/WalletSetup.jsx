import { useState, useEffect } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { openUrl } from '@tauri-apps/plugin-opener'
import GlowPanel from '../components/GlowPanel.jsx'

// Validate Solana public key: base58, 32–44 chars, valid base58 alphabet
const BASE58_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/

function isValidSolanaAddress(addr) {
  if (!BASE58_RE.test(addr)) return false
  // Rough length check: a compressed 32-byte pubkey in base58 is 43-44 chars
  // Shorter addresses can be valid too (leading zero bytes compress)
  return addr.length >= 32 && addr.length <= 44
}

export default function WalletSetup({ wallet, setWallet, onNext, onBack }) {
  const [minerCount, setMinerCount] = useState(null)
  const [checking,   setChecking]   = useState(false)
  const [error,      setError]      = useState('')

  const valid = isValidSolanaAddress(wallet)

  useEffect(() => {
    invoke('get_miner_count')
      .then(n => setMinerCount(n))
      .catch(() => setMinerCount(null))
  }, [])

  const handleContinue = async () => {
    if (!valid) {
      setError('Invalid Solana address. Please check and try again.')
      return
    }
    setError('')
    setChecking(true)
    try {
      await invoke('validate_wallet', { address: wallet })
      onNext()
    } catch (e) {
      setError(String(e))
    } finally {
      setChecking(false)
    }
  }

  const isFree  = minerCount != null && minerCount < 20
  const feeText = isFree
    ? `✔  SLOT ${minerCount + 1}/20 — You're in the FREE early-miner tier!`
    : `Registration fee: 0.01 SOL (~$1.40). Network grows stronger with every miner.`

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
            STEP 2 OF 4
          </div>
          <h2 className="glow-green" style={{ fontSize: 22, letterSpacing: '0.08em' }}>
            WALLET SETUP
          </h2>
          <div style={{ fontSize: 11, color: '#5a9a5a', marginTop: 4 }}>
            Connect your Solana wallet to receive $LIFE tokens
          </div>
        </div>

        {/* Wallet input */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ display: 'block', fontSize: 11, color: '#5a9a5a', letterSpacing: '0.1em', marginBottom: 8 }}>
            SOLANA WALLET ADDRESS
          </label>
          <input
            className="input-green"
            type="text"
            value={wallet}
            onChange={e => { setWallet(e.target.value); setError('') }}
            placeholder="Enter your Solana public key..."
            style={{ width: '100%', padding: '12px 14px', fontSize: 12, borderRadius: 4 }}
          />
          {/* Live validation indicator */}
          {wallet.length > 0 && (
            <div style={{
              marginTop: 6, fontSize: 11,
              color: valid ? '#00ff41' : '#ff003c',
              textShadow: `0 0 6px ${valid ? '#00ff41' : '#ff003c'}44`,
            }}>
              {valid ? '✔ Valid Solana address' : '✖ Invalid address format'}
            </div>
          )}
        </div>

        {/* No wallet link */}
        <div style={{ marginBottom: 20, fontSize: 11, color: '#5a9a5a' }}>
          No wallet yet?{' '}
          <button
            onClick={() => openUrl('https://phantom.app')}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: '#ff69b4', textDecoration: 'underline', fontFamily: 'inherit',
              fontSize: 11, textShadow: '0 0 6px #ff69b444',
            }}
          >
            Download Phantom →
          </button>
        </div>

        {/* Miner slot / fee badge */}
        <div
          className={isFree ? '' : 'panel-pink'}
          style={{
            padding: '12px 16px', borderRadius: 4, marginBottom: 20,
            fontSize: 12, letterSpacing: '0.04em',
            background: isFree ? '#00ff4108' : '#ff69b408',
            border: `1px solid ${isFree ? '#00ff4133' : '#ff69b433'}`,
            color: isFree ? '#00ff41' : '#aaddaa',
            textShadow: isFree ? '0 0 6px #00ff4166' : 'none',
          }}
        >
          {minerCount == null
            ? '⟳  Checking miner slots...'
            : feeText}
        </div>

        {/* Error */}
        {error && (
          <div style={{ color: '#ff003c', fontSize: 12, marginBottom: 14, textShadow: '0 0 6px #ff003c44' }}>
            {error}
          </div>
        )}

        {/* Nav */}
        <div style={{ display: 'flex', gap: 12 }}>
          <button className="btn-green" onClick={onBack} style={{ padding: '10px 24px', fontSize: 12, flex: 1, borderRadius: 4 }}>
            ← BACK
          </button>
          <button
            className="btn-green"
            onClick={handleContinue}
            disabled={!valid || checking}
            style={{ padding: '10px 24px', fontSize: 12, flex: 2, borderRadius: 4 }}
          >
            {checking ? 'CHECKING...' : 'CONTINUE →'}
          </button>
        </div>
      </GlowPanel>
    </div>
  )
}
