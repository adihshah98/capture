export let currentProject = '';

export function setProject(token) {
  currentProject = token;
}

export function projectQuery() {
  return currentProject ? `?project=${encodeURIComponent(currentProject)}` : '';
}

export async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    throw new Error('Server unreachable, or it needs the password again — try reloading.');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
  }
  if (!res.ok) {
    if (res.status === 401) {
      if (body && body.login) {
        window.location = body.login;
        throw new Error('Signing in…');
      }
      throw new Error('Not authorized — reload the page to re-enter the replay password.');
    }
    throw new Error((body && body.error) || `Server returned ${res.status}.`);
  }
  return body || {};
}
