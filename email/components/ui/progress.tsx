// shadcn/ui Progress. Email clients ignore transforms, so the indicator is a
// table cell sized by percentage rather than a translated div.
import { cn } from "./utils";

export function Progress({
  value,
  className,
  indicatorClassName,
}: {
  value: number;
  className?: string;
  indicatorClassName?: string;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <table
      role="presentation"
      width="100%"
      cellPadding={0}
      cellSpacing={0}
      className={cn("h-[6px] rounded-full bg-secondary", className)}
    >
      <tbody>
        <tr>
          {pct > 0 && (
            <td width={`${pct}%`} className={cn("h-[6px] rounded-full bg-primary", indicatorClassName)} />
          )}
          {pct < 100 && <td className="h-[6px]" />}
        </tr>
      </tbody>
    </table>
  );
}
