// The morning-brief dashboard. Pure presentation: every word comes from the
// JSON the agent wrote (validated against brief.schema.json) plus the facts
// `mitsync email` added. No judgment happens here.
//
// Dark by design (zinc + emerald). A fixed title, then sections: Homework
// (one banner card per homework, two per row), Today's schedule (an hour
// grid that keeps the gaps between events) and Heads up. The agent's
// `headline` is the inbox preview and subject, and `summary` stays in the
// JSON for tomorrow's run; neither is drawn.
import {
  Body,
  Column,
  Container,
  Head,
  Html,
  Preview,
  Row,
  Section,
  Tailwind,
  Text,
} from "@react-email/components";
import { Fragment, type ReactNode } from "react";
import { Badge, type BadgeVariant } from "./components/ui/badge";
import { Card, CardContent } from "./components/ui/card";
import { Progress } from "./components/ui/progress";
import { Separator } from "./components/ui/separator";
import { shadcnTheme } from "./components/ui/theme";

export type Homework = {
  course: string;
  title: string;
  due_at: string;
  urgency: "now" | "soon" | "later";
  status: "ready_to_submit" | "in_progress" | "not_started" | "submitted" | "unknown";
  progress: number | null;
  summary: string;
  next_step: string;
  review: { file: string; where: string; why: string }[];
  effort_hours: number | null;
};

export type ScheduleItem = {
  start: string; // local HH:MM
  end: string;
  title: string;
  course: string | null;
  location: string | null;
  calendar: string;
  all_day: boolean;
  ends_next_day: boolean;
};

export type Brief = {
  date: string;
  headline: string;
  homework: Homework[];
  gaps: string[];
  schedule: ScheduleItem[];
  sync: { last: string | null; fresh: boolean };
  generated_at: string;
};

const TZ = "America/New_York";

// Geist from Google Fonts (latin subset). Apple Mail loads it; Gmail falls
// back to the system sans in the theme's font stack.
const GF = "https://fonts.gstatic.com/s";
const GEIST: [string, number, string][] = [
  ["Geist", 400, `${GF}/geist/v5/gyBhhwUxId8gMGYQMKR3pzfaWI_RnOMImpna6VEdtZiI.woff2`],
  ["Geist", 500, `${GF}/geist/v5/gyBhhwUxId8gMGYQMKR3pzfaWI_RruMImpna6VEdtZiI.woff2`],
  ["Geist", 600, `${GF}/geist/v5/gyBhhwUxId8gMGYQMKR3pzfaWI_RQuQImpna6VEdtZiI.woff2`],
  ["Geist", 700, `${GF}/geist/v5/gyBhhwUxId8gMGYQMKR3pzfaWI_Re-QImpna6VEdtZiI.woff2`],
  ["Geist Mono", 500, `${GF}/geistmono/v6/or3yQ6H-1_WfwkMZI_qYPLs1a-t7PU0AbeEPKK5U5Cl4PuCTTNs.woff2`],
];

// Phones: the two-column homework grid stacks (Apple Mail, iOS, Gmail app).
const RESPONSIVE =
  "@media (max-width:600px){.hw-cell{display:block!important;width:100%!important;margin-bottom:16px}.hw-gap{display:none!important}}";

const status: Record<Homework["status"], { label: string; variant: BadgeVariant }> = {
  ready_to_submit: { label: "Ready to submit", variant: "success" },
  in_progress: { label: "In progress", variant: "warning" },
  not_started: { label: "Not started", variant: "outline" },
  submitted: { label: "Submitted", variant: "secondary" },
  unknown: { label: "Unknown", variant: "outline" },
};

const dueTone: Record<Homework["urgency"], string> = {
  now: "text-destructive",
  soon: "text-warning",
  later: "text-foreground",
};

// -- dates -------------------------------------------------------------------

