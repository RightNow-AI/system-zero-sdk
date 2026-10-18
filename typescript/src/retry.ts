const capacityCodes = new Set(['capacity_exhausted', 'rate_limit', 'rate_limit_exceeded']);

/** Classify HTTP responses for bounded client recovery. */
export function shouldRetry(status: number, code: string | null, headers: Headers, retryable?: boolean, _nowMs: number = Date.now()): boolean {
  const hint = headers.get('x-should-retry');
  if (hint === 'false' || retryable === false) return false;
  const marked = hint === 'true' || retryable === true;
  if (status === 429) return marked && code !== null && capacityCodes.has(code);
  if (status !== 503) return false;
  if (code === 'service_unavailable' && marked) return true;
  return false;
}

/** Workflow recovery accepts explicit admission code/cause pairs. */
export function shouldRecoverWorkflow(status: number, code: string | null, headers: Headers,
  retryable?: boolean, details?: Readonly<Record<string, unknown>>): boolean {
  const hint = headers.get('x-should-retry');
  if (status !== 429 || hint === 'false' || retryable === false || !(hint === 'true' || retryable === true)) return false;
  const cause = details?.cause;
  // Only these code and cause pairs establish refusal before work started.
  return code === 'rate_limit_exceeded' && (cause === 'key_rate' || cause === 'preauth')
    || code === 'concurrency_limit_exceeded' && cause === 'account_concurrency';
}

function nonnegativeNumber(value: string | null): number | null {
  if (value === null || !/^\d+(?:\.\d+)?$/u.test(value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

/** Every valid server delay is a minimum, including when both headers are present. */
export function retryAfterMs(headers: Headers, nowMs: number = Date.now()): number | null {
  const milliseconds = nonnegativeNumber(headers.get('retry-after-ms'));
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
  if (milliseconds === null) return delay;
  return delay === null ? milliseconds : Math.max(milliseconds, delay);
}

/** Milliseconds; retryIndex is zero for the first retry. */
export function retryWaitMs(headers: Headers, retryIndex: number, random: () => number = Math.random, nowMs: number = Date.now()): number {
  const jitter = Math.min(8000, 500 * 2 ** retryIndex) * random();
  return Math.max(jitter, retryAfterMs(headers, nowMs) ?? 0);
}
