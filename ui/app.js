'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const app = $('#app');

let S = null;                 // dernier état du service
let presets = [];             // presets chargés
let view = 'auto';            // 'auto' | 'config'
let draft = null;             // brouillon de configuration
let setup = null;             // dernier contrôle de `/api/setup`, `null` tant qu'il manque
let setupPhase = null;        // phase à laquelle ce contrôle a été demandé
let folderDraft = '';         // dossier du jeu en cours de saisie
let addressDraft = '';        // adresse publique en cours de saisie
let portsLease = null;        // dernier bail annonce par `ports_opening`, pour le libelle du bouton
let portsPrefilled = null;    // `expires_at` du resultat `mapped` deja propose dans `#f-address`
let shown = null;             // dernier contenu posé dans `#app`
let quitting = false;         // la sortie est demandée : plus aucun autre écran
let stopped = false;          // le service ne répond plus après cette demande
let quitArmed = false;        // premier clic sur « Quit » pendant un duel
let pollTimer = null;
let toastTimer = null;

const TOAST_MS = 2600;
// `/api/setup` ne mesure qu'en `idle` ; hors de ces phases, il n'y a rien à relire.
const SETTLED_PHASES = ['idle', 'over', 'error'];
const LAUNCHER_FILE = 'pz-duel-join.bat';

// ---------------------------------------------------------------- réseau
async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  return res.json();
}

function unreachable() {
  $('#svc').textContent = 'service injoignable';
  $('#svc').className = 'badge badge-bad';
}

async function poll() {
  try {
    const state = await api('/api/state');
    // Un 403, un 500 ou le 503 de `turned_away` portent du JSON sans `phase` : `S` ne change pas.
    if (!state || typeof state.phase !== 'string') return unreachable();
    S = state;
    prefillAddress();
    $('#svc').textContent = 'service : ok';
    $('#svc').className = 'badge badge-ok';
  } catch (e) {
    if (quitting) {
      stopped = true;
      clearInterval(pollTimer);
      $('#svc').textContent = 'service stopped';
      $('#svc').className = 'badge badge-mock';
      return render();
    }
    return unreachable();
  }
  if (S.phase !== setupPhase && SETTLED_PHASES.includes(S.phase)) refreshSetup();
  setupPhase = S.phase;
  render();
}

// Une mesure lie les ports et relit le mod : elle suit les changements de phase et les
// actions de l'écran, pas la cadence de `poll`.
async function refreshSetup() {
  try {
    setup = await api('/api/setup');
  } catch (e) {
    setup = null;
  }
  render();
}

function answered(out) {
  if (!out.ok) toast(out.message || out.error || 'action refusée', true);
  if (out.state) { S = out.state; prefillAddress(); render(); }
  return out;
}

async function act(action, extra = {}) {
  return answered(await api('/api/action', Object.assign({ action }, extra)));
}

async function setupAction(path, body) {
  const out = answered(await api(path, body));
  if (out.ok) toast(out.message);
  await refreshSetup();
}

async function saveLauncher() {
  const res = await fetch('/api/invite/launcher');
  if (!res.ok) {
    const out = await res.json();
    return toast(out.error || 'Launcher unavailable', true);
  }
  const url = URL.createObjectURL(await res.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = LAUNCHER_FILE;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url));
}

