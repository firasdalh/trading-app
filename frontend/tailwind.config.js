/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Trading-desk palette. `brand` = the primary accent (indigo); bull/bear tuned to pop on the
        // deep neutral background without glaring.
        brand: {
          DEFAULT: "#6366f1",
          400: "#818cf8",
          500: "#6366f1",
          600: "#4f46e5",
          700: "#4338ca",
        },
        bull: "#22c55e",
        bear: "#ef4444",
        warn: "#f59e0b",
      },
      boxShadow: {
        card: "0 1px 2px 0 rgba(0,0,0,0.3), 0 1px 3px 0 rgba(0,0,0,0.15)",
        pop: "0 10px 30px -10px rgba(0,0,0,0.6)",
      },
    },
  },
  plugins: [],
};
