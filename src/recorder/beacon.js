export const BEACON_SPLIT_FLOOR_BYTES = 16 * 1024;

export function beaconEvents(events, encode, send, fallback) {
  const json = encode(events);
  if (send(json)) return true;
  if (events.length > 1 && json.length > BEACON_SPLIT_FLOOR_BYTES) {
    const mid = Math.ceil(events.length / 2);
    const first = beaconEvents(events.slice(0, mid), encode, send, fallback);
    const rest = beaconEvents(events.slice(mid), encode, send, fallback);
    return first && rest;
  }
  fallback(json);
  return false;
}
