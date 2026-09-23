// shadcn/ui Progress. Email clients ignore transforms, so the indicator is a
// table cell sized by percentage rather than a translated div.
import { cn } from "./utils";

export function Progress({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <table
      role="presentation"
      width="100%"
      cellPadding={0}
      cellSpacing={0}
      className={cn("h-2 rounded-full bg-secondary", className)}
    >
      <tbody>
        <tr>
          {pct > 0 && <td width={`${pct}%`} className="h-2 rounded-full bg-primary" />}
          {pct < 100 && <td className="h-2" />}
        </tr>
      </tbody>
    </table>
  );
}
