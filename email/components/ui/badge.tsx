// shadcn/ui Badge: same variants, rendered as an inline <span>. The tinted
// variants are shadcn's dark-mode "soft" badges (tinted fill, bright text).
import type { ReactNode } from "react";
import { cn } from "./utils";

const variants = {
  default: "bg-primary text-primary-foreground",
  secondary: "bg-secondary text-secondary-foreground",
  success: "bg-primary-soft text-primary",
  warning: "bg-warning-soft text-warning",
  destructive: "bg-destructive-soft text-destructive",
  outline: "border border-solid border-border text-muted-foreground",
} as const;

export type BadgeVariant = keyof typeof variants;

export function Badge({
  variant = "default",
  className,
  children,
}: {
  variant?: BadgeVariant;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-block whitespace-nowrap rounded-full px-[10px] py-[3px] text-[13px] font-medium leading-[18px]",
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}
