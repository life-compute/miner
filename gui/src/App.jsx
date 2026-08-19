import { useState, useCallback } from 'react'
import Welcome        from './screens/Welcome.jsx'
import SystemCheck    from './screens/SystemCheck.jsx'
import WalletSetup    from './screens/WalletSetup.jsx'
import InstallProgress from './screens/InstallProgress.jsx'
import Dashboard      from './screens/Dashboard.jsx'
import MatrixBg       from './components/MatrixBg.jsx'

const SCREENS = ['welcome', 'system', 'wallet', 'install', 'dashboard']

export default function App() {
  const [screen,  setScreen]  = useState('welcome')
  const [wallet,  setWallet]  = useState('')
  const [sysInfo, setSysInfo] = useState(null)

  const go = useCallback((s) => setScreen(s), [])

  return (
    <div className="scanlines" style={{ width: '100vw', height: '100vh', background: '#020805', position: 'relative', overflow: 'hidden' }}>
      <MatrixBg />

      {screen === 'welcome'   && <Welcome   onNext={() => go('system')} />}
      {screen === 'system'    && <SystemCheck
                                    onNext={(info) => { setSysInfo(info); go('wallet') }}
                                    onBack={() => go('welcome')} />}
      {screen === 'wallet'    && <WalletSetup
                                    wallet={wallet}
                                    setWallet={setWallet}
                                    sysInfo={sysInfo}
                                    onNext={() => go('install')}
                                    onBack={() => go('system')} />}
      {screen === 'install'   && <InstallProgress
                                    wallet={wallet}
                                    onDone={() => go('dashboard')} />}
      {screen === 'dashboard' && <Dashboard wallet={wallet} />}
    </div>
  )
}
