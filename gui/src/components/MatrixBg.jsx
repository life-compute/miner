import { useEffect, useRef } from 'react'

export default function MatrixBg() {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const ctx    = canvas.getContext('2d')

    const resize = () => {
      canvas.width  = window.innerWidth
      canvas.height = window.innerHeight
    }
    resize()
    window.addEventListener('resize', resize)

    const cols  = Math.floor(canvas.width / 14)
    const drops = Array.from({ length: cols }, () => Math.random() * canvas.height / 14)
    const chars = 'ATCGLIFECMPUTE01脳ΨΔΩαβγδ∑∫'

    let frame
    const draw = () => {
      ctx.fillStyle = 'rgba(2,8,5,0.07)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)

      ctx.font      = '12px monospace'
      for (let i = 0; i < drops.length; i++) {
        const ch = chars[Math.floor(Math.random() * chars.length)]
        const x  = i * 14
        const y  = drops[i] * 14

        // Alternate green and occasional pink
        ctx.fillStyle = Math.random() > 0.97 ? '#ff69b4' : '#00ff41'
        ctx.fillText(ch, x, y)

        if (y > canvas.height && Math.random() > 0.975) drops[i] = 0
        else drops[i] += 0.5
      }
      frame = requestAnimationFrame(draw)
    }

    draw()
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      style={{ position: 'fixed', inset: 0, opacity: 0.05, pointerEvents: 'none', zIndex: 0 }}
    />
  )
}
