import { useEffect, useRef } from 'react';

/**
 * Calls `callback` every `intervalMs` milliseconds while the browser tab is
 * visible. Polling is paused automatically when the user switches away and
 * resumes when they come back — avoiding wasted API calls for hidden tabs.
 *
 * @param callback   Function to call on each tick. Wrap in useCallback to
 *                   avoid restarting the interval on every render.
 * @param intervalMs Polling interval in ms. Pass 0 to disable.
 * @param enabled    Set to false to pause (e.g. while PAT is not connected).
 */
export function useAutoRefresh(
  callback: () => void,
  intervalMs: number,
  enabled = true,
): void {
  // Keep a stable ref so we can update the callback without restarting the timer.
  const savedCallback = useRef(callback);
  useEffect(() => {
    savedCallback.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;

    const tick = () => {
      if (typeof document === 'undefined') return;
      if (document.visibilityState === 'visible') {
        savedCallback.current();
      }
    };

    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, enabled]);
}
