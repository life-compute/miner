/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        'life-green':  '#00ff41',
        'life-pink':   '#ff69b4',
        'life-cyan':   '#00ffff',
        'life-amber':  '#ff8c00',
        'life-red':    '#ff003c',
        'life-bg':     '#020805',
        'life-surface':'#080f08',
      },
      fontFamily: {
        mono: ["'Courier New'", "'Source Code Pro'", 'monospace'],
      },
      animation: {
        'pulse-green': 'pulseGreen 2s ease-in-out infinite',
        'glow-text':   'glowText 3s ease-in-out infinite',
        'scanline':    'scanline 8s linear infinite',
        'float':       'float 4s ease-in-out infinite',
      },
      keyframes: {
        pulseGreen: {
          '0%, 100%': { boxShadow: '0 0 8px #00ff41, 0 0 16px #00ff4144' },
          '50%':      { boxShadow: '0 0 20px #00ff41, 0 0 40px #00ff4166' },
        },
        glowText: {
          '0%, 100%': { textShadow: '0 0 8px #00ff41, 0 0 20px #00ff4144' },
          '50%':      { textShadow: '0 0 20px #00ff41, 0 0 40px #00ff4188' },
        },
        float: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%':      { transform: 'translateY(-8px)' },
        },
      },
    },
  },
  plugins: [],
}