function ordinal(n: number): string {
  const teen = n % 100 >= 11 && n % 100 <= 13;
  const suffix = teen ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${suffix}`;
}

function newsletterTitle(date: string): string {
  const d = new Date(`${date}T12:00:00Z`);
  const weekday = d.toLocaleDateString("en-US", { timeZone: "UTC", weekday: "long" });
  const month = d.toLocaleDateString("en-US", { timeZone: "UTC", month: "long" });
  return `Newsletter for ${weekday}, ${month} ${ordinal(d.getUTCDate())}`;
}

function localDay(d: Date): string {
  return d.toLocaleDateString("en-CA", { timeZone: TZ }); // YYYY-MM-DD
}

function clock(d: Date): string {
  return d.toLocaleTimeString("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// "Tonight 23:59", "Tomorrow 12:00", "Mon 28 · 23:59"; plus "in 9 h" / "in 5 days".
function due(dueAt: string, now: Date): { when: string; rel: string } {
  const d = new Date(dueAt);
  const days = Math.round((Date.parse(localDay(d)) - Date.parse(localDay(now))) / 86_400_000);
  const time = clock(d);
  const weekday = d.toLocaleDateString("en-US", { timeZone: TZ, weekday: "short" });
  const dayNum = d.toLocaleDateString("en-US", { timeZone: TZ, day: "numeric" });
  const when =
    days === 0
      ? `${Number(time.slice(0, 2)) >= 17 ? "Tonight" : "Today"} ${time}`
      : days === 1
        ? `Tomorrow ${time}`
        : `${weekday} ${dayNum} · ${time}`;
  const hours = (d.getTime() - now.getTime()) / 3_600_000;
  const rel =
    hours < 0 ? "overdue" : hours < 48 ? `in ${Math.max(1, Math.round(hours))} h` : `in ${Math.round(hours / 24)} days`;
  return { when, rel };
}

// -- layout pieces -------------------------------------------------------------

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <Section className="mb-3 mt-9 px-1">
      <Text className="m-0 text-[20px] font-semibold leading-7 tracking-[-0.01em] text-foreground">
        {children}
      </Text>
    </Section>
  );
}

function HomeworkCard({ hw, now }: { hw: Homework; now: Date }) {
  const s = status[hw.status];
  const d = due(hw.due_at, now);
  const numbers = [hw.progress === null ? null : `${hw.progress}%`, hw.effort_hours ? `~${hw.effort_hours} h` : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      <Section className="rounded-t-2xl bg-primary-soft px-5 pb-4 pt-5">
        <Text className="m-0 text-[22px] font-bold leading-7 tracking-[-0.01em] text-foreground">
          {hw.course.replace(/_/g, " ")}
        </Text>
        <Text className="m-0 mt-1 text-[15px] leading-5 text-primary">{hw.title}</Text>
      </Section>
      <CardContent className="px-5 pb-5 pt-4">
        <Text className={`m-0 text-[17px] font-semibold leading-6 ${dueTone[hw.urgency]}`}>{d.when}</Text>
        <Text className="m-0 text-[14px] leading-5 text-subtle">{d.rel}</Text>
        <Section className="mt-3">
          <Row>
            <Column className="align-middle">
              <Badge variant={s.variant}>{s.label}</Badge>
            </Column>
            <Column align="right" className="align-middle">
              <Text className="m-0 text-[14px] leading-5 text-muted-foreground">{numbers}</Text>
            </Column>
          </Row>
        </Section>
        {hw.progress !== null && hw.status !== "submitted" && <Progress value={hw.progress} className="mt-3" />}
        <Text className="m-0 mt-4 text-[15px] leading-[22px] text-secondary-foreground">
          <span className="text-primary">→ </span>
          {hw.next_step}
        </Text>
        {hw.review.length > 0 && (
          <Section className="mt-3 rounded-xl bg-muted px-3 py-2">
            <Text className="m-0 pb-1 text-[12px] font-medium uppercase leading-4 tracking-[0.08em] text-subtle">
              Review
            </Text>
            {hw.review.map((r) => (
              <Text key={r.file + r.where} className="m-0 py-[2px] text-[13px] leading-[18px] text-muted-foreground">
                <span className="font-medium text-foreground">{r.file.split("/").pop()}</span>
                <span className="text-primary"> · {r.where}</span>
              </Text>
            ))}
          </Section>
        )}
      </CardContent>
    </>
  );
}

// Two banner cards per row. Each card is a table cell, so both cards in a
// row share one height, as in a real grid.
function HomeworkGrid({ homework, now }: { homework: Homework[]; now: Date }) {
  const rows: Homework[][] = [];
  for (let i = 0; i < homework.length; i += 2) rows.push(homework.slice(i, i + 2));
  return (
    <>
      {rows.map((pair) => (
        <Row key={pair.map((h) => h.course + h.title).join("|")} className="mb-4">
          {pair.map((hw, i) => (
            <Fragment key={hw.course + hw.title}>
              {i === 1 && <Column className="hw-gap w-4" />}
              <Card as="column" className="hw-cell w-[296px] align-top">
                <HomeworkCard hw={hw} now={now} />
              </Card>
            </Fragment>
          ))}
          {pair.length === 1 && (
            <>
              <Column className="hw-gap w-4" />
              <Column className="hw-cell w-[296px]" />
            </>
          )}
        </Row>
      ))}
    </>
  );
}

// -- the day as a calendar ------------------------------------------------------

const SLOT_MIN = 30;
const SLOT_PX = 28;

type Placed = { item: ScheduleItem; from: number; to: number; lane: number; alone: boolean };

function minutes(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

// Greedy lanes, as in Calendar.app: overlapping events sit side by side, and
// an event that overlaps nothing takes the full width.
function place(items: ScheduleItem[]): { placed: Placed[]; lanes: number; first: number; last: number } {
  const spans = items
    .filter((i) => !i.all_day)
    .map((item) => {
      const from = minutes(item.start);
      const to = item.ends_next_day ? 24 * 60 : Math.max(minutes(item.end), from + SLOT_MIN);
      return { item, from, to };
    })
    .sort((a, b) => a.from - b.from || b.to - a.to);
  const firstHour = Math.min(8, ...spans.map((s) => Math.floor(s.from / 60)));
  const lastHour = Math.max(18, ...spans.map((s) => Math.ceil(s.to / 60)));
  const laneEnds: number[] = [];
  const placed = spans.map((s) => {
    let lane = laneEnds.findIndex((end) => end <= s.from);
    if (lane === -1) lane = laneEnds.push(0) - 1;
    laneEnds[lane] = s.to;
    const alone = !spans.some((o) => o !== s && o.from < s.to && s.from < o.to);
    return { ...s, lane, alone };
  });
  return { placed, lanes: Math.max(1, laneEnds.length), first: firstHour * 60, last: lastHour * 60 };
}

// The event is drawn on the <td> itself, so its colour fills the whole time
// span; its text stays on one line each so it can never stretch the grid.
function EventCell({ p, span, colSpan }: { p: Placed; span: number; colSpan: number }) {
  const { item } = p;
  const when = `${item.start}–${item.end}${item.ends_next_day ? " +1" : ""}`;
  const where = [when, item.location?.replace(/^Classroom\s+/i, "")].filter(Boolean).join(" · ");
  const tone = item.course ? "border-l-primary bg-primary-soft" : "border-l-event bg-event-soft";
  const line = "m-0 overflow-hidden text-ellipsis whitespace-nowrap";
  return (
    <td
      rowSpan={span}
      colSpan={colSpan}
      height={span * SLOT_PX}
      className={`rounded-lg border-0 border-y-2 border-r-2 border-l-[3px] border-solid border-y-card border-r-card px-3 align-top ${tone}`}
    >
      <Text className={`${line} pt-[5px] text-[14px] font-semibold leading-[18px] text-foreground`}>{item.title}</Text>
      {span > 1 && <Text className={`${line} text-[12px] leading-4 text-muted-foreground`}>{where}</Text>}
    </td>
  );
}

function DayCalendar({ items }: { items: ScheduleItem[] }) {
  const allDay = items.filter((i) => i.all_day);
  const { placed, lanes, first, last } = place(items);
  const slotOf = (m: number) => Math.floor((m - first) / SLOT_MIN);
  const endSlot = (p: Placed) => Math.ceil((p.to - first) / SLOT_MIN);
  const covers = (p: Placed, s: number) => slotOf(p.from) <= s && s < endSlot(p);
  const rows = Array.from({ length: (last - first) / SLOT_MIN }, (_, s) => {
    const at = first + s * SLOT_MIN;
    const onHour = at % 60 === 0;
    const line = onHour ? "border-0 border-t border-solid border-border" : "";
    const cells: ReactNode[] = [];
    cells.push(
      <td key="h" width={48} height={SLOT_PX} className={`pr-2 align-top ${line}`}>
        {onHour && (
          <Text className="m-0 pt-[3px] font-mono text-[12px] leading-4 text-subtle">
            {String(at / 60).padStart(2, "0")}:00
          </Text>
        )}
      </td>,
    );
    for (let lane = 0; lane < lanes; lane++) {
      const wide = placed.find((p) => p.alone && covers(p, s));
      if (wide) {
        if (lane === 0 && slotOf(wide.from) === s) {
          cells.push(<EventCell key="w" p={wide} span={endSlot(wide) - s} colSpan={lanes} />);
        }
        continue;
      }
      const here = placed.find((p) => !p.alone && p.lane === lane && covers(p, s));
      if (here) {
        if (slotOf(here.from) === s) cells.push(<EventCell key={lane} p={here} span={endSlot(here) - s} colSpan={1} />);
        continue;
      }
      cells.push(<td key={lane} className={line} height={SLOT_PX} />);
    }
    return (
      <tr key={s} style={{ height: SLOT_PX }}>
        {cells}
      </tr>
    );
  });

  return (
    <Card>
      <CardContent className="px-5 pb-5 pt-5">
        {allDay.length > 0 && (
          <Section className="mb-4">
            <Text className="m-0 mb-2 text-[12px] font-medium uppercase leading-4 tracking-[0.08em] text-subtle">
              All day
            </Text>
            {allDay.map((i) => (
              <Badge key={i.title} variant={i.course ? "success" : "secondary"} className="mb-1 mr-1">
                {i.title}
              </Badge>
            ))}
          </Section>
        )}
        {placed.length === 0 ? (
          <Text className="m-0 py-2 text-[16px] leading-6 text-muted-foreground">Nothing on the calendar today.</Text>
        ) : (
          <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={{ tableLayout: "fixed" }}>
            <tbody>{rows}</tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

// -- the email ----------------------------------------------------------------

export function MorningBrief({ brief }: { brief: Brief }) {
  const now = new Date(brief.generated_at);
  const synced = brief.sync.last
    ? `Canvas synced ${new Date(brief.sync.last).toLocaleString("en-US", { timeZone: TZ, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`
    : "Canvas never synced";
  return (
    <Html lang="en">
      <Tailwind config={shadcnTheme}>
        <Head>
          <meta name="color-scheme" content="dark" />
          <meta name="supported-color-schemes" content="dark" />
          <style>
            {GEIST.map(
              ([family, weight, url]) =>
                `@font-face{font-family:'${family}';font-style:normal;font-weight:${weight};font-display:swap;src:url(${url}) format('woff2')}`,
            ).join("") + RESPONSIVE}
          </style>
        </Head>
        <Preview>{brief.headline}</Preview>
        <Body className="m-0 bg-background py-8 font-sans">
          <Container className="mx-auto w-full max-w-[640px] px-4">
            <Section className="px-1">
              <Text className="m-0 text-[13px] font-medium uppercase leading-5 tracking-[0.12em] text-primary">
                Morning brief
              </Text>
              <Text className="m-0 mt-1 text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
                {newsletterTitle(brief.date)}
              </Text>
            </Section>

            {brief.homework.length > 0 && (
              <>
                <SectionTitle>Homework</SectionTitle>
                <HomeworkGrid homework={brief.homework} now={now} />
              </>
            )}

            <SectionTitle>Today's schedule</SectionTitle>
            <DayCalendar items={brief.schedule} />

            {brief.gaps.length > 0 && (
              <>
                <SectionTitle>Heads up</SectionTitle>
                <Card>
                  <CardContent className="pt-4">
                    {brief.gaps.map((g, i) => (
                      <Section key={g}>
                        {i > 0 && <Separator className="my-1" />}
                        <Row className="py-1.5">
                          <Column className="w-[18px] align-top">
                            <Text className="m-0 text-[15px] leading-6 text-warning">•</Text>
                          </Column>
                          <Column className="align-top">
                            <Text className="m-0 text-[15px] leading-6 text-muted-foreground">{g}</Text>
                          </Column>
                        </Row>
                      </Section>
                    ))}
                  </CardContent>
                </Card>
              </>
            )}

            <Text className="m-0 mt-8 text-center text-[13px] leading-5 text-subtle">
              <span className={brief.sync.fresh ? "text-primary" : "text-warning"}>●</span> {synced}
              {brief.sync.fresh ? "" : " (stale)"} · mitsync
            </Text>
          </Container>
        </Body>
      </Tailwind>
    </Html>
  );
}

export default MorningBrief;
