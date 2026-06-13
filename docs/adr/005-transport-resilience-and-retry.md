# ADR 005 — Transport Resilience: Timeouts, Redirect Bound, and Retry / Backoff

**Status:** accepted (2026-06-12, waves 0–2)

## Problem

The v0.2.11 principal audit (wave 0) surfaced three classes of transport
fragility in `UrllibTransport` and `PlaudClient`:

1. **No timeouts.** `urlopen` had no timeout parameter. A hung Plaud API
   response (network stall, proxy, slow link) would block the MCP server
   or CLI indefinitely with no recovery path.

2. **Unbounded region-redirect recursion.** Plaud returns a `-302` status
   code with a new base URL when a request hits the wrong region.
   `_request_json` called itself recursively on every `-302`. A server
   returning `-302` indefinitely would recurse until a stack overflow.

3. **No retry on transient errors.** HTTP 429 (rate limit) and 5xx (server
   error) responses propagated immediately as `PlaudApiError`. Callers
   received a hard failure for what was often a transient condition, and
   the poll loops in `wait_for_transcript` / `wait_for_summary` /
   `wait_for_merge` aborted on any error instead of retrying.

## Decisions

### Timeouts (Wave 0 / A1)

`UrllibTransport` gains a `timeout` parameter applied to every `urlopen`
call:

- **30 s default** for normal API calls.
- **120 s ceiling** for S3 multipart chunk PUTs (large recordings on slow
  links need more headroom).

A `URLError` or `socket.timeout` wrapping a timeout raises `PlaudApiError`
with a meaningful message. The timeout is injectable for tests.

### Region-redirect bound (Wave 0 / A1)

`_request_json` accepts a `_redirected: bool = False` flag. When the
first response is a `-302`, the client calls `_update_region` to persist
the new base URL and then retries exactly once with `_redirected=True`. A
second `-302` on the retry raises `PlaudApiError("region redirect loop")`
immediately. Infinite recursion is therefore impossible. The body is
forwarded through the redirect on POST/PATCH/DELETE.

### Retry / backoff (Wave 2 / C5)

`_request_json` wraps the transport call in a retry loop with these
properties:

- **Scope:** HTTP 429 and 5xx responses only. 4xx errors (except 429) are
  not retried — they indicate a client-side problem.
- **Attempts:** up to 2 retries (3 total attempts), controlled by
  `_MAX_ATTEMPTS = 3`.
- **Backoff:** exponential with ±25 % full jitter.
  - Attempt 0 → retry 1: base ≈ 1 s, actual ∈ [0.75, 1.25] s.
  - Attempt 1 → retry 2: base ≈ 3 s, actual ∈ [2.25, 3.75] s.
- **`Retry-After` header:** when present, the client sleeps
  `max(computed_delay, retry_after)` so it never retries faster than the
  server's hint.
- **Region redirect interaction:** the `_redirected` flag is propagated
  through the retry loop so a 429 retry cannot accidentally re-follow a
  redirect and recurse.
- **Poll loops:** `wait_for_transcript`, `wait_for_summary`, and
  `wait_for_merge` classify each exception via `exc.classify()`. A
  retryable error (429 / 5xx) is treated as a skipped poll tick; the loop
  continues until its deadline rather than aborting. Non-retryable errors
  (4xx, auth errors) still propagate immediately.

### Sleep / jitter injection

`_request_json` accepts injectable `_sleep_fn` and `_jitter_fn` parameters
(default: `time.sleep` and `random.uniform`). Tests pass deterministic
stubs, keeping the retry logic fully exercised without real sleeps.

## Alternatives rejected

- **Infinite retries with a total-time budget:** Rejected in favour of a
  fixed attempt count. A time budget is harder to reason about and test,
  and two retries cover the common transient-error case without masking
  persistent failures.
- **Client-level retry wrapper separate from `_request_json`:** Rejected.
  The `_redirected` flag must be visible to both redirect handling and
  retry handling, and composing two independent loops around the same
  state variable invites ordering bugs. A single function owns both
  concerns.
- **Retry on all 4xx responses:** Rejected. 400/401/403/404 responses
  indicate a caller-side error; retrying them wastes quota and masks bugs.
- **Fixed retry delay (no jitter):** Rejected. Without jitter, a fleet of
  clients that hit a rate limit simultaneously will all retry at the same
  instant, producing another rate-limit spike (thundering herd). Jitter
  spreads the retry load.
