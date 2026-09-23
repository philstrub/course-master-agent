// shadcn/ui Badge: same variants, rendered as an inline <span>.
import type { ReactNode } from "react";
import { cn } from "./utils";

const variants = {
  default: "bg-primary text-primary-foreground",
  secondary: "bg-secondary text-secondary-foreground",
  destructive: "bg-destructive text-white",
  warning: "bg-warning text-warning-foreground",
  success: "bg-success text-white",
  outline: "border border-solid border-border text-foreground",
} as const;

export type BadgeVariant = keyof typeof variants;

export function Badge({
  variant = "default",
  children,
}: {
  variant?: BadgeVariant;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-block rounded-md px-2 py-0.5 text-xs font-semibold leading-4",
        variants[variant],
      )}
    >
      {children}
    </span>
  );
}
