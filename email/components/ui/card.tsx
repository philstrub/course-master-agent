// shadcn/ui Card, ported to email-safe markup: <Section> tables instead of
// divs with flex/gap, which Gmail and Outlook drop.
import { Section, Text } from "@react-email/components";
import type { ReactNode } from "react";
import { cn } from "./utils";

type Props = { className?: string; children: ReactNode };

export function Card({ className, children }: Props) {
  return (
    <Section className={cn("rounded-xl border border-solid border-border bg-card", className)}>
      {children}
    </Section>
  );
}

export function CardHeader({ className, children }: Props) {
  return <Section className={cn("px-5 pt-5", className)}>{children}</Section>;
}

export function CardTitle({ className, children }: Props) {
  return (
    <Text className={cn("m-0 text-base font-semibold leading-6 text-foreground", className)}>
      {children}
    </Text>
  );
}

export function CardDescription({ className, children }: Props) {
  return (
    <Text className={cn("m-0 mt-1 text-sm leading-5 text-muted-foreground", className)}>
      {children}
    </Text>
  );
}

export function CardContent({ className, children }: Props) {
  return <Section className={cn("px-5 pb-5 pt-3", className)}>{children}</Section>;
}
