// Shared by every signed-in page: the CSRF token on every change, a sign-in
// redirect when the session ends, and small helpers.
(function () {
  let CSRF = '';
  const realFetch = window.fetch.bind(window);
  async function token() {
    if (CSRF) return CSRF;
    try {
      const r = await realFetch('/api/auth/state', {credentials: 'same-origin'});
      const d = await r.json();
      CSRF = d.csrf || '';
    } catch (e) {}
    return CSRF;
  }
  window.setCsrf = t => { CSRF = t || ''; };
  window.fetch = async (url, opts = {}) => {
    opts = Object.assign({credentials: 'same-origin'}, opts);
    const method = (opts.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD'].includes(method)) {
      const h = new Headers(opts.headers || {});
      h.set('X-CSRF-Token', await token());
      opts.headers = h;
    }
    const res = await realFetch(url, opts);
    const local = typeof url === 'string' && url.startsWith('/') && !url.startsWith('/api/auth/');
    if (res.status === 401 && local) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    }
    return res;
  };
  window.esc = s => (s ?? '').toString().replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  window.when = t => t ? new Date(t * 1000).toLocaleString('en-GB',
    {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—';
  window.apiError = async res => {
    try { const d = await res.json(); return d.detail || JSON.stringify(d); }
    catch (e) { return res.status + ' ' + res.statusText; }
  };
  window.signOut = async () => {
    await fetch('/api/auth/logout', {method: 'POST'});
    location.href = '/login';
  };
})();
