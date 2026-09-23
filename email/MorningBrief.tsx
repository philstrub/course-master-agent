// The morning-brief dashboard. Pure presentation: every word comes from the
// JSON the agent wrote (validated against brief.schema.json) plus the facts
// `mitsync email` added. No judgment happens here.
//
// Dark by design (zinc + emerald), three widgets: what's due, today's
// classes, and what the agent couldn't see. `summary` is the agent's
// evidence; it stays in the JSON for tomorrow's run and is not shown.
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
import { Badge, type BadgeVariant } from "./components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./components/ui/card";
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

export type ClassSlot = {
  time: string;
  end?: string;
  title: string;
  course: string | null;
  location?: string | null;
};

export type Brief = {
  date: string;
  headline: string;
  homework: Homework[];
  gaps: string[];
  classes: ClassSlot[];
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
  ["Geist Mono", 500, `${GF}/geistmono/v6/or3yQ6H-1_WfwkMZI_qYPLs1a-t7PU0AbeEPKK5U5Cl4PuCTTNs.woff2`],
];

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
  later: "text-muted-foreground",
};

function localDay(d: Date): string {
  return d.toLocaleDateString("en-CA", { timeZone: TZ }); // YYYY-MM-DD
}

function clock(d: Date): string {
  return d.toLocaleTimeString("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

// "Tonight 23:59", "Tomorrow 12:00", "Mon 28 · 23:59"; plus "in 9 h" / "in 5 d".
function due(dueAt: string, now: Date): { when: string; rel: string } {
  const d = new Date(dueAt);
  const days = Math.round(
    (Date.parse(localDay(d)) - Date.parse(localDay(now))) / 86_400_000,
  );
  const time = clock(d);
  const when =
    days === 0
      ? `${d.getTime() > now.getTime() && Number(time.slice(0, 2)) >= 17 ? "Tonight" : "Today"} ${time}`
      : days === 1
        ? `Tomorrow ${time}`
        : `${d.toLocaleDateString("en-US", { timeZone: TZ, weekday: "short" })} ${d.toLocaleDateString("en-US", { timeZone: TZ, day: "numeric" })} · ${time}`;
  const hours = (d.getTime() - now.getTime()) / 3_600_000;
  const rel = hours < 0 ? "overdue" : hours < 48 ? `in ${Math.max(1, Math.round(hours))} h` : `in ${Math.round(hours / 24)} d`;
  return { when, rel };
}

function HomeworkRow({ hw, now }: { hw: Homework; now: Date }) {
  const s = status[hw.status];
  const d = due(hw.due_at, now);
  return (
    <Section className="py-5">
      <Row>
        <Column className="align-top">
          <Text className="m-0 text-[14px] leading-5 text-subtle">{hw.course}</Text>
          <Text className="m-0 mt-1 text-[18px] font-semibold leading-6 text-foreground">{hw.title}</Text>
        </Column>
        <Column align="right" className="w-[150px] align-top">
          <Text className={`m-0 text-[15px] font-medium leading-5 ${dueTone[hw.urgency]}`}>{d.when}</Text>
          <Text className="m-0 mt-1 text-[14px] leading-5 text-subtle">{d.rel}</Text>
        </Column>
      </Row>
      <Row className="mt-3">
        <Column className="align-middle">
          <Badge variant={s.variant}>{s.label}</Badge>
        </Column>
        <Column align="right" className="align-middle">
          <Text className="m-0 text-[14px] leading-5 text-muted-foreground">
            {hw.progress === null ? "" : `${hw.progress}%`}
            {hw.effort_hours ? `${hw.progress === null ? "" : " · "}~${hw.effort_hours} h left` : ""}
          </Text>
        </Column>
      </Row>
      {hw.progress !== null && hw.status !== "submitted" && <Progress value={hw.progress} className="mt-3" />}
      <Text className="m-0 mt-3 text-[16px] leading-6 text-secondary-foreground">
        <span className="text-primary">→</span> {hw.next_step}
      </Text>
      {hw.review.length > 0 && (
        <Section className="mt-3 rounded-xl bg-muted px-4 py-3">
          {hw.review.map((r) => (
            <Text key={r.file + r.where} className="m-0 py-[2px] text-[14px] leading-5 text-muted-foreground">
              <span className="font-medium text-foreground">{r.file.split("/").pop()}</span>
              <span className="text-primary"> · {r.where}</span> · {r.why}
            </Text>
          ))}
        </Section>
      )}
    </Section>
  );
}

function ClassRow({ c }: { c: ClassSlot }) {
  const where = [c.location, c.course].filter(Boolean).join(" · ");
  return (
    <Row className="py-3">
      <Column className="w-[64px] align-top">
        <Text className="m-0 font-mono text-[15px] font-medium leading-6 text-primary">{c.time}</Text>
        {c.end && <Text className="m-0 font-mono text-[13px] leading-5 text-subtle">{c.end}</Text>}
      </Column>
      <Column className="border-0 border-l-2 border-solid border-primary-soft pl-4 align-top">
        <Text className="m-0 text-[16px] font-medium leading-6 text-foreground">{c.title}</Text>
        {where && <Text className="m-0 text-[14px] leading-5 text-muted-foreground">{where}</Text>}
      </Column>
    </Row>
  );
}

export function MorningBrief({ brief }: { brief: Brief }) {
  const now = new Date(brief.generated_at);
  const day = new Date(`${brief.date}T12:00:00Z`);
  const longDay = day.toLocaleDateString("en-US", { timeZone: TZ, weekday: "long", month: "long", day: "numeric" });
  const shortDay = day.toLocaleDateString("en-US", { timeZone: TZ, weekday: "short", day: "numeric", month: "short" });
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
            ).join("")}
          </style>
        </Head>
        <Preview>{brief.headline}</Preview>
        <Body className="m-0 bg-background py-8 font-sans">
          <Container className="mx-auto w-full max-w-[640px] px-4">
            <Section className="px-2 pb-6">
              <Text className="m-0 text-[14px] font-medium leading-5 text-primary">{longDay}</Text>
              <Text className="m-0 mt-2 text-[26px] font-semibold leading-[34px] tracking-[-0.01em] text-foreground">
                {brief.headline}
              </Text>
            </Section>

            {brief.homework.length > 0 && (
              <Card className="mb-4">
                <CardHeader>
                  <CardTitle>Due</CardTitle>
                </CardHeader>
                <CardContent className="pb-1 pt-0">
                  {brief.homework.map((hw, i) => (
                    <Section key={hw.course + hw.title}>
                      {i > 0 && <Separator />}
                      <HomeworkRow hw={hw} now={now} />
                    </Section>
                  ))}
                </CardContent>
              </Card>
            )}

            <Card className="mb-4">
              <CardHeader action={<CardDescription>{shortDay}</CardDescription>}>
                <CardTitle>Today</CardTitle>
              </CardHeader>
              <CardContent className="pt-2">
                {brief.classes.length === 0 ? (
                  <Text className="m-0 py-2 text-[16px] leading-6 text-muted-foreground">No classes today.</Text>
                ) : (
                  brief.classes.map((c) => <ClassRow key={c.time + c.title} c={c} />)
                )}
              </CardContent>
            </Card>

            {brief.gaps.length > 0 && (
              <Card className="mb-4">
                <CardHeader>
                  <CardTitle>Heads up</CardTitle>
                </CardHeader>
                <CardContent className="pt-2">
                  {brief.gaps.map((g) => (
                    <Row key={g} className="py-1.5">
                      <Column className="w-[18px] align-top">
                        <Text className="m-0 text-[15px] leading-6 text-warning">•</Text>
                      </Column>
                      <Column className="align-top">
                        <Text className="m-0 text-[15px] leading-6 text-muted-foreground">{g}</Text>
                      </Column>
                    </Row>
                  ))}
                </CardContent>
              </Card>
            )}

            <Text className="m-0 mt-2 text-center text-[13px] leading-5 text-subtle">
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
