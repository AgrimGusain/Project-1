/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#07090c", 900: "#0b0f14", 850: "#10151c", 800: "#141b24", 700: "#1b2430", 600: "#243041", 500: "#34445a" },
        fg: { DEFAULT: "#e6edf3", muted: "#8b98a9", faint: "#5d6b7d" },
        amber: { DEFAULT: "#f5b83d", dim: "#8a6a24" },
        ok: "#3ecf8e",
        warn: "#f5b83d",
        bad: "#ff5a5f",
        info: "#5aa9ff",
        sim: "#b197fc",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "Roboto", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "ui-monospace", "monospace"],
      },
      boxShadow: { panel: "0 1px 0 0 rgba(255,255,255,0.03) inset, 0 8px 24px -12px rgba(0,0,0,0.6)" },
    },
  },
  plugins: [],
};
