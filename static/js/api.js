// Обращения к серверу
async function req(method, path, body, params) {
  const url = new URL(path, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    });
  }
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(url, opts);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `Ошибка ${r.status}`);
  return data;
}

export const api = {
  get: (p, params) => req('GET', p, undefined, params),
  post: (p, body) => req('POST', p, body),
  patch: (p, body) => req('PATCH', p, body),
  put: (p, body) => req('PUT', p, body),
  del: (p) => req('DELETE', p),
  /** загрузка с прогрессом */
  upload(form, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('POST', '/api/cases');
      x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      x.onload = () => {
        let d = null;
        try { d = JSON.parse(x.responseText); } catch (e) { /* */ }
        if (x.status >= 200 && x.status < 300) resolve(d);
        else reject(new Error((d && d.error) || `Ошибка ${x.status}`));
      };
      x.onerror = () => reject(new Error('Сеть недоступна'));
      x.send(form);
    });
  },
};
