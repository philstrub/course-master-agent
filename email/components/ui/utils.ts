// shadcn's `cn`, minus tailwind-merge: email classes are few and never conflict.
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}
