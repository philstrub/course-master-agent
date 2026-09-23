// The morning-brief dashboard. Pure presentation: every word comes from the
// JSON the agent wrote (validated against brief.schema.json) plus the facts
// `mitsync email` added. No judgment happens here.
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

export type Brief = {
  date: string;
  headline: string;
  homework: Homework[];
  gaps: string[];
  classes: { time: string; title: string; course: string | null }[];
  sync: { last: string | null; fresh: boolean };
  generated_at: string;
};

const TZ = "America/New_York";

const urgency: Record<Homework["urgency"], { label: string; variant: BadgeVariant }> = {
  now: { label: "Due soon", variant: "destructive" },
  soon: { label: "This week", variant: "warning" },
  later: { label: "Later", variant: "secondary" },
};

const status: Record<Homework["status"], { label: string; variant: BadgeVariant }> = {
  ready_to_submit: { label: "Ready to submit", variant: "success" },
  in_progress: { label: "In progress", variant: "default" },
  not_started: { label: "Not started", variant: "outline" },
  submitted: { label: "Submitted", variant: "secondary" },
  unknown: { label: "Unknown", variant: "outline" },
};

function dueLabel(dueAt: string, now: Date): string {
  const due = new Date(dueAt);
  const when = due.toLocaleString("en-US", {
    timeZone: TZ,
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const hours = Math.round((due.getTime() - now.getTime()) / 3_600_000);
  const rel = hours < 0 ? "overdue" : hours < 48 ? `in ${hours} h` : `in ${Math.round(hours / 24)} days`;
  return `${when} · ${rel}`;
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <Card className="mx-1">
      <CardHeader className="px-4 pb-4 pt-4">
        <Text className="m-0 text-2xl font-bold leading-8 text-foreground">{value}</Text>
        <Text className="m-0 text-xs leading-4 text-muted-foreground">{label}</Text>
      </CardHeader>
    </Card>
  );
}

function HomeworkCard({ hw, now }: { hw: Homework; now: Date }) {
  const u = urgency[hw.urgency];
  const s = status[hw.status];
  return (
    <Card className="mb-4">
      <CardHeader>
        <Row>
          <Column>
            <Text className="m-0 text-xs font-medium uppercase leading-4 tracking-wide text-muted-foreground">
              {hw.course}
            </Text>
          </Column>
          <Column align="right">
            <Badge variant={u.variant}>{u.label}</Badge>
          </Column>
        </Row>
        <CardTitle className="mt-1">{hw.title}</CardTitle>
        <CardDescription>{dueLabel(hw.due_at, now)}</CardDescription>
      </CardHeader>
      <CardContent>
        <Row className="mb-2">
          <Column>
            <Badge variant={s.variant}>{s.label}</Badge>
          </Column>
          <Column align="right">
            <Text className="m-0 text-xs leading-4 text-muted-foreground">
              {hw.progress === null ? "progress unknown" : `${hw.progress}%`}
              {hw.effort_hours !== null ? ` · ~${hw.effort_hours} h left` : ""}
            </Text>
          </Column>
        </Row>
        {hw.progress !== null && <Progress value={hw.progress} className="mb-3" />}
        <Text className="m-0 text-sm leading-5 text-foreground">{hw.summary}</Text>
        <Text className="m-0 mt-2 text-sm leading-5 text-foreground">
          <strong>Next:</strong> {hw.next_step}
        </Text>
        {hw.review.length > 0 && (
          <Section className="mt-3 rounded-lg bg-muted px-3 py-2">
            <Text className="m-0 mb-1 text-xs font-semibold uppercase leading-4 tracking-wide text-muted-foreground">
              Review
            </Text>
            {hw.review.map((r) => (
              <Text key={r.file + r.where} className="m-0 py-0.5 text-sm leading-5 text-foreground">
                <code className="text-xs">{r.file.split("/").pop()}</code> · {r.where}
                <span className="text-muted-foreground"> · {r.why}</span>
              </Text>
            ))}
          </Section>
        )}
      </CardContent>
    </Card>
  );
}

export function MorningBrief({ brief }: { brief: Brief }) {
  const now = new Date(brief.generated_at);
  const open = brief.homework.filter((h) => h.status !== "submitted");
  const day = now.toLocaleDateString("en-US", {
    timeZone: TZ,
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  return (
    <Html lang="en">
      <Tailwind config={shadcnTheme}>
        <Head />
        <Preview>{brief.headline}</Preview>
        <Body className="m-0 bg-muted py-6 font-sans">
          <Container className="mx-auto w-full max-w-[600px] px-3">
            <Text className="m-0 text-xs font-medium uppercase leading-4 tracking-wide text-muted-foreground">
              Morning brief · {day}
            </Text>
            <Text className="m-0 mt-1 text-xl font-bold leading-7 text-foreground">
              {brief.headline}
            </Text>
            <Text className="m-0 mb-4 mt-1 text-xs leading-4 text-muted-foreground">
              {brief.sync.last
                ? `Canvas synced ${new Date(brief.sync.last).toLocaleString("en-US", { timeZone: TZ, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${brief.sync.fresh ? "" : " (stale)"}`
                : "Canvas has never been synced"}
            </Text>

            <Row className="mb-4">
              <Column className="w-1/3">
                <Stat value={open.filter((h) => h.urgency === "now").length} label="due within 48 h" />
              </Column>
              <Column className="w-1/3">
                <Stat value={open.filter((h) => h.status === "in_progress").length} label="in progress" />
              </Column>
              <Column className="w-1/3">
                <Stat value={open.filter((h) => h.status === "not_started").length} label="not started" />
              </Column>
            </Row>

            {brief.homework.map((hw) => (
              <HomeworkCard key={hw.course + hw.title} hw={hw} now={now} />
            ))}

            <Card className="mb-4">
              <CardHeader>
                <CardTitle>Today</CardTitle>
              </CardHeader>
              <CardContent>
                {brief.classes.length === 0 && (
                  <Text className="m-0 text-sm text-muted-foreground">No classes on the calendar.</Text>
                )}
                {brief.classes.map((c, i) => (
                  <Section key={c.time + c.title}>
                    {i > 0 && <Separator />}
                    <Row className="py-2">
                      <Column className="w-16 align-top">
                        <Text className="m-0 text-sm font-semibold leading-5 text-foreground">{c.time}</Text>
                      </Column>
                      <Column>
                        <Text className="m-0 text-sm leading-5 text-foreground">{c.title}</Text>
                      </Column>
                    </Row>
                  </Section>
                ))}
              </CardContent>
            </Card>

            {brief.gaps.length > 0 && (
              <Card className="mb-4 border-warning bg-warning">
                <CardContent className="pt-4">
                  <Text className="m-0 mb-1 text-sm font-semibold leading-5 text-warning-foreground">
                    What I couldn't see
                  </Text>
                  {brief.gaps.map((g) => (
                    <Text key={g} className="m-0 text-sm leading-5 text-warning-foreground">
                      · {g}
                    </Text>
                  ))}
                </CardContent>
              </Card>
            )}

            <Text className="m-0 text-center text-xs leading-4 text-muted-foreground">
              Written by your OpenClaw agent with mitsync · reply is not monitored
            </Text>
          </Container>
        </Body>
      </Tailwind>
    </Html>
  );
}

export default MorningBrief;
