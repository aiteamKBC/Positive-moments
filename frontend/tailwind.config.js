/**
 * The Kent Business College design tokens.
 *
 * Every colour here was taken from the current kentbusinesscollege.com
 * stylesheet (inspected 2026-09-20) or derived from it. The brand scale is
 * deliberately NOT used for status: an operations console that paints
 * "complete" in the house purple has no colour left to mean "complete", so
 * the semantic ramps below stay independent of the brand.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // --- brand -------------------------------------------------------
        // The official KBC plum. 700 is the action colour, 900/950 the shell.
        kent: {
          50: '#f5f1f9', 100: '#eadff3', 200: '#ded3e8', 300: '#c9aee0',
          400: '#a98ad0', 500: '#7751c6', 600: '#62409f', 700: '#4f2d7f',
          800: '#3e2365', 900: '#29163d', 950: '#1b1026',
        },
        brand: {
          50: '#f5f1f9', 100: '#eadff3', 200: '#ded3e8', 300: '#c9aee0',
          400: '#a98ad0', 500: '#7751c6', 600: '#62409f', 700: '#4f2d7f',
          800: '#3e2365', 900: '#29163d', 950: '#1b1026',
        },
        // The warm gold that appears as the accent on the public site. Used
        // sparingly, and never to mean a status.
        gold: { 100: '#f7edd6', 400: '#d5ab4e', 600: '#a97c1f' },

        // --- neutrals ----------------------------------------------------
        ink: '#211b26',
        body: '#433d4a',
        muted: '#6b6572',
        faint: '#948fa0',
        line: '#e7e2ec',
        canvas: '#f7f5fa',
        cream: '#f7f5fa',
      },
      borderRadius: { xl: '0.75rem', '2xl': '1rem' },
      boxShadow: {
        card: '0 1px 2px rgba(33, 27, 38, 0.04), 0 1px 3px rgba(33, 27, 38, 0.06)',
        raised: '0 10px 30px -12px rgba(41, 22, 61, 0.28)',
        header: '0 1px 0 rgba(231, 226, 236, 1)',
      },
      fontFamily: {
        sans: ['DM Sans', 'Segoe UI', 'Arial', 'sans-serif'],
        display: ['DM Serif Display', 'Georgia', 'serif'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      letterSpacing: { kicker: '0.14em' },
      transitionDuration: { DEFAULT: '150ms' },
    },
  },
  plugins: [],
}
