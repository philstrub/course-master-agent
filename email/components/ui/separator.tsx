// shadcn/ui Separator.
import { Hr } from "@react-email/components";
import { cn } from "./utils";

export function Separator({ className }: { className?: string }) {
  return <Hr className={cn("my-0 w-full border-0 border-t border-solid border-border", className)} />;
}
