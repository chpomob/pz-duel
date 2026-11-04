'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const app = $('#app');

let S = null;                 // dernier état du service
let presets = [];             // presets chargés
let view = 'auto';            // 'auto' | 'config'
let draft = null;             // brouillon de configuration
let lastQr = null;            // signature du QR déjà rendu
let toastTimer = null;

// ---------------------------------------------------------------- réseau
async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  return res.json();
}

async function poll() {
  try {
    S = await api('/api/state');
    $('#svc').textContent = 'service : ok';
    $('#svc').className = 'badge badge-ok';
  } catch (e) {
    $('#svc').textContent = 'service injoignable';
    $('#svc').className = 'badge badge-bad';
    return;
  }
  render();
}

async function act(action, extra = {}) {
  const out = await api('/api/action', Object.assign({ action }, extra));
  if (!out.ok) toast(out.message || 'action refusée', true);
  if (out.state) { S = out.state; render(); }
  return out;
}

function toast(msg, bad = false) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.borderColor = bad ? 'var(--red)' : 'var(--amber)';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

function copy(text) {
  navigator.clipboard?.writeText(text).then(
    () => toast('copié'),
    () => toast('copie impossible', true)
  );
}

// ---------------------------------------------------------------- utils
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const durLabel = (s) => {
  s = Math.floor(s || 0);
  if (s < 120) return `${s} s`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m} min ${r}` : `${m} min`;
};

const mmss = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

function code5() {
  const a = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  return Array.from({ length: 5 }, () => a[Math.floor(Math.random() * a.length)]).join('');
}

function newDraft() {
  const p = presets[0] || { id: 'duel_rapide', duration_s: 1500, day_length: 1 };
  return {
    preset: p.id,
    duration_s: p.duration_s,
    day_length: p.day_length,
    pseudo: localStorage.getItem('pzduel.pseudo') || 'hôte',
    password: code5(),
  };
}

const presetOf = (id) => presets.find((p) => p.id === id) || presets[0] || { summary: [] };

// ---------------------------------------------------------------- vues
function render() {
  if (!S) return;
  const mock = S.mode === 'mock';
  $('#mode-badge').textContent = mock ? 'mode simulé' : 'serveurs réels';
  $('#mode-badge').className = 'badge ' + (mock ? 'badge-mock' : 'badge-ok');
  renderLogs();

  if (view === 'config') return renderConfig();

  switch (S.phase) {
    case 'idle': return renderHome();
    case 'over': return renderOver();
    case 'error': return renderError();
    default: return renderLive();
  }
}

function shell(inner) { app.innerHTML = inner; }

function serverChips() {
  const chip = (label, status) => {
    const cls = status === 'up' ? 'up' : status === 'starting' ? 'busy' : 'down';
    const txt = status === 'up' ? 'prêt' : status === 'starting' ? 'démarrage…' : 'arrêté';
    return `<span class="chip"><span class="dot ${cls}"></span>${label} · ${txt}</span>`;
  };
  return `<div class="srv">${chip('Serveur A', S.servers.A)}${chip('Serveur B', S.servers.B)}</div>`;
}

function renderHome() {
  const cfg = S.config && S.config.preset ? S.config : null;
  shell(`
    <section class="card">
      <h2>État</h2>
      <p class="sub">Le service tourne en local. Rien n'est lancé pour l'instant.</p>
      ${serverChips()}
      <div class="grid two" style="margin-top:14px">
        <div class="row"><span class="k">Joueur A</span><span class="v">${esc(S.players.A || '—')}</span></div>
        <div class="row"><span class="k">Joueur B</span><span class="v">${esc(S.players.B || '—')}</span></div>
      </div>
      <div class="actions">
        <button class="primary" data-action="config">Configurer un duel</button>
        ${cfg ? '<button class="ghost" data-action="rematch">Reprendre le dernier réglage</button>' : ''}
        <button class="ghost" data-action="logs">Voir les logs</button>
      </div>
    </section>
    ${cfg ? `<section class="card"><h2>Dernier réglage</h2>
      <div class="row"><span class="k">Preset</span><span class="v">${esc(cfg.preset_label || cfg.preset)}</span></div>
      <div class="row"><span class="k">Durée</span><span class="v">${durLabel(cfg.duration_s)}</span></div>
      <div class="row"><span class="k">Jour de jeu</span><span class="v">${dayLabel(cfg.day_length)}</span></div>
    </section>` : ''}
  `);
}

function dayLabel(v) {
  return { 1: '15 min', 2: '30 min', 3: '1 h', 4: '1 h 30' }[v] || `${v}`;
}

function renderConfig() {
  if (!draft) draft = newDraft();
  const p = presetOf(draft.preset);
  const durations = [300, 600, 900, 1200, 1500, 1800];
  if (!durations.includes(draft.duration_s)) durations.unshift(draft.duration_s);
  durations.sort((a, b) => a - b);

  shell(`
    <section class="card">
      <h2>Nouveau duel</h2>
      <p class="sub">Tu héberges les deux serveurs. Ton adversaire n'aura qu'à scanner le QR.</p>

      <label class="field"><span>Preset</span>
        <select id="f-preset">
          ${presets.map((x) => `<option value="${x.id}" ${x.id === draft.preset ? 'selected' : ''}>${esc(x.label)} — ${esc(x.tagline || '')}</option>`).join('')}
        </select>
      </label>

      <div class="grid two">
        <label class="field"><span>Durée du match</span>
          <select id="f-duration">
            ${durations.map((d) => `<option value="${d}" ${d === draft.duration_s ? 'selected' : ''}>${d < 120 ? d + ' s' : Math.round(d / 60) + ' min'}</option>`).join('')}
          </select>
        </label>
        <label class="field"><span>Jour de jeu</span>
          <select id="f-day">
            ${[1, 2, 3, 4].map((d) => `<option value="${d}" ${d === draft.day_length ? 'selected' : ''}>${dayLabel(d)}</option>`).join('')}
          </select>
        </label>
      </div>

      <div class="grid two">
        <label class="field"><span>Ton pseudo</span>
          <input id="f-pseudo" value="${esc(draft.pseudo)}" maxlength="20">
        </label>
        <label class="field"><span>Mot de passe du serveur</span>
          <input id="f-password" value="${esc(draft.password)}" maxlength="24">
        </label>
      </div>

      <div class="row"><span class="k">Mods requis</span>
        <span class="v">duelcore <span style="color:var(--green)">✓</span> · duelhud <span style="color:var(--green)">✓</span>
        <span class="hint"> (lecture seule)</span></span></div>

      <h2 style="margin-top:18px">Règles — ${esc(p.label)}</h2>
      <p class="sub">Aperçu. L'invité verra exactement ceci avant d'accepter.</p>
      <ul class="feed">
        ${(p.summary || []).map((line) => `<li>• ${esc(line)}</li>`).join('')}
      </ul>

      <div class="actions">
        <button class="ghost" data-action="back">Annuler</button>
        <button class="primary" data-action="start">Lancer le duel</button>
      </div>
    </section>
  `);

  $('#f-preset').onchange = (e) => {
    draft.preset = e.target.value;
    const np = presetOf(draft.preset);
    draft.duration_s = np.duration_s;
    draft.day_length = np.day_length;
    renderConfig();
  };
  $('#f-duration').onchange = (e) => { draft.duration_s = Number(e.target.value); };
  $('#f-day').onchange = (e) => { draft.day_length = Number(e.target.value); };
  $('#f-pseudo').oninput = (e) => { draft.pseudo = e.target.value; };
  $('#f-password').oninput = (e) => { draft.password = e.target.value; };
}

function stepsList() {
  const ic = { pending: '·', running: '⟳', ok: '✓', error: '✕' };
  return `<ul class="steps">
    ${S.steps.map((s) => `<li class="st-${s.state}">
      <span class="ic">${ic[s.state] || '·'}</span>
      <span>${esc(s.label)}</span>
      <span class="detail">${esc(s.detail || '')}</span>
    </li>`).join('')}
  </ul>`;
}

function renderLive() {
  const phase = S.phase;
  let body = '';

  if (phase === 'running' || phase === 'countdown') {
    body = `
      <section class="card">
        <div class="clock">${mmss(S.clock.remaining_s)}</div>
        <div class="scorebar">
          <div class="side"><div class="n">${S.score.A}</div><div class="who">${esc(S.players.A || 'A')}</div></div>
          <div class="vs">VS</div>
          <div class="side"><div class="n">${S.score.B}</div><div class="who">${esc(S.players.B || 'B')}</div></div>
        </div>
        <div class="row"><span class="k">Menaces / min</span><span class="v">${S.threats_per_min}</span></div>
        <div class="row"><span class="k">Dérive d'horloge</span><span class="v">${S.clock.drift_s ?? '—'} s</span></div>
        <div class="actions">${phase === 'running' ? '<button class="danger" data-action="cancel">Arrêter le match</button>' : ''}</div>
      </section>`;
    if (phase === 'countdown') {
      body = `<section class="card"><div class="countdown">${S.clock.countdown ?? 3}</div>
        <p class="hint" style="text-align:center">le duel commence</p></section>`;
    }
    if (S.events.length) {
      body += `<section class="card"><h2>Fil du match</h2>
        <ul class="feed">${S.events.slice().reverse().map((e) => `<li><span class="t">${e.t}</span>${esc(e.text)}</li>`).join('')}</ul>
      </section>`;
    }
    return shell(body + `<section class="card">${serverChips()}</section>`);
  }

  if (phase === 'lobby') {
    body = `
      <section class="card">
        <h2>Invitation</h2>
        <p class="sub">Envoie ce QR ou ce code à ton adversaire.</p>
        <div class="invite">
          <div class="qr" id="qr"></div>
          <div>
            <div class="code">${esc(S.invite.code || '')}</div>
            <div class="line">${esc(S.invite.ip)}:${S.invite.port}</div>
            <div class="line">mot de passe : <b>${esc(S.invite.password)}</b></div>
            <div class="actions">
              <button class="ghost" data-action="copy-link">Copier le lien</button>
              <button class="ghost" data-action="open-game">Ouvrir PZ</button>
              ${S.mode === 'mock' ? '<button class="ghost" data-action="simulate">Simuler l\'adversaire</button>' : ''}
            </div>
            <p class="hint" style="margin-top:10px">Adversaire : ${S.players.B ? esc(S.players.B) : 'en attente…'}</p>
          </div>
        </div>
      </section>`;
    shell(`<section class="card"><h2>Lancement</h2>${stepsList()}</section>` + body +
      `<section class="card"><div class="actions"><button class="danger" data-action="cancel">Annuler le duel</button></div></section>`);
    renderQr();
    return;
  }

  // preflight / starting / contract / stopping
  shell(`
    <section class="card">
      <h2>${esc(S.phase_label || phase)}</h2>
      <p class="sub">Chaque étape est vérifiée. Si ça bloque, tu vois où.</p>
      ${stepsList()}
      ${serverChips()}
      <div class="actions"><button class="danger" data-action="cancel">Annuler</button></div>
    </section>`);
}

function renderOver() {
  const a = S.score.A, b = S.score.B;
  const verdict = a === b ? 'égalité' : a > b ? 'victoire de A' : 'victoire de B';
  shell(`
    <section class="card">
      <h2>Match terminé</h2>
      <p class="sub">${esc(S.config.preset_label || '')} · ${durLabel(S.config.duration_s || 0)}</p>
      <div class="scorebar">
        <div class="side"><div class="n">${a}</div><div class="who">${esc(S.players.A || 'A')}</div></div>
        <div class="vs">${esc(verdict)}</div>
        <div class="side"><div class="n">${b}</div><div class="who">${esc(S.players.B || 'B')}</div></div>
      </div>
      <div class="actions">
        <button class="primary" data-action="rematch">Revanche</button>
        <button class="ghost" data-action="config">Nouveau duel</button>
      </div>
    </section>
    <section class="card"><h2>Fil du match</h2>
      <ul class="feed">${S.events.slice().reverse().map((e) => `<li><span class="t">${e.t}</span>${esc(e.text)}</li>`).join('')}</ul>
    </section>`);
}

function renderError() {
  shell(`<section class="card"><h2>Erreur</h2>
    <div class="err">${esc(S.error || 'erreur inconnue')}</div>
    <div class="actions"><button class="primary" data-action="config">Nouveau duel</button></div>
  </section>`);
}

function renderQr() {
  const box = $('#qr');
  if (!box || !window.qrcode) return;
  const inv = S.invite || {};
  const text = `pzduel://join?host=${inv.ip}&port=${inv.port}&pw=${inv.password}&code=${inv.code}`;
  if (lastQr === text) return;
  lastQr = text;
  try {
    const qr = qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    box.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 1 });
  } catch (e) {
    box.innerHTML = '<div class="hint" style="color:#333">QR indisponible</div>';
  }
}

