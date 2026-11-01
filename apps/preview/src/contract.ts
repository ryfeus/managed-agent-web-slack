/** Ephemeral assistant text only. No provider identity or authoritative state. */
export type Preview = {
  v: 1; taskId: string; previewId: string; offset: number;
  type: "start" | "delta" | "end"; text?: string;
};
export const MAX_FRAME_BYTES = 8192;
export const MAX_PREVIEW_BYTES = 262144;
export function parsePreview(value: unknown, taskId: string): Preview | undefined {
  if (!value || typeof value !== "object") return;
  const p = value as Preview;
  if (Object.keys(p).some(k => !["v", "taskId", "previewId", "offset", "type", "text"].includes(k))) return;
  if (p.v !== 1 || p.taskId !== taskId || !/^[a-f0-9]{64}$/.test(p.previewId)
    || !Number.isSafeInteger(p.offset) || p.offset < 0 || !["start", "delta", "end"].includes(p.type)) return;
  if (p.type === "delta" ? typeof p.text !== "string" || !p.text : p.text !== undefined) return;
  if (Buffer.byteLength(JSON.stringify(p)) > MAX_FRAME_BYTES) return;
  return p;
}

/** A gap invalidates this disposable message; never concatenate a later suffix. */
export class PreviewAccumulator {
  private states = new Map<string, { next: number; text: string; ended: boolean }>();
  private bytes = 0;
  accept(p: Preview): { id: string; text: string; start: boolean; end: boolean } | undefined {
    let state = this.states.get(p.previewId);
    if (!state) {
      if (p.type !== "start" || p.offset !== 0 || this.states.size >= 32) return;
      state = { next: 0, text: "", ended: false };
      this.states.set(p.previewId, state);
    }
    if (state.ended || p.offset < state.next) return;
    if (p.offset !== state.next || (p.type === "start" && state.next !== 0)) {
      state.ended = true;
      return;
    }
    state.next++;
    if (p.text) {
      this.bytes += Buffer.byteLength(p.text);
      if (this.bytes > MAX_PREVIEW_BYTES) { state.ended = true; return; }
      state.text += p.text;
    }
    state.ended = p.type === "end";
    return { id: `preview-${p.previewId}`, text: state.text, start: p.type === "start", end: state.ended };
  }
}

export function metric(event: string, fields: Record<string, string | number | boolean> = {}) {
  // Callers supply identifiers/counters only; never pass an Error or wire payload.
  process.stdout.write(JSON.stringify({ event, ...fields }) + "\n");
}