async function quit() {
  // Le bouton reste visible en plein duel : un clic égaré n'y met pas fin.
  if (S && !SETTLED_PHASES.includes(S.phase) && !quitArmed) {
    quitArmed = true;
    setTimeout(() => { quitArmed = false; }, TOAST_MS);
    return toast('Click Quit again to stop the duel and exit', true);
  }
  let out;
  try {
    out = await api('/api/quit', {});
  } catch (e) {
    return toast('Could not reach the service', true);
  }
  if (!out.ok) return toast(out.error || 'Quit refused', true);
  quitting = true;
  $('#quit').disabled = true;
  render();
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
  toastTimer = setTimeout(() => el.classList.remove('show'), TOAST_MS);
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

function newDraft() {
  const p = presets[0] || { id: 'duel_rapide', duration_s: 1500, day_length: 1 };
  return {
    preset: p.id,
    duration_s: p.duration_s,
    day_length: p.day_length,
    pseudo: localStorage.getItem('pzduel.pseudo') || 'hôte',
    password: '',
  };
}

// Sans mesure, on ne sait rien : le bouton reste désactivé.
const canLaunch = (setup) => Boolean(setup && setup.ok);

const presetOf = (id) => presets.find((p) => p.id === id) || presets[0] || { summary: [] };

// ---------------------------------------------------------------- vues
function render() {
  if (quitting) return renderQuit();
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

// `poll` repasse ici sans cesse : un contenu inchangé garde le QR, le menu ouvert et la saisie.
function shell(inner) {
  if (inner === shown) return;
  const active = document.activeElement;
  const id = active && app.contains(active) ? active.id : '';
  const caret = id ? active.selectionStart : null;
  app.innerHTML = inner;
  shown = inner;
  const next = id && document.getElementById(id);
  if (!next) return;
  next.focus();
  if (caret !== null && caret !== undefined) next.setSelectionRange(caret, caret);
}

function serverChips() {
  const chip = (label, status) => {
    const cls = status === 'up' ? 'up' : status === 'starting' ? 'busy' : 'down';
    const txt = status === 'up' ? 'prêt' : status === 'starting' ? 'démarrage…' : 'arrêté';
    return `<span class="chip"><span class="dot ${cls}"></span>${label} · ${txt}</span>`;
  };
  return `<div class="srv">${chip('Serveur A', S.servers.A)}${chip('Serveur B', S.servers.B)}</div>`;
}

// Le défaut qu'on peut corriger d'ici passe avant ceux qui demandent seulement de réessayer.
function defaultDefect() {
  return setup.defects.find((d) => d.action !== 'retry') || setup.defects[0];
}

function setupRow(label, kind, value) {
  const bad = setup.defects.some((d) => d.kind === kind);
  return `<div class="row"><span class="k">${label}</span>
    <span class="v ${bad ? 'bad' : 'good'}">${bad ? '✕' : '✓'} ${esc(value)}</span></div>`;
}

function setupNextStep() {
  if (S.phase !== 'idle') {
    return '<p class="hint">Quit and reopen PZ Duel to fix this.</p>';
  }
  const defect = setup.ok ? null : defaultDefect();
  if (defect && defect.action === 'install_mod') {
    return '<div class="actions"><button class="primary" data-action="install-mod">Install mod</button></div>';
  }
  if (defect && defect.action === 'choose_folder') {
    return `<label class="field"><span>Project Zomboid folder</span>
        <input id="f-folder" value="${esc(folderDraft)}" placeholder="Folder that contains the game">
      </label>
      <div class="actions"><button class="primary" data-action="choose-folder">Choose folder</button></div>`;
  }
  return `<div class="actions"><button class="${defect ? 'primary' : 'ghost'}" data-action="retry">Check again</button></div>`;
}

// État de la machine : le service mesure, l'UI affiche. En simulé, il n'y a rien à montrer.
function setupCard() {
  if (!setup || !setup.measured) return '';
  const v = setup.values;
  return `<section class="card">
    <h2>Setup</h2>
    <p class="sub">${setup.ok ? 'Everything is ready.' : 'Fix this before launching a duel.'}</p>
    ${setupRow('Project Zomboid', 'pz', v.pz_dir || 'Not found')}
    ${setupRow('Mod', 'mod', v.mod_fingerprint === setup.expected_mod_fingerprint ? 'Installed' : 'Not installed')}
    ${setupRow('RAM', 'ram', `${v.mem_available_mb ?? '?'} MB free, ${v.min_free_ram_mb} MB needed`)}
    ${setupRow('Ports', 'port', `Free: ${v.free_ports.join(', ') || 'none'}`)}
    ${setup.defects.length ? `<ul class="feed">${setup.defects.map((d) => `<li>${esc(d.message)}</li>`).join('')}</ul>` : ''}
    ${setupNextStep()}
  </section>`;
}

function renderHome() {
  const cfg = S.config && S.config.preset ? S.config : null;
  shell(`
    ${setupCard()}
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
    ${canLaunch(setup) ? '' : setupCard()}
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
        <label class="field"><span>Mot de passe de l'invité</span>
          <input id="f-password" value="${esc(draft.password)}" maxlength="24" placeholder="tiré au hasard">
        </label>
      </div>

      <div class="row"><span class="k">Mods requis</span>
        <span class="v">duelcore <span style="color:var(--green)">✓</span> · duelhud <span style="color:var(--green)">✓</span>
        <span class="hint"> (lecture seule)</span></span></div>

      <h2 style="margin-top:18px">Règles — ${esc(p.label)}</h2>
      <p class="sub">Aperçu du preset. Le lobby annonce la table jouée, réduite si un atome est retiré.</p>
      <ul class="feed">
        ${(p.summary || []).map((line) => `<li>• ${esc(line)}</li>`).join('')}
      </ul>

      <div class="actions">
        <button class="ghost" data-action="back">Annuler</button>
        <button class="primary" data-action="start" ${canLaunch(setup) ? '' : 'disabled'}>Launch duel</button>
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

// La table que le service joue : ni le preset, ni un calcul de l'UI. Le hash vient de la même table.
function playedTable() {
  const table = S.capabilities && S.capabilities.table;
  if (!table) return '';
  const rules = S.capabilities.disabled_rules || [];
  return `<p class="hint">Table jouée : atomes ${esc(table.atoms.join(', '))} ; règles ${esc(table.rules.join(', ') || 'aucune')}</p>
    ${rules.length ? `<p class="hint">Règles retirées de ce match : ${esc(rules.join(', '))}</p>` : ''}
    <p class="hint">Hash des règles : ${esc(S.config.rules_hash || '')}</p>`;
}

// ---------------------------------------------------------------- ports (UPnP, R8)
// Tout vient de `ports_opening` : ports, bail et consigne manuelle sont ceux de l'API.
function portsNamed(po) {
  return (po.ports || []).map((p) => `${p.port}/${p.protocol}`).join(', ');
}

function leaseText(s) {
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}

// Heure locale du navigateur, depuis l'epoch rendue par le service.
function expiryText(expiresAt) {
  return new Date(expiresAt * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function portsReason(po) {
  if (po.status === 'invalid_config') return `invalid setting ${po.key || ''}`.trim();
  if (po.error_code !== undefined) return `router error ${po.error_code} ${po.error_text || ''}`.trim();
  return po.failure || po.reason || 'unknown reason';
}

// Une proposition par resultat `mapped`, jamais par-dessus une saisie : l'invitation ne change
// qu'au clic sur "Use this address" (R5).
function prefillAddress() {
  const po = S && S.ports_opening;
  if (!po || po.status !== 'mapped' || !po.address || po.expires_at === portsPrefilled) return;
  portsPrefilled = po.expires_at;
  if (addressDraft === '') addressDraft = po.address;
}

function portsBlock() {
  const po = S.ports_opening || {};
  if (Number.isFinite(po.lease_s) && po.lease_s > 0) portsLease = po.lease_s;
  const busy = po.status === 'in_progress';
  const label = busy ? 'Opening ports…'
    : `Open ports ${portsNamed(po)}${portsLease ? ` for ${leaseText(portsLease)}` : ''}`;
  let result = '';
  if (po.status === 'mapped') {
    result = `<p class="hint">Ports ${esc(portsNamed(po))} are open until ${esc(expiryText(po.expires_at))}.</p>
      <p class="hint">${po.address ? `Public address read from the router: ${esc(po.address)}` : esc(po.address_note || 'Public address not available.')}</p>`;
  } else if (['no_gateway', 'refused', 'invalid_config'].includes(po.status)) {
    result = `<p class="hint">Ports could not be opened automatically (${esc(portsReason(po))}).</p>
      ${po.manual ? `<p class="hint">${esc(po.manual)}</p>` : ''}`;
  }
  return `<div class="ports ${esc(po.status || '')}">
        <div class="actions"><button class="ghost" data-action="open-ports" ${busy ? 'disabled' : ''}>${esc(label)}</button></div>
        ${result}
      </div>`;
}

// Tout vient de `invite` : le service décide de ce qui existe et le dit en anglais.
function inviteCard() {
  const inv = S.invite || {};
  const real = S.mode === 'real';
  const canSave = real && inv.address && !inv.launcher_refused;
  return `
    <section class="card">
      <h2>Invitation</h2>
      <p class="sub">Send the link, the QR code or the launcher to your opponent.</p>
      ${inv.scope_text ? `<p class="${inv.needs_address ? 'err' : 'notice'}">${esc(inv.scope_text)}</p>` : ''}
      ${inv.share_text ? `<p class="err">${esc(inv.share_text)}</p>` : ''}
      ${inv.launcher_refused ? `<p class="err">${esc(inv.launcher_refused)}</p>` : ''}
      <div class="invite">
        ${inv.qr_text ? '<div class="qr" id="qr"></div>' : ''}
        <div class="invite-body">
          ${inv.link ? `<div class="line link">${esc(inv.link)}</div>` : ''}
          <div class="code">${esc(inv.code || '')}</div>
          ${inv.launcher_code ? `<p class="hint">Launcher code (their launcher asks for it):</p>
          <div class="line"><b>${esc(inv.launcher_code)}</b></div>
          <p class="hint">${esc(inv.launcher_code_text)}</p>` : ''}
          ${inv.needs_address ? '' : `<p class="hint">Manual join, for a guest on Linux:</p>
          <div class="line">${esc(inv.ip)}:${inv.port}</div>
          <div class="line">mot de passe : <b>${esc(inv.password)}</b></div>`}
          <div class="actions">
            ${inv.link ? '<button class="ghost" data-action="copy-link">Copy link</button>' : ''}
            ${canSave ? '<button class="ghost" data-action="save-launcher">Save launcher</button>' : ''}
            <button class="ghost" data-action="open-game">Ouvrir PZ</button>
            ${S.mode === 'mock' ? '<button class="ghost" data-action="simulate">Simuler l\'adversaire</button>' : ''}
          </div>
          ${real ? `<p class="hint" style="margin-top:10px">Ton accès, à garder pour toi : ${esc(S.config.host_address)}, mot de passe <b>${esc(S.config.host_password)}</b></p>` : ''}
          <p class="hint" style="margin-top:10px">Adversaire : ${S.players.B ? esc(S.players.B) : 'en attente…'}</p>
        </div>
      </div>
      ${real ? `<label class="field address"><span>Public address, for a guest outside your network</span>
        <input id="f-address" value="${esc(addressDraft)}" maxlength="253" placeholder="IP address or host name">
      </label>
      <div class="actions"><button class="ghost" data-action="set-address">Use this address</button></div>
      ${portsBlock()}` : ''}
      ${inv.rematch_note ? `<p class="hint">${esc(inv.rematch_note)}</p>` : ''}
      ${playedTable()}
    </section>`;
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
        ${playedTable()}
        <div class="actions">${phase === 'running' ? '<button class="danger" data-action="cancel">Arrêter le match</button>' : ''}</div>
      </section>`;
    if (phase === 'countdown') {
      body = `<section class="card"><div class="countdown">${S.clock.countdown ?? 3}</div>
        <p class="hint" style="text-align:center">le duel commence</p>${playedTable()}</section>`;
    }
    if (S.events.length) {
      body += `<section class="card"><h2>Fil du match</h2>
        <ul class="feed">${S.events.slice().reverse().map((e) => `<li><span class="t">${e.t}</span>${esc(e.text)}</li>`).join('')}</ul>
      </section>`;
    }
    return shell(body + `<section class="card">${serverChips()}</section>`);
  }

  if (phase === 'lobby') {
    body = inviteCard();
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
      ${playedTable()}
      <div class="actions">
        <button class="primary" data-action="rematch">Revanche</button>
        <button class="ghost" data-action="config">Nouveau duel</button>
      </div>
    </section>
    <section class="card"><h2>Fil du match</h2>
      <ul class="feed">${S.events.slice().reverse().map((e) => `<li><span class="t">${e.t}</span>${esc(e.text)}</li>`).join('')}</ul>
    </section>`);
}

// Hors d'`idle`, `/api/setup` ne remesure plus : seul un nouveau départ repasse le préflight.
function renderError() {
  shell(`<section class="card"><h2>Erreur</h2>
    <div class="err">${esc(S.error || 'erreur inconnue')}</div>
    <div class="actions">
      <button class="primary" data-action="rematch">Try again</button>
      <button class="ghost" data-action="config">Nouveau duel</button>
    </div>
  </section>`);
}

// Écran de fin : il attend que le service ne réponde plus pour dire qu'il est arrêté.
function renderQuit() {
  shell(stopped
    ? `<section class="card"><h2>PZ Duel has stopped</h2>
        <p class="sub">You can close this tab. Start PZ Duel again for a new duel.</p>
      </section>`
    : `<section class="card"><h2>Shutting down</h2>
        <p class="sub">PZ Duel is stopping the servers. This can take a moment.</p>
      </section>`);
}

// Le QR porte `invite.qr_text` tel quel : l'UI ne fabrique aucun lien.
function renderQr() {
  const box = $('#qr');
  const text = (S.invite || {}).qr_text;
  if (!box || !text || box.dataset.text === text) return;
  box.dataset.text = text;
  try {
    const qr = qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    box.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0 });
  } catch (e) {
    box.innerHTML = '<div class="hint" style="color:#333">QR code unavailable</div>';
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

  if (a === 'config') {
    view = 'config';
    draft = draft || newDraft();
    render();
    return refreshSetup();
  }
  if (a === 'back') { view = 'auto'; return render(); }
  if (a === 'logs') { $('#logsbox').classList.toggle('collapsed'); return; }
  if (a === 'quit') { return quit(); }
  if (a === 'retry') { return refreshSetup(); }
  if (a === 'install-mod') { return setupAction('/api/setup/install-mod', {}); }
  if (a === 'choose-folder') { return setupAction('/api/setup/pz-dir', { folder: folderDraft }); }
  if (a === 'copy-link') { return copy(S.invite.link); }
  if (a === 'save-launcher') { return saveLauncher(); }
  if (a === 'open-ports') { return answered(await api('/api/ports/open', {})); }
  if (a === 'set-address') { return answered(await api('/api/invite/address', { address: addressDraft })); }
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

// Délégué : `shell` remplace ces champs, et leur saisie doit survivre à `poll`.
document.addEventListener('input', (ev) => {
  if (ev.target.id === 'f-folder') folderDraft = ev.target.value;
  if (ev.target.id === 'f-address') addressDraft = ev.target.value;
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
  pollTimer = setInterval(poll, 700);
})();
