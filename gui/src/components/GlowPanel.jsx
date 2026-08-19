/* Biopunk panel wrapper */
export default function GlowPanel({ children, variant = 'green', className = '', style = {} }) {
  const variants = {
    green:  { borderColor: '#00ff4133', boxShadow: '0 0 1px #00ff41, 0 0 20px #00ff4122, inset 0 0 40px #00ff4105' },
    pink:   { borderColor: '#ff69b433', boxShadow: '0 0 1px #ff69b4, 0 0 20px #ff69b422, inset 0 0 40px #ff69b405' },
    amber:  { borderColor: '#ff8c0033', boxShadow: '0 0 1px #ff8c00, 0 0 20px #ff8c0022, inset 0 0 40px #ff8c0005' },
    cyan:   { borderColor: '#00ffff33', boxShadow: '0 0 1px #00ffff, 0 0 20px #00ffff22, inset 0 0 40px #00ffff05' },
    red:    { borderColor: '#ff003c33', boxShadow: '0 0 1px #ff003c, 0 0 20px #ff003c22, inset 0 0 40px #ff003c05' },
  }

  return (
    <div
      className={`rounded-md ${className}`}
      style={{
        background: '#080f08',
        border: `1px solid ${variants[variant].borderColor}`,
        boxShadow: variants[variant].boxShadow,
        ...style,
      }}
    >
      {children}
    </div>
  )
}
