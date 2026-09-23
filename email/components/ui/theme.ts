// shadcn/ui "zinc" tokens as literal colours: email clients don't support the
// CSS variables shadcn normally uses, so each token is resolved here once.
export const shadcnTheme = {
  theme: {
    extend: {
      colors: {
        background: "#ffffff",
        foreground: "#09090b",
        card: "#ffffff",
        primary: "#18181b",
        "primary-foreground": "#fafafa",
        secondary: "#f4f4f5",
        "secondary-foreground": "#18181b",
        muted: "#f4f4f5",
        "muted-foreground": "#71717a",
        border: "#e4e4e7",
        destructive: "#dc2626",
        warning: "#fef3c7",
        "warning-foreground": "#92400e",
        success: "#16a34a",
      },
    },
  },
};
