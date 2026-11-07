/**
 * Asks the feed whether it refuses a stream URL (HTTP 400, its answer for an unknown asset), without
 * keeping the stream open. Any other outcome, including a network failure, is "not rejected": a feed
 * that is merely down must not make the page forget the stored asset.
 */
export async function streamRejected(url: string, fetchImpl: typeof fetch = fetch): Promise<boolean> {
  const abort = new AbortController();
  try {
    const res = await fetchImpl(url, { signal: abort.signal });
    return res.status === 400;
  } catch {
    return false;
  } finally {
    abort.abort(); // headers are enough; do not hold a stream open
  }
}
