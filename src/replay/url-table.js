import { pagePath } from './format.js';
import { queryParams } from './params.js';

const urlParamsEl = document.getElementById('url-params');

export function clearUrlTable() {
  urlParamsEl.innerHTML = '<div class="empty">Page URLs and their query params appear here.</div>';
}

export function renderUrlTable(navigations) {
  urlParamsEl.innerHTML = '';
  if (!navigations.length) {
    clearUrlTable();
    return;
  }
  for (const nav of navigations) {
    const url = document.createElement('div');
    url.className = 'url';
    url.textContent = pagePath(nav.url).split('?')[0];
    url.title = nav.url;
    urlParamsEl.appendChild(url);
    const params = queryParams(nav.url);
    if (!params.length) continue;
    const table = document.createElement('table');
    for (const { key, value } of params) {
      const row = document.createElement('tr');
      const k = document.createElement('td');
      k.className = 'k';
      k.textContent = key;
      const v = document.createElement('td');
      v.textContent = value;
      row.append(k, v);
      table.appendChild(row);
    }
    urlParamsEl.appendChild(table);
  }
}
