const deadline = Date.now() + 90_000;
for (const url of ['http://127.0.0.1:3001/health', 'http://127.0.0.1:3000']) {
  while (true) {
    try {
      const response = await fetch(url, {signal: AbortSignal.timeout(3000)});
      if (response.ok) break;
    } catch { /* readiness polling */ }
    if (Date.now() > deadline) throw new Error(`Readiness timeout: ${url}; inspect test-results/services`);
    await new Promise(resolve => setTimeout(resolve, 200));
  }
}
