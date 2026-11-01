import { it, expect, vi } from "vitest";
import { metadataOnlyLibraryLogs } from "../src/logging.js";

it("library parse errors cannot emit transcript or tool/preview bodies", () => {
  const output = vi.spyOn(process.stdout, "write").mockReturnValue(true);
  const restore = metadataOnlyLibraryLogs();
  try {
    console.error("Failed to parse SSE event data:", { text: "private transcript" }, new Error("sensitive"));
    console.warn({ preview: "private" });
    expect(output.mock.calls.map(c => c[0])).toEqual(['{"event":"web_library_error"}\n', '{"event":"web_library_warning"}\n']);
  } finally { restore(); output.mockRestore(); }
});
