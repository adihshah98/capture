export function queryParams(url) {
  try {
    const u = new URL(url);
    const params = [...u.searchParams].map(([key, value]) => ({ key, value }));
    const hash = u.hash.slice(1);
    if (/^[^/]+=/.test(hash)) {
      for (const [key, value] of new URLSearchParams(hash)) params.push({ key, value });
    }
    return params.filter((p) => p.key);
  } catch {
    return [];
  }
}
