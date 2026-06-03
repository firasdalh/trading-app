/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Subtle trading-desk palette.
        bull: "#16a34a",
        bear: "#dc2626",
        warn: "#f59e0b",
      },
    },
  },
  plugins: [],
};
