import { metric } from "@managed-agents/preview";

/** A2A SDK 1.3 logs raw invalid SSE data through console.error. Do not log bodies. */
export function metadataOnlyLibraryLogs() {
  const error = console.error, warn = console.warn;
  console.error = () => metric("web_library_error");
  console.warn = () => metric("web_library_warning");
  return () => { console.error = error; console.warn = warn; };
}
