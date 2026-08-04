const PORTS = [24680, 7100];

async function findBackend() {
  for (const port of PORTS) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/version`, { signal: AbortSignal.timeout(1500) });
      if (r.ok) return port;
    } catch (e) { /* не слушает */ }
  }
  return null;
}

document.getElementById('send').addEventListener('click', async () => {
  const st = document.getElementById('status');
  st.className = '';
  st.textContent = 'Ищу DeckPipe…';
  const port = await findBackend();
  if (!port) {
    st.className = 'err';
    st.textContent = 'Приложение DeckPipe не запущено.\nЗапустите его и попробуйте снова.';
    return;
  }
  const dz = await chrome.cookies.getAll({ domain: 'deezer.com', name: 'arl' });
  const sc = await chrome.cookies.getAll({ domain: 'soundcloud.com', name: 'oauth_token' });
  const payload = {};
  if (dz.length) payload.arl = dz[0].value;
  if (sc.length) payload.oauth_token = sc[0].value;
  if (!payload.arl && !payload.oauth_token) {
    st.className = 'err';
    st.textContent = 'Вход не найден: сначала войдите на deezer.com или soundcloud.com в этом браузере.';
    return;
  }
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/login/from-browser`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    const ok = [], fail = [];
    if (d.deezer) ok.push(`Deezer: ${d.deezer}`);
    if (d.sc) ok.push(`SoundCloud: ${d.sc}`);
    if (d.deezer_error) fail.push('Deezer: ' + d.deezer_error);
    if (d.sc_error) fail.push('SC: ' + d.sc_error);
    st.className = ok.length ? 'ok' : 'err';
    st.textContent = [...ok.map(x => '✔ ' + x), ...fail.map(x => '✖ ' + x)].join('\n') || '✖ ничего не принято';
  } catch (e) {
    st.className = 'err';
    st.textContent = 'Ошибка отправки: ' + e.message;
  }
});
