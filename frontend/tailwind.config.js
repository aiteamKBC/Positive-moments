/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#172033',
        muted: '#657084',
        cream: '#f7f8f4',
        brand: {
          50: '#edfdf7',
          100: '#d4f8e9',
          200: '#adf0d6',
          300: '#77e2bd',
          400: '#3bc99d',
          500: '#18ad83',
          600: '#0c8a69',
          700: '#0b6e57',
          800: '#0c5746',
          900: '#0a483b'
        }
      },
      boxShadow: {
        card: '0 12px 32px rgba(26, 36, 51, 0.07)',
      },
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'sans-serif'],
      },
    },
  },
  plugins: [],
}

