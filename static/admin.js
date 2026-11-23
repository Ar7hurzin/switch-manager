"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
const body = document.getElementById("access-body");
const requestsBody = document.getElementById("requests-body");
const message = document.getElementById("admin-message");
const input = document.getElementById("add-value");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function show(text, ok) {
  message.textContent = text;
  message.className = "admin-message " + (ok ? "ok" : "error");
}

async function call(url, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {}) } };
  const response = await fetch(url, init);
  if (response.status === 401) { window.location.assign("/login"); throw new Error("Sessão encerrada."); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) throw new Error(data.error || "Falha na operação.");
  return data;
}

function when(stamp) {
  if (!stamp) return "—";
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("pt-BR");
}

function renderRequests(requests) {
  requestsBody.innerHTML = requests.length ? requests.map(item =>
    `<tr><td>${escapeHtml(item.name || "—")}</td><td>${escapeHtml(item.email)}</td><td>${escapeHtml(when(item.requested_at))}</td>` +
    `<td><div class="admin-actions"><button type="button" class="button button-primary" data-approve="${escapeHtml(item.email)}">Aprovar</button>` +
    `<button type="button" class="button button-secondary" data-reject="${escapeHtml(item.email)}">Recusar</button></div></td></tr>`
  ).join("") : '<tr><td colspan="4" class="table-empty">Nenhum pedido pendente.</td></tr>';
}

function render(data) {
  renderRequests(data.requests || []);
  document.getElementById("google-note").hidden = !data.google;
  const rows = [
    `<tr><td>${escapeHtml(data.admin)}</td><td><span class="admin-badge admin">ADMINISTRADOR</span></td><td>Configuração</td><td></td></tr>`,
    ...data.configured.filter(email => email !== data.admin).map(email =>
      `<tr><td>${escapeHtml(email)}</td><td><span class="admin-badge">E-MAIL</span></td><td>Configuração (configure)</td><td></td></tr>`),
    ...data.entries.map(entry => {
      const domain = entry.value.startsWith("@");
      return `<tr><td>${escapeHtml(entry.value)}</td><td><span class="admin-badge">${domain ? "DOMÍNIO" : "E-MAIL"}</span></td>` +
        `<td>${escapeHtml(when(entry.added_at))}</td>` +
        `<td><button type="button" class="button button-secondary" data-remove="${escapeHtml(entry.value)}">Remover</button></td></tr>`;
    }),
  ];
  body.innerHTML = rows.join("");
}

async function load() {
  try { render(await call("/api/admin/access")); }
  catch (error) {
    const row = `<tr><td colspan="4" class="table-empty">${escapeHtml(error.message)}</td></tr>`;
    body.innerHTML = row; requestsBody.innerHTML = row;
  }
}

requestsBody.addEventListener("click", async event => {
  const approve = event.target.closest("[data-approve]")?.dataset.approve;
  const reject = event.target.closest("[data-reject]")?.dataset.reject;
  const email = approve || reject;
  if (!email) return;
  if (reject && !window.confirm(`Recusar o pedido de ${email}? A pessoa só poderá pedir de novo após 24 horas.`)) return;
  try {
    await call(approve ? "/api/admin/requests/approve" : "/api/admin/requests/reject", { method: "POST", body: JSON.stringify({ email }) });
    show(approve ? `${email} aprovado: já pode entrar.` : `Pedido de ${email} recusado.`, true);
    await load();
  } catch (error) { show(error.message, false); }
});

// New requests appear without reloading the page.
setInterval(() => { if (!document.hidden) load(); }, 30000);

document.getElementById("add-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const data = await call("/api/admin/access", { method: "POST", body: JSON.stringify({ value: input.value }) });
    input.value = "";
    show(`${data.value} liberado.`, true);
    await load();
  } catch (error) { show(error.message, false); }
});

body.addEventListener("click", async event => {
  const value = event.target.closest("[data-remove]")?.dataset.remove;
  if (!value || !window.confirm(`Remover o acesso de ${value}? Quem estiver usando o painel perde o acesso imediatamente.`)) return;
  try {
    await call("/api/admin/access/remove", { method: "POST", body: JSON.stringify({ value }) });
    show(`${value} removido.`, true);
    await load();
  } catch (error) { show(error.message, false); }
});

load();
