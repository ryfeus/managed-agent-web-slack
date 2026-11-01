const deadline = Date.now() + 90_000;
for (const url of [`${process.env.E2E_BACKEND_URL || 'http://127.0.0.1:3001'}/health`, process.env.E2E_BASE_URL || 'http://127.0.0.1:3000']) {
  while (true) {
    try {
      const response = await fetch(url, {signal: AbortSignal.timeout(3000)});
      if (response.ok) break;
    } catch { /* readiness polling */ }
    if (Date.now() > deadline) throw new Error(`Readiness timeout: ${url}; inspect ${process.env.E2E_ARTIFACT_DIR || "test-results/dev"}`);
    await new Promise(resolve => setTimeout(resolve, 200));
  }
}
