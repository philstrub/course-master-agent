// shadcn/ui Card, ported to email-safe markup: <Section> tables instead of
// divs with flex/gap, which Gmail and Outlook drop.
import { Column, Row, Section, Text } from "@react-email/components";
import type { ReactNode } from "react";
import { cn } from "./utils";

type Props = { className?: string; children: ReactNode };

const card = "rounded-2xl border border-solid border-border bg-card";

// `as="column"` makes the card a table cell, so cards side by side in one
// <Row> share a height, which divs can't do in email.
export function Card({ className, children, as }: Props & { as?: "section" | "column" }) {
  if (as === "column") return <Column className={cn(card, className)}>{children}</Column>;
  return <Section className={cn(card, className)}>{children}</Section>;
}

// shadcn's CardHeader with a CardAction slot on the right.
export function CardHeader({ className, action, children }: Props & { action?: ReactNode }) {
  return (
    <Section className={cn("px-6 pt-5", className)}>
      <Row>
        <Column className="align-middle">{children}</Column>
        {action && (
          <Column align="right" className="align-middle">
            {action}
          </Column>
        )}
      </Row>
    </Section>
  );
}

export function CardTitle({ className, children }: Props) {
  return (
    <Text
      className={cn(
        "m-0 text-[13px] font-medium uppercase leading-5 tracking-[0.08em] text-subtle",
        className,
      )}
    >
      {children}
    </Text>
  );
}

export function CardDescription({ className, children }: Props) {
  return (
    <Text className={cn("m-0 text-[14px] leading-5 text-muted-foreground", className)}>
      {children}
    </Text>
  );
}

export function CardContent({ className, children }: Props) {
  return <Section className={cn("px-6 pb-5 pt-2", className)}>{children}</Section>;
}
