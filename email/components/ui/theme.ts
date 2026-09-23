// shadcn/ui "zinc" dark tokens with an emerald primary, as literal colours:
// email clients don't support the CSS variables shadcn normally uses, so
// each token is resolved here once.
export const shadcnTheme = {
  theme: {
    extend: {
      colors: {
        background: "#09090b",
        foreground: "#fafafa",
        card: "#111113",
        border: "#232327",
        muted: "#1c1c20",
        "muted-foreground": "#a1a1aa",
        subtle: "#71717a",
        primary: "#34d399",
        "primary-soft": "#0c2a21",
        "primary-foreground": "#022c22",
        secondary: "#27272a",
        "secondary-foreground": "#e4e4e7",
        warning: "#fbbf24",
        "warning-soft": "#2a1f07",
        destructive: "#fb7185",
        "destructive-soft": "#2c1016",
      },
      fontFamily: {
        sans: ["Geist", "Inter", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Helvetica", "Arial", "sans-serif"],
        mono: ["Geist Mono", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
};
