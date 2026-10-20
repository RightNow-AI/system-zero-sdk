const capacityCodes = new Set(['capacity_exhausted', 'rate_limit', 'rate_limit_exceeded', 'concurrency_limit_exceeded', 'workspace_limit_exceeded']);

 
export function shouldRetry(status: number, code: string | null, headers: Headers, retryable?: boolean, nowMs: number = Date.now()): boolean {
  const hint = headers.get('x-should-retry');
  if (hint === 'false' || retryable === false) return false;
  const marked = hint === 'true' || retryable === true;
  if (status === 429) return marked && code !== null && capacityCodes.has(code);
  if (status === 409) return marked && code === 'idempotency_conflict';
  if (status !== 503) return false;
  if ((code === 'service_unavailable' || code === 'system_zero_unavailable') && marked) return true;
  const starting = code === 'starting' || ((code === null || code === 'service_unavailable') && ['true', '1'].includes(headers.get(('x-sz-' + 'starting')) ?? ''));
  return starting && retryAfterMs(headers, nowMs) !== null;
}

 
export function shouldRecoverWorkflow(status: number, code: string | null, headers: Headers,
  retryable?: boolean, details?: Readonly<Record<string, unknown>>): boolean {
  const hint = headers.get('x-should-retry');
  if (status !== 429 || hint === 'false' || retryable === false || !(hint === 'true' || retryable === true)) return false;
  const cause = details?.cause;
  // Structured node capacity shares cause capacity with gateway admission.
  // Neither that pair, an invented dispatched marker nor starting/5xx proves no work.
  return code === 'rate_limit_exceeded' && (cause === 'key_rate' || cause === 'preauth')
    || code === 'concurrency_limit_exceeded' && cause === 'account_concurrency';
}

function nonnegativeNumber(value: string | null): number | null {
  if (value === null || !/^\d+(?:\.\d+)?$/u.test(value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

/** The precise millisecond header takes precedence; seconds apply only when it is absent. */
export function retryAfterMs(headers: Headers, nowMs: number = Date.now()): number | null {
  if (headers.has('retry-after-ms')) return nonnegativeNumber(headers.get('retry-after-ms'));
  const raw = headers.get('retry-after');
  const seconds = nonnegativeNumber(raw);
  let delay = seconds === null ? null : seconds * 1000;
  // Date.parse accepts negative numbers as dates; only an HTTP date has a weekday.
  if (seconds === null && raw !== null && /^(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?),?\s/iu.test(raw.trim())) {
    const date = raw.trim();
    // Missing zones (including asctime) use GMT in both SDKs, never the host zone.
    const time = Date.parse(/\d{2}:\d{2}:\d{2}(?:\s+\d{4})?$/u.test(date) ? `${date} GMT` : date);
    if (Number.isFinite(time)) delay = Math.max(0, time - nowMs);
  }
  return delay;
}

/** Milliseconds; retryIndex is zero for the first retry. */
export function retryWaitMs(headers: Headers, retryIndex: number, random: () => number = Math.random, nowMs: number = Date.now()): number {
  const jitter = Math.min(8000, 500 * 2 ** retryIndex) * random();
  return Math.max(jitter, retryAfterMs(headers, nowMs) ?? 0);
}