function renderLogs() {
  const box = $('#logs');
  if (!box || !S) return;
  box.innerHTML = (S.logs || []).slice().reverse()
    .map((l) => `<div class="logline ${l.level}"><span class="lt">${l.t}</span>${esc(l.msg)}</div>`)
    .join('');
  $('#logcount').textContent = String((S.logs || []).length);
}

// ---------------------------------------------------------------- actions
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-action]');
  if (!btn) return;
  const a = btn.dataset.action;

  if (a === 'config') { view = 'config'; draft = draft || newDraft(); return render(); }
  if (a === 'back') { view = 'auto'; return render(); }
  if (a === 'logs') { $('#logsbox').classList.toggle('collapsed'); return; }
  if (a === 'copy-link') {
    const i = S.invite || {};
    return copy(`pzduel://join?host=${i.ip}&port=${i.port}&pw=${i.password}&code=${i.code}`);
  }
  if (a === 'start') {
    localStorage.setItem('pzduel.pseudo', draft.pseudo);
    view = 'auto';
    return act('start', {
      preset: draft.preset,
      duration_s: draft.duration_s,
      day_length: draft.day_length,
      pseudo: draft.pseudo,
      password: draft.password,
    });
  }
  if (a === 'rematch') { view = 'auto'; return act('rematch'); }
  if (a === 'cancel') { return act('cancel'); }
  if (a === 'simulate') { return act('simulate_join'); }
  if (a === 'open-game') { return act('open_game'); }
});

$('#logstoggle').addEventListener('click', () => $('#logsbox').classList.toggle('collapsed'));

// ---------------------------------------------------------------- boot
(async function boot() {
  try {
    const res = await api('/api/presets');
    presets = res.presets || [];
  } catch (e) {
    presets = [];
  }
  draft = newDraft();
  await poll();
  setInterval(poll, 700);
})();
