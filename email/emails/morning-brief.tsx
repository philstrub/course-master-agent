// Preview for `npm run dev` (React Email's live server): the real template,
// fed with example.brief.json. Edit either and the browser reloads.
import example from "../example.brief.json";
import { MorningBrief, type Brief } from "../MorningBrief";

export default function Preview({ brief }: { brief: Brief }) {
  return <MorningBrief brief={brief} />;
}

Preview.PreviewProps = { brief: example as Brief };
