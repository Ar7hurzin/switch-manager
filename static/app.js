const $ = (id) => document.getElementById(id);
const baseFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
  const method = String(init.method || "GET").toUpperCase();
  if (url.origin === window.location.origin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    const headers = new Headers(init.headers || {});
    headers.set("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]')?.content || "");
    init = { ...init, headers };
  }
  return baseFetch(input, init).then(response => {
    if (url.origin === window.location.origin && url.pathname.startsWith("/api/") && response.status === 401) {
      window.location.assign("/login");
      throw new Error("Sessão encerrada. Entre novamente.");
    }
    return response;
  });
};
let appStatus = { mode: "demo", ready: false };
let configStatus = { available: false, writes_enabled: false };
let pendingConfigPreview = null;
let configPreviewRevision = 0;
let configPreviewBusy = false;
let configApplying = false;
let cliToken = sessionStorage.getItem("qm_cli_token") || "";
let cliConnected = false;
let cliOccupiedElsewhere = false;
let cliSensitivePrompt = false;
let cliClosing = false;
let cliOpening = false;
let cliSending = false;
let cliStateRevision = 0;
let cliStatusRevision = 0;
let cliStateUncertain = false;
let cliStatusTimer = null;
let physicalRefreshBusy = false;
let physicalTimer = null;

function setMode(status) {
  appStatus = status;
  document.documentElement.dataset.mode = status.mode;
  const isVirtual = status.mode === "virtual";
  const isSerial = status.mode === "serial";
  const isLive = status.mode === "real" || isSerial || isVirtual;
  if ($("live-status")) $("live-status").style.display = isSerial ? "" : "none";
  const pill = $("mode-pill");
  const banner = $("mode-banner");
  pill.textContent = isVirtual ? "SIMULAÇÃO · S3900 VIRTUAL" : status.mode === "real" ? "MODO REAL · SSH" : isSerial ? "MODO REAL · CONSOLE" : "DEMONSTRAÇÃO";
  pill.classList.toggle("real", isLive);
  banner.classList.toggle("real", isLive);
  banner.innerHTML = `<span class="banner-icon">${isLive ? "✓" : "!"}</span><span>${escapeHtml(status.message)}</span>`;
  $("ssh-message").textContent = isLive
    ? (status.ready ? (isSerial ? "Console serial configurado. A coleta de leitura ocorre automaticamente, sem clicar em Atualizar." : "Conector SSH configurado. A interface envia somente os comandos fixos de consulta.") : status.message)
    : "Modo de demonstração: nenhuma sessão é aberta e nenhum dado vem do switch.";
  $("transport-badge").textContent = isVirtual ? "VIRTUAL" : isSerial ? "CONSOLE" : "SSH";
  $("refresh").disabled = isLive && !status.ready;
  $("refresh").title = isLive && !status.ready ? status.message : "Executar leitura segura";
  if (!isVirtual && !isSerial) fillPortSelect([]);
  if (status.mode === "demo") {
    $("device-name").textContent = "S3900 · demonstração";
    $("device-model").textContent = "Exemplo de interface · aparelho não identificado";
    $("ports-count").textContent = "18 / 24";
    $("vlan-count").textContent = "4";
    renderDemo();
  } else if (isVirtual) {
    $("ssh-message").textContent = "Console virtual isolado; não acessa hardware.";
    $("ports-note").textContent = "Interfaces simuladas. UP indica link virtual e porta habilitada.";
    $("system-page").querySelector(".subtle-tag").textContent = "Simulação";
    $("system-page").querySelector(".system-note").textContent = "Perfil simulado; não executa firmware Huawei original.";
    // Only texts change: the cards keep the elements applyDevice fills in.
    $("system-model-overline").textContent = "PERFIL VIRTUAL";
    $("system-hardware-title").textContent = "Modelo lógico";
    $("system-hardware").textContent = "Sem emulação de CPU, memória ou ASIC";
    $("system-cpld").textContent = "—";
    $("system-bootrom").textContent = "—";
    $("system-console-overline").textContent = "CONSOLE VIRTUAL";
    $("system-console").textContent = "Simulador";
    $("system-console-config").textContent = "QM_VIRTUAL_MODEL";
    $("system-read-state").textContent = "Aguardando leitura";
    $("system-transport").textContent = "Nenhuma porta COM ou SSH física utilizada";
    $("system-access").textContent = "Simulação sem conexão física";
    $("switch-connections-note").textContent = "Links virtuais não representam dispositivos físicos.";
    $("cli-connection-note").textContent = "Console virtual: comandos alteram somente o simulador.";
    document.querySelector(".cli-warning").textContent = "Terminal virtual: comandos alteram apenas a simulação. Envie um comando por vez.";
    loadVirtualState();
    loadDevice();
    window.setInterval(loadVirtualState, 3000);
  } else if (isLive) {
    $("device-name").textContent = "Switch não consultado";
    $("device-model").textContent = "O modelo e o firmware só aparecem após uma leitura real";
    $("ports-count").textContent = "—";
    $("vlan-count").textContent = "—";
    $("last-read").textContent = "—";
    $("port-grid").innerHTML = '<div class="empty-inline">O mapa ficará vazio até validar o parser deste firmware.</div>';
    $("vlan-body").innerHTML = '<tr><td colspan="4" class="table-empty">Sem leitura real. Dados de demonstração desativados.</td></tr>';
    $("outputs").innerHTML = '<div class="empty-inline">Nenhuma consulta executada nesta sessão.</div>';
    $("connection-state").classList.remove("online");
    $("connection-state").querySelector("span").textContent = "Sem conexão";
    if (isSerial) {
      $("refresh").style.display = "none";
      loadDevice();
      beginPhysicalPolling();
    }
  }
  loadConfigurationStatus();
  if (pageKind === "cli" || cliToken) loadCliStatus();
}

function renderDemo() {
  const ports = Array.from({ length: 24 }, (_, i) => {
    const num = i + 1;
    const down = [3, 6, 10, 13, 17, 20].includes(num);
    return `<div class="port ${down ? "down" : ""}" title="Porta ${num} · ${down ? "exemplo sem link" : "exemplo ativa"}"><span class="port-num">${String(num).padStart(2, "0")}</span><span class="port-state">${down ? "DOWN" : "UP"}</span></div>`;
  }).join("");
  $("port-grid").innerHTML = ports;
  const rows = [
    ["1", "default", "23–24", "Ilustrativo"],
    ["10", "ADMIN", "23 (tagged), 1–4", "Ilustrativo"],
    ["20", "USUARIOS", "23 (tagged), 5–16", "Ilustrativo"],
    ["99", "GESTAO", "24 (tagged), 17–18", "Ilustrativo"],
  ];
  $("vlan-body").innerHTML = rows.map(row => `<tr>${row.map(v => `<td>${escapeHtml(v)}</td>`).join("")}</tr>`).join("");
  $("last-read").textContent = "Demonstração";
}

function renderPhysical(data, stale = false) {
  if (!data) return;
  const text = (id, value) => { $(id).textContent = value ?? "—"; };
  const ports = [...(data.ports || [])].sort((a,b) => a.number - b.number);
  const fieldOld = (port, field) => stale || Boolean(port.field_stale?.[field] ?? port.stale);
  const observedAt = (port, field) => port.field_observed_at?.[field] || (field === "state" ? port.link_at : null);
  const timeLabel = value => value ? new Date(value).toLocaleTimeString("pt-BR") : "não coletado";
  const fieldLabel = (port, field, suffix = "") => port[field] == null ? "—" : String(port[field]) + suffix + (fieldOld(port, field) ? " (anterior)" : "");
  const known = ports.filter(p => (p.state === "up" || p.state === "down") && !fieldOld(p, "state"));
  text("device-name", data.name);
  text("device-model", data.version);
  text("ports-count", known.length === ports.length ? known.filter(p => p.state === "up").length + " / " + ports.length : known.filter(p => p.state === "up").length + " / " + (data.expected_ports || 28) + " *");
  text("ports-count-note", known.length === ports.length ? "Todos os links com leitura recente" : known.length + " de " + (data.expected_ports || 28) + " links com leitura recente");
  text("vlan-count", data.vlans === null ? "—" : data.vlans.length);
  text("vlan-count-note", data.vlans === null ? "Lista não reconhecida" : (stale || data.inventory_stale ? "Inventário anterior" : "VLANs lidas do switch"));
  text("last-read", new Date(data.collected_at).toLocaleTimeString("pt-BR"));
  text("last-read-note", stale ? "Última leitura · desatualizada" : "Coleta automática em lotes");
  $("connection-state").classList.toggle("online", !stale);
  $("connection-state").querySelector("span").textContent = stale ? "Dados anteriores" : "Console consultado";
  text("physical-collection-note", (stale ? "Última leitura disponível. " : "Leitura real. ") + "Estados e detalhes têm horários próprios; dados não reconhecidos aparecem como —. " + (data.notes || []).join(" "));
  $("ports-note").textContent = "Portas físicas: " + known.length + " de " + ports.length + " links recentes. ANT. indica um estado anterior; selecione uma porta para conferir os horários de cada campo.";
  $("port-grid").innerHTML = ports.map(p => '<button type="button" class="port ' + (fieldOld(p, "state") && p.state ? "stale" : p.state === "down" ? "down" : p.state === "up" ? "" : "unknown") + '" data-real-port="' + p.number + '" title="' + escapeHtml(p.interface + " · Link: " + fieldLabel(p, "state") + " · " + timeLabel(observedAt(p, "state")) + " · " + fieldLabel(p, "mode") + " · PVID " + fieldLabel(p, "pvid")) + '"><span class="port-num">' + String(p.number).padStart(2,"0") + '</span><span class="port-state">' + (p.state && fieldOld(p, "state") ? "ANT." : (p.state || "—").toUpperCase()) + '</span></button>').join("");
  const rows = data.vlans?.slice().sort((a,b) => a.id - b.id) ?? null;
  const vlanPortNumbers = vlan => {
    const confirmed = [...(vlan.effective_tagged_numbers ?? vlan.tagged_numbers ?? []), ...(vlan.effective_untagged_numbers ?? vlan.untagged_numbers ?? [])];
    const numbers = [...new Set(confirmed.map(Number).filter(n => Number.isInteger(n) && n >= 1))].sort((a,b) => a-b);
    return numbers.length ? numbers.join(", ") : "—";
  };
  $("vlan-body").innerHTML = rows === null ? '<tr><td colspan="4" class="table-empty">Lista não reconhecida; consulte Diagnóstico.</td></tr>' : rows.map(v => "<tr>" + [v.id,v.name ?? "—",vlanPortNumbers(v),(stale || v.stale || v.membership_stale ? "Leitura anterior · " : "Console · ") + timeLabel(v.collected_at) + (v.membership_partial ? " · membros parciais" : "")].map(x=>"<td>"+escapeHtml(x)+"</td>").join("") + "</tr>").join("");
  const selected = Number($("physical-port-detail").dataset.port || 1);
  function detail(number) {
    const p = ports.find(p => p.number === number);
    if (!p) return;
    $("physical-port-detail").dataset.port = String(number);
    const fields = [["state", "Link"], ["pvid", "PVID"], ["mode", "Modo"], ["description", "Descrição"], ["tagged", "Tagged"], ["untagged", "Untagged"], ["rx_bps", "Entrada"], ["tx_bps", "Saída"]];
    $("physical-port-detail").textContent = p.interface + "\n" + fields.map(([field, label]) => label + ": " + fieldLabel(p, field, field.endsWith("_bps") ? " bit/s" : "") + " · " + timeLabel(observedAt(p, field))).join("\n");
  }
  $("port-grid").querySelectorAll("[data-real-port]").forEach(button => button.addEventListener("click", () => detail(Number(button.dataset.realPort))));
  detail(selected);
  const macs = data.mac_table;
  const macList = $("physical-mac-list");
  $("switch-device-placeholder").hidden = Boolean(macs);
  $("switch-device-count").textContent = macs ? String(macs.count) : "N/D";
  $("switch-connections-note").textContent = macs
    ? (stale || macs.stale ? "Tabela MAC anterior · " : "MACs aprendidos identificados · ") + new Date(macs.collected_at).toLocaleTimeString("pt-BR") + ". Não é uma contagem de pessoas nem prova de link ativo."
    : "Tabela MAC ainda indisponível ou não reconhecida neste firmware.";
  macList.innerHTML = macs ? (macs.entries.length ? '<div class="table-wrap"><table><thead><tr><th>MAC</th><th>VLAN</th><th>Porta</th></tr></thead><tbody>' + [...macs.entries].sort((a,b) => a.port-b.port || a.vlan-b.vlan || a.mac.localeCompare(b.mac)).slice(0,100).map(r => "<tr>" + [r.mac,r.vlan,r.port].map(v=>"<td>"+escapeHtml(v)+"</td>").join("") + "</tr>").join("") + '</tbody></table></div>' : '<div class="empty-inline">Nenhum MAC aprendido na leitura.</div>') : "";
  if (macs && macs.entries.length > 100) macList.insertAdjacentHTML("beforeend", '<p class="panel-note">Exibindo as primeiras 100 entradas reconhecidas.</p>');
  const traffic = ports.filter(p => p.rx_bps != null || p.tx_bps != null);
  $("traffic-placeholder").hidden = traffic.length > 0;
  $("physical-traffic").innerHTML = traffic.length ? '<p class="panel-note">Amostras do próprio switch; cada porta tem seu horário de coleta.</p><div class="table-wrap"><table><thead><tr><th>Porta</th><th>Entrada (bit/s)</th><th>Saída (bit/s)</th><th>Coleta</th></tr></thead><tbody>' + traffic.map(p=>"<tr>" + [p.number,fieldLabel(p, "rx_bps"),fieldLabel(p, "tx_bps"),"Entrada: " + timeLabel(observedAt(p, "rx_bps")) + " · Saída: " + timeLabel(observedAt(p, "tx_bps"))].map(v=>"<td>"+escapeHtml(v)+"</td>").join("") + "</tr>").join("") + '</tbody></table></div>' : "";
  $("system-model").textContent = data.model;
  $("system-version").textContent = data.version;
  $("system-hardware").textContent = data.hardware || "—";
  $("system-cpld").textContent = data.cpld || "—";
  $("system-bootrom").textContent = data.bootrom || "—";
  $("system-console").textContent = data.port + " · " + data.baud_rate + " baud";
  text("system-read-state", stale ? "Leitura anterior" : "Consultado às " + new Date(data.collected_at).toLocaleTimeString("pt-BR"));
  $("system-page").querySelector(".system-note").textContent = "Identificação e dados lidos pelo console. Consulte o mapa para informações de cada porta.";
  $("system-page").querySelector(".subtle-tag").textContent = stale ? "Leitura anterior" : "Leitura real";
}

let physicalCacheBusy = false;
let lastPhysicalRenderKey = "";
let lastPhysicalOutputKey = "";
let lastPhysicalSnapshot = null;
let currentDevice = null;
let deviceBreadcrumb = "HUAWEI";

function fillPortSelect(names) {
  const select = $("config-port");
  const previous = select.value;
  const list = names.length ? names : Array.from({ length: 28 }, () => "");
  // "Porta 3 · Ethernet1/0/3" when the interface name is known, otherwise just "Porta 3".
  select.innerHTML = list.map((name, i) => `<option value="${i + 1}">Porta ${i + 1}${name ? " · " + escapeHtml(name) : ""}</option>`).join("");
  if (previous && Number(previous) <= list.length) select.value = previous;
}

// Adapts the whole panel to the switch identified on the console.
function applyDevice(device, loginRequired = false, storedCredentials = false) {
  const banner = $("device-banner");
  $("console-login").hidden = !loginRequired;
  // Stored credentials are never re-sent on their own: a new login prompt could
  // come from another device on the cable, so the operator confirms first.
  $("console-confirm").hidden = !(loginRequired && storedCredentials);
  if (!device) {
    banner.hidden = !loginRequired;
    banner.className = "mode-banner";
    banner.innerHTML = '<span class="banner-icon">!</span><span>O console do switch pede login. Informe usuário e senha abaixo.</span>';
    if (!currentDevice) fillPortSelect([]);
    return;
  }
  const changed = currentDevice && currentDevice.fingerprint !== device.fingerprint;
  const first = !currentDevice;
  currentDevice = device;
  deviceBreadcrumb = escapeHtml(device.model).toUpperCase();
  if (pageKind === "dashboard") $("page-breadcrumb").innerHTML = "SWITCHES <span>/</span> " + deviceBreadcrumb;
  $("header-device").textContent = device.model;
  const ports = device.ports || [];
  $("config-scope-tag").textContent = device.model + " · " + ports.length + " portas";
  $("config-ports-title").textContent = ports.length ? `Portas 1–${ports.length}` : "Portas";
  $("config-ports-tag").textContent = device.port_summary || "Inventário indisponível";
  $("system-ports-title").textContent = ports.length + " interfaces";
  $("system-ports-summary").textContent = device.port_summary || "—";
  $("system-profile").textContent = device.profile_label;
  $("system-model").textContent = device.model;
  $("system-vendor").textContent = device.vendor;
  $("system-version").textContent = device.version;
  $("system-validation").textContent = device.profile_label;
  $("system-writes").textContent = !device.guided_config ? "Indisponíveis · use a CLI"
    : device.validated_writes ? "Validadas neste modelo" : "Adaptadas · modelo ainda não validado";
  if (appStatus.mode === "virtual") $("ports-note").textContent = ports.length + " interfaces simuladas. UP indica link virtual e porta habilitada.";
  fillPortSelect(ports);
  const text = !device.guided_config
    ? `Equipamento detectado: ${device.vendor} ${device.model} · ${device.note}`
    : device.validated_writes
    ? `Switch detectado: ${device.model} · ${device.version} · perfil validado (monitoramento e configuração guiada).`
    : `Switch detectado: ${device.vendor} ${device.model} · ${device.version} · CONFIGURAÇÃO ADAPTADA: modelo ainda não validado. Confira cada prévia; operações que o switch recusar passam para a CLI.`;
  banner.hidden = false;
  banner.className = "mode-banner";
  banner.classList.toggle("real", device.guided_config && device.validated_writes);
  banner.classList.toggle("error", !device.guided_config);
  banner.innerHTML = `<span class="banner-icon">${device.guided_config && device.validated_writes ? "✓" : "!"}</span><span>${escapeHtml((changed ? "NOVO SWITCH: " : "") + text)}</span>`;
  // A different switch: writes availability and the port catalog changed.
  if (changed || first) loadConfigurationStatus();
}

async function loadDevice() {
  try {
    const response = await fetch("/api/device", { cache: "no-store" });
    const result = await response.json();
    applyDevice(result.device, result.login_required, result.console_credentials);
  } catch { /* the next dashboard poll retries */ }
}

async function loadPhysicalCache() {
  if (appStatus.mode !== "serial" || physicalCacheBusy) return;
  physicalCacheBusy = true;
  try {
    const response = await fetch("/api/dashboard", {
      cache: "no-store", signal: AbortSignal.timeout(5000)
    });
    if (!response.ok) throw new Error("API de monitoramento indisponível (" + response.status + ")");
    const result = await response.json();
    const monitor = result.monitor || {state: "iniciando", message: "Aguardando o monitoramento em segundo plano."};
    const state = monitor.state;
    const stale = result.stale || result.cli_busy || ["erro", "desatualizado", "pausado"].includes(state);
    const status = $("live-status");
    if (status) {
      const labels = {
        ativo: "● Monitoramento ativo", consultando: "◌ Lendo o switch",
        pausado: "Ⅱ CLI aberta · pausado", erro: "! Sem leitura recente",
        desatualizado: "! Dados anteriores", aguardando: "◌ Aguardando console",
        iniciando: "◌ Iniciando monitoramento"
      };
      status.textContent = labels[state] || "◌ Aguardando console";
      status.title = monitor.message || "";
    }
    applyDevice(result.device, result.login_required, result.console_credentials);
    if (result.snapshot) {
      lastPhysicalSnapshot = result.snapshot;
      const key = JSON.stringify(result.snapshot) + "|" + stale;
      if (key !== lastPhysicalRenderKey) {
        renderPhysical(result.snapshot, stale);
        lastPhysicalRenderKey = key;
      }
    }
    if (result.outputs?.length) {
      const key = JSON.stringify(result.outputs);
      if (key !== lastPhysicalOutputKey) {
        showOutputs(result.outputs);
        lastPhysicalOutputKey = key;
      }
    }
    const last = result.snapshot?.collected_at
      ? " Última leitura: " + new Date(result.snapshot.collected_at).toLocaleTimeString("pt-BR") + "."
      : "";
    const notes = (result.snapshot?.notes || []).slice(0, 2).join(" ");
    $("physical-collection-note").textContent =
      (monitor.message || "Monitoramento aguardando.") + last +
      " Console de 9600 baud: medições não são instantâneas; taxas podem representar médias de 300 segundos. " + notes;
    if (result.cli_busy) {
      $("physical-collection-note").textContent = "Monitoramento pausado: encerre a sessão CLI para liberar a porta serial." + last;
    }
  } catch (error) {
    if (lastPhysicalSnapshot) renderPhysical(lastPhysicalSnapshot, true);
    lastPhysicalRenderKey = "";
    $("connection-state").classList.remove("online");
    $("connection-state").querySelector("span").textContent = "Sem comunicação · dados anteriores";
    $("last-read-note").textContent = "Última leitura · sem comunicação";
    $("physical-collection-note").textContent = "Monitoramento sem acesso ao servidor: " + error.message + ". Últimos dados preservados.";
    const status = $("live-status");
    if (status) status.textContent = "! Conexão com servidor indisponível";
  } finally {
    physicalCacheBusy = false;
  }
}

function beginPhysicalPolling() {
  if (physicalTimer) window.clearTimeout(physicalTimer);
  const tick = async () => {
    if (appStatus.mode !== "serial") return;
    await loadPhysicalCache();
    physicalTimer = window.setTimeout(tick, document.hidden ? 8000 : 2500);
  };
  tick();
}

// Remains for existing call sites after guided configuration.
// It reads the cache only; serial collection belongs to the background worker.
async function refreshPhysical() {
  return loadPhysicalCache();
}

function renderVirtual(data) {
  $("device-name").textContent = data.name;
  $("device-model").textContent = data.model + " · " + data.version;
  $("ports-count").textContent = data.ports.filter(p => p.state === "up").length + " / " + data.ports.length;
  $("vlan-count").textContent = data.vlans.length;
  $("last-read").textContent = new Date().toLocaleTimeString("pt-BR");
  $("connection-state").classList.add("online");
  $("connection-state").querySelector("span").textContent = "Virtual ativo";
  $("port-grid").innerHTML = data.ports.map(p => '<div class="port ' + (p.state === "down" ? "down" : "") + '" title="' + escapeHtml(p.interface + " · " + p.mode + " · PVID " + p.pvid + " · " + (p.admin ? "Habilitada" : "Desabilitada") + " · " + p.description) + '"><span class="port-num">' + String(p.number).padStart(2,"0") + '</span><span class="port-state">' + (p.admin ? p.state.toUpperCase() : "OFF") + '</span></div>').join("");
  $("vlan-body").innerHTML = data.vlans.map(v => "<tr>" + [v.id,v.name,v.members,v.note].map(x=>"<td>"+escapeHtml(x)+"</td>").join("") + "</tr>").join("");
  $("virtual-details").hidden = false;
  $("virtual-save-state").textContent = data.dirty ? "Alterações ainda não salvas no startup virtual" : "Configuração virtual salva";
  $("virtual-ips").textContent = Object.entries(data.ips).map(([v,ip]) => "VLAN " + v + ": " + ip.ip + " / " + ip.mask).join("\n") || "Nenhum IP configurado";
  $("virtual-routes").textContent = data.routes.map(r => r.join(" ")).join("\n") || "Nenhuma rota estática";
}

async function loadVirtualState() {
  if (appStatus.mode !== "virtual") return;
  try {
    const response = await fetch("/api/virtual/state", {cache:"no-store"});
    if (!response.ok) throw new Error("Estado virtual indisponível");
    renderVirtual(await response.json());
  } catch {
    $("connection-state").classList.remove("online");
    $("connection-state").querySelector("span").textContent = "Virtual indisponível";
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Serviço indisponível");
    setMode(await response.json());
  } catch {
    $("mode-pill").textContent = "INDISPONÍVEL";
    $("mode-banner").textContent = "Não foi possível consultar o serviço local. Atualize a página.";
    $("refresh").disabled = true;
  }
}

function showOutputs(items) {
  if (!items?.length) {
    $("outputs").innerHTML = '<div class="empty-inline">Sem saída de diagnóstico. A conexão não enviou nenhum comando não autorizado.</div>';
    return;
  }
  $("outputs").innerHTML = items.map(item => `<section class="output-block"><div class="output-title">${escapeHtml(item.command)}</div><pre>${escapeHtml(item.output || "(sem saída)")}</pre></section>`).join("");
}

const pageKind = document.documentElement.dataset.page || "dashboard";

async function loadConnections() {
  try {
    const response = await fetch("/api/connections", { cache: "no-store" });
    if (!response.ok) throw new Error("A sessão expirou ou a lista não está disponível.");
    const data = await response.json();
    const manager = data.manager || {};
    const count = Number(manager.active_sessions || 0);
    $("active-session-count").textContent = `${count} ${count === 1 ? "sessão" : "sessões"}`;
    const users = manager.users || [];
    $("manager-session-list").innerHTML = users.length
      ? users.map(user => {
          const lastActivity = new Date(user.last_activity).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
          return `<div class="manager-session-row"><span class="session-indicator"></span><strong>${escapeHtml(user.username)}</strong><span>${user.sessions} ${user.sessions === 1 ? "sessão" : "sessões"}</span><small>Atividade às ${escapeHtml(lastActivity)}</small></div>`;
        }).join("")
      : '<div class="empty-inline">Nenhuma sessão foi registrada nos últimos 5 minutos.</div>';
    if (appStatus.mode !== "serial") {
      $("switch-device-count").textContent = "N/D";
      $("switch-connections-note").textContent = appStatus.mode === "virtual" ? "Links virtuais não representam dispositivos físicos." : data.switch?.message || "A coleta de dispositivos ainda não está disponível.";
    }
  } catch (error) {
    $("manager-session-list").innerHTML = `<div class="empty-inline">${escapeHtml(error.message)}</div>`;
    $("active-session-count").textContent = "—";
    if (appStatus.mode !== "serial") $("switch-device-count").textContent = "N/D";
  }
}

function syncNavigation() {
  document.querySelectorAll(".main-nav .nav-item").forEach(link => {
    const targetPage = link.dataset.pageLink;
    link.classList.toggle("active", targetPage === pageKind);
  });
  const page = pageKind === "configuration"
    ? ["Configuração", "GERENCIAMENTO <span>/</span> CONFIGURAÇÃO"]
    : pageKind === "system"
      ? ["Sistema", "EQUIPAMENTO <span>/</span> INFORMAÇÕES DO SISTEMA"]
    : pageKind === "cli"
      ? ["CLI do Quidway", "GERENCIAMENTO <span>/</span> TERMINAL INTERATIVO"]
      : pageKind === "monitoring"
      ? ["Monitoramento", "SWITCHING <span>/</span> VISÃO GERAL"]
      : ["Dashboard", "SWITCHES <span>/</span> " + deviceBreadcrumb];
  $("page-title").textContent = page[0];
  $("page-breadcrumb").innerHTML = page[1];
}

syncNavigation();

$("refresh").addEventListener("click", async () => {
  if (appStatus.mode === "serial") return refreshPhysical(false);
  const button = $("refresh");
  button.disabled = true;
  button.innerHTML = "<span>◌</span> Consultando...";
  try {
    const response = await fetch("/api/refresh", { method: "POST", headers: { "Accept": "application/json" } });
    const result = await response.json();
    $("ssh-message").textContent = result.message || result.error || "Não foi possível consultar o switch.";
    $("last-read").textContent = result.ok ? new Date(result.collected_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "Falha";
    const systemReadState = $("system-read-state");
    if (systemReadState) systemReadState.textContent = result.ok ? `Consultado às ${new Date(result.collected_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}` : "Falha na consulta";
    if (result.mode === "virtual") {
      if (result.snapshot) renderVirtual(result.snapshot);
      if (result.outputs?.length) showOutputs(result.outputs);
    } else if (result.mode === "real" || result.mode === "serial") {
      $("connection-state").classList.toggle("online", result.ok);
      $("connection-state").querySelector("span").textContent = result.ok ? (result.mode === "serial" ? "Console consultado" : "SSH consultado") : "Sem conexão";
      showOutputs(result.outputs);
    } else {
      $("mode-banner").innerHTML = `<span class="banner-icon">!</span><span>${escapeHtml(result.message || result.error || "Não foi possível concluir a consulta.")}</span>`;
    }
  } catch {
    $("ssh-message").textContent = "Não foi possível falar com o serviço local.";
  } finally {
    button.innerHTML = "<span>↻</span> Atualizar leitura";
    button.disabled = (appStatus.mode === "real" || appStatus.mode === "serial") && !appStatus.ready;
  }
});

loadStatus();
if (pageKind === "dashboard") {
  loadConnections();
  window.setInterval(loadConnections, 30_000);
}

function parseVlanCsv(value, label) {
  const items = String(value || "").split(/[ ,]+/).filter(Boolean).map(Number);
  if (!items.length || items.some(value => !Number.isInteger(value) || value < 1 || value > 4094)) {
    throw new Error(`Informe ${label} como IDs inteiros de 1 a 4094, separados por vírgula.`);
  }
  return [...new Set(items)];
}

async function loadConfigurationStatus() {
  try {
    const response = await fetch("/api/config/status", { cache: "no-store" });
    configStatus = await response.json();
  } catch {
    configStatus = { available: false, writes_enabled: false, message: "Não foi possível consultar o modo de configuração." };
  }
  const state = $("config-state");
  const banner = $("config-banner");
  state.textContent = configStatus.writes_enabled ? "Alterações habilitadas" : "Prévia · gravação bloqueada";
  banner.textContent = configStatus.message;
  banner.classList.toggle("ready", configStatus.writes_enabled);
  banner.classList.toggle("locked", !configStatus.writes_enabled);
  markCliOnlyActions(configStatus.unsupported_actions || {});
  updateConfigurationControls();
}

// Operations this switch model rejected are done through the CLI instead.
function markCliOnlyActions(unsupported) {
  document.querySelectorAll('select[name="action"] option').forEach(option => {
    if (!option.dataset.label) option.dataset.label = option.textContent;
    const rejected = Object.prototype.hasOwnProperty.call(unsupported, option.value);
    option.disabled = rejected;
    option.textContent = option.dataset.label + (rejected ? " (use a CLI)" : "");
    option.title = rejected ? "O switch recusou: " + unsupported[option.value] : "";
  });
  document.querySelectorAll('select[name="action"]').forEach(select => {
    if (select.selectedOptions[0]?.disabled) {
      const usable = [...select.options].find(option => !option.disabled);
      if (usable) select.value = usable.value;
    }
  });
  const save = $("save-config-button");
  if (save) save.dataset.cliOnly = Object.prototype.hasOwnProperty.call(unsupported, "save") ? "1" : "";
}

function updateConfigurationControls() {
  $("backup-button").disabled = configApplying || !configStatus.available;
  $("save-config-button").disabled = configApplying || !configStatus.available || $("save-config-button").dataset.cliOnly === "1";
  for (const id of ["vlan-form", "port-form", "system-form", "route-form"]) {
    const form = $(id);
    form.querySelector('button[type="submit"]').disabled = configApplying || !configStatus.available;
    form.querySelectorAll("input, select").forEach(field => { field.disabled = configApplying; });
  }
  updateApplyAvailability();
}

function setFieldsVisible(selector, visible) {
  document.querySelectorAll(selector).forEach(element => { element.hidden = !visible; });
}

function updateConfigurationFields() {
  const vlanAction = $("vlan-action").value;
  setFieldsVisible(".vlan-name-field", vlanAction !== "delete_vlan");
  const portAction = $("port-action").value;
  setFieldsVisible(".port-vlan-field", portAction === "set_trunk_port");
  setFieldsVisible(".port-access-field", portAction === "set_access_port");
  setFieldsVisible(".port-pvid-field", portAction === "set_trunk_port" || portAction === "set_hybrid_port");
  setFieldsVisible(".port-hybrid-field", portAction === "set_hybrid_port");
  setFieldsVisible(".port-description-field", portAction === "set_port_description");
  setFieldsVisible(".port-state-field", portAction === "set_port_state");
  const systemAction = $("system-action").value;
  setFieldsVisible(".system-name-field", systemAction === "set_sysname");
  setFieldsVisible(".management-field", systemAction === "set_management_ip");
}

$("vlan-action").addEventListener("change", updateConfigurationFields);
$("port-action").addEventListener("change", updateConfigurationFields);
$("system-action").addEventListener("change", updateConfigurationFields);
updateConfigurationFields();

function showConfigFeedback(message, error = false, source = null) {
  const box = $("config-feedback");
  if (source) source.closest("article")?.append(box);
  box.hidden = false;
  box.classList.toggle("error", error);
  box.textContent = message;
}

async function previewConfiguration(payload, source = null) {
  if (configApplying) return;
  const revision = ++configPreviewRevision;
  configPreviewBusy = true;
  pendingConfigPreview = null;
  showConfigFeedback("Gerando prévia…", false, source);
  updateApplyAvailability();
  $("config-result").textContent = "";
  $("config-preview").hidden = true;
  try {
    const response = await fetch("/api/config/preview", {
      method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(payload), cache: "no-store",
    });
    const result = await response.json();
    if (revision !== configPreviewRevision || configApplying) return;
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível criar a prévia.");
    pendingConfigPreview = result;
    $("preview-summary").textContent = result.summary;
    $("preview-warnings").replaceChildren(...result.warnings.map(warning => {
      const item = document.createElement("li"); item.textContent = warning; return item;
    }));
    $("preview-commands").textContent = result.commands.join("\n");
    $("config-result").textContent = "";
    $("config-preview").hidden = false;
    showConfigFeedback("Prévia pronta. Confira os comandos e clique em Aplicar no switch.", false, source);
    $("config-preview").scrollIntoView({ behavior: "smooth", block: "start" });
    updateApplyAvailability();
  } catch (error) {
    if (revision !== configPreviewRevision || configApplying) return;
    $("config-result").classList.add("error");
    $("config-result").textContent = error.message;
    showConfigFeedback(error.message, true, source);
  } finally {
    if (revision === configPreviewRevision) {
      configPreviewBusy = false;
      updateApplyAvailability();
    }
  }
}

$("vlan-form").addEventListener("submit", event => {
  event.preventDefault();
  if (configApplying) return;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  data.vlan_id = Number(data.vlan_id);
  previewConfiguration(data, event.currentTarget);
});

$("port-form").addEventListener("submit", event => {
  event.preventDefault();
  if (configApplying) return;
  try {
    const data = Object.fromEntries(new FormData(event.currentTarget));
    data.port = Number(data.port);
    if (data.action === "set_access_port") data.vlan_id = Number(data.vlan_id);
    if (data.action === "set_trunk_port") {
      data.vlans = parseVlanCsv(data.vlans, "as VLANs permitidas");
      data.pvid = Number(data.pvid);
    }
    if (data.action === "set_hybrid_port") {
      data.tagged_vlans = data.tagged_vlans ? parseVlanCsv(data.tagged_vlans, "as VLANs tagged") : [];
      data.untagged_vlans = data.untagged_vlans ? parseVlanCsv(data.untagged_vlans, "as VLANs untagged") : [];
      data.pvid = Number(data.pvid);
    }
    previewConfiguration(data, event.currentTarget);
  } catch (error) {
    $("config-result").classList.add("error");
    $("config-result").textContent = error.message;
    $("config-preview").hidden = true;
    ++configPreviewRevision;
    configPreviewBusy = false;
    pendingConfigPreview = null;
    updateApplyAvailability();
    showConfigFeedback(error.message, true, event.currentTarget);
  }
});

$("system-form").addEventListener("submit", event => {
  event.preventDefault();
  if (configApplying) return;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (data.action === "set_management_ip") {
    data.vlan_id = Number(data.vlan_id);
    data.prefix = Number(data.prefix);
  }
  previewConfiguration(data, event.currentTarget);
});

$("route-form").addEventListener("submit", event => {
  event.preventDefault();
  if (configApplying) return;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  data.prefix = Number(data.prefix);
  previewConfiguration(data, event.currentTarget);
});

$("save-config-button").addEventListener("click", event => previewConfiguration({ action: "save" }, event.currentTarget));

$("backup-button").addEventListener("click", async () => {
  if (configApplying) return;
  const resultBox = $("config-result");
  resultBox.classList.remove("error");
  resultBox.textContent = "";
  try {
    const response = await fetch("/api/config/backup", {
      method: "POST", headers: { "Accept": "application/json" }, cache: "no-store",
    });
    if (!response.ok) {
      const error = await response.json(); throw new Error(error.error || "Falha ao exportar backup.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)"?/)?.[1] || "quidway-config.txt";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    resultBox.textContent = "Backup exportado. Guarde o arquivo em local privado; ele pode conter credenciais ou hashes.";
  } catch (error) {
    resultBox.classList.add("error");
    resultBox.textContent = error.message;
  }
});

function updateApplyAvailability() {
  if (!$("apply-config-button")) return;
  const button = $("apply-config-button");
  const hint = $("apply-hint");
  button.disabled = configApplying || configPreviewBusy || !configStatus.writes_enabled || !pendingConfigPreview;
  if (configApplying) {
    hint.textContent = "Aplicação em andamento. Aguarde o resultado antes de iniciar outra operação.";
  } else if (configPreviewBusy) {
    hint.textContent = "Aguardando a prévia mais recente.";
  } else if (!configStatus.writes_enabled) {
    hint.textContent = configStatus.message || "O envio de alterações está desabilitado na configuração local.";
  } else if (!pendingConfigPreview) {
    hint.textContent = "Gere uma prévia; depois o botão Aplicar fica disponível.";
  } else {
    hint.textContent = "Prévia pronta. Backup é opcional e recomendado; revise os comandos antes de aplicar.";
  }
  button.title = button.disabled ? hint.textContent : "Aplicar a operação mostrada na prévia";
}

$("apply-config-button").addEventListener("click", async () => {
  if (configApplying || configPreviewBusy || !pendingConfigPreview || !configStatus.writes_enabled) return;
  configApplying = true;
  ++configPreviewRevision;
  updateConfigurationControls();
  const applyButton = $("apply-config-button");
  const previewToApply = pendingConfigPreview;
  pendingConfigPreview = null;
  applyButton.disabled = true;
  applyButton.textContent = "Enviando...";
  const resultBox = $("config-result");
  resultBox.classList.remove("error");
  resultBox.textContent = "";
  try {
    const response = await fetch("/api/config/apply", {
      method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({
        token: previewToApply.token,
      }), cache: "no-store",
    });
    const result = await response.json();
    if (result.cli_only) loadConfigurationStatus();  // marks the operation "(use a CLI)"
    if (!response.ok || !result.ok) throw new Error(result.error || "O switch não aceitou a operação.");
    pendingConfigPreview = null;
    resultBox.textContent = result.message;
    showConfigFeedback(result.message);
    const output = document.createElement("pre");
    output.textContent = result.outputs.map(item => `${item.command}\n${item.output}`).join("\n\n");
    resultBox.append(output);
    if (result.snapshot) renderVirtual(result.snapshot);
    if (appStatus.mode === "serial") await refreshPhysical(false);
    await loadConfigurationStatus();
  } catch (error) {
    resultBox.classList.add("error");
    resultBox.textContent = error.message;
    showConfigFeedback(error.message, true);
  } finally {
    configApplying = false;
    applyButton.textContent = "Aplicar no switch";
    updateConfigurationControls();
  }
});

let cliAvailable = false;
function setCliConnected(connected, note = "") {
  cliConnected = connected;
  if (connected) cliOccupiedElsewhere = false;
  const usable = connected && cliAvailable && !cliStateUncertain && !cliClosing && !cliOpening && !cliSending;
  $("cli-state").textContent = cliClosing ? "Encerrando…" : cliOpening ? "Conectando…" : cliSending ? "Aguardando resposta…" : cliStateUncertain ? "Estado a confirmar" : connected ? (appStatus.mode === "virtual" ? "Conectado · virtual" : "Conectado · serial") : "Desconectado";
  $("cli-state").classList.toggle("cli-online", usable);
  $("cli-open").disabled = cliOpening || cliSending || cliClosing || Boolean(cliToken) || connected || cliOccupiedElsewhere || !cliAvailable || !["serial","virtual"].includes(appStatus.mode);
  $("cli-close").disabled = !cliToken || cliClosing || cliOpening || cliSending;
  $("cli-command").disabled = !usable;
  $("cli-enter").disabled = !usable;
  $("cli-send").disabled = !usable;
  if (note) $("cli-connection-note").textContent = note;
}

function appendCliOutput(text, command = "") {
  const output = $("cli-output");
  const hint = output.querySelector(".cli-hint");
  if (hint) hint.remove();
  const block = document.createElement("div");
  block.className = "cli-block";
  if (command) {
    const sent = document.createElement("div");
    sent.className = "cli-sent";
    const redact = cliSensitivePrompt || /password|passwd|secret|community|usm-user|cipher|key-string/i.test(command);
    sent.textContent = redact ? "> [entrada oculta]" : `> ${command}`;
    block.append(sent);
  }
  const received = document.createElement("pre");
  const redactEcho = cliSensitivePrompt || /password|passwd|secret|community|usm-user|cipher|key-string/i.test(command);
  received.textContent = (redactEcho && command ? text.split(command).join("[entrada oculta]") : text) || "(sem saída)";
  block.append(received);
  output.append(block);
  output.scrollTop = output.scrollHeight;
  cliSensitivePrompt = /password|passwd|secret/i.test(text);
}

function forgetCliToken() {
  ++cliStateRevision;
  cliToken = "";
  sessionStorage.removeItem("qm_cli_token");
  cliStateUncertain = false;
}

async function loadCliStatus() {
  if (cliStatusTimer) window.clearTimeout(cliStatusTimer);
  cliStatusTimer = null;
  const requestRevision = ++cliStatusRevision;
  const stateRevision = cliStateRevision;
  const tokenAtRequest = cliToken;
  const isCurrent = () => requestRevision === cliStatusRevision && stateRevision === cliStateRevision && tokenAtRequest === cliToken && !cliOpening && !cliClosing && !cliSending;
  try {
    const response = await fetch("/api/cli/status", { cache: "no-store", signal: AbortSignal.timeout(5000), headers: cliToken ? {"X-CLI-Token": cliToken} : {} });
    if (!response.ok) throw new Error("Estado do console indisponível.");
    const status = await response.json();
    if (!isCurrent()) return;
    cliAvailable = Boolean(status.available);
    if (status.busy || status.token_valid === null) {
      cliStateUncertain = Boolean(cliToken);
      setCliConnected(false, "Console ocupado. Aguardando confirmação do estado; o acesso desta aba foi preservado.");
      if (cliToken) cliStatusTimer = window.setTimeout(loadCliStatus, 1500);
    } else if (!status.connected) {
      forgetCliToken();
      cliOccupiedElsewhere = false;
      setCliConnected(false, status.available ? (status.mode === "virtual" ? "Console virtual disponível. Nenhum cabo necessário." : "Console serial configurado. Feche outros terminais antes de conectar.") : status.message);
    } else if (cliToken && status.token_valid === true) {
      cliStateUncertain = false;
      setCliConnected(true, "Sessão recuperada nesta aba. Envie somente comandos destinados a este Quidway.");
    } else {
      if (status.token_valid === false) forgetCliToken();
      cliOccupiedElsewhere = true;
      setCliConnected(false, "Já existe uma sessão ativa em outra aba. Encerre-a na aba original.");
    }
  } catch {
    if (!isCurrent()) return;
    cliStateUncertain = Boolean(cliToken);
    setCliConnected(false, "Não foi possível confirmar o estado. O acesso desta aba foi preservado; tente Encerrar novamente.");
  }
}

$("cli-open").addEventListener("click", async () => {
  if (cliOpening || cliClosing || cliSending || cliToken || cliConnected || cliOccupiedElsewhere || !cliAvailable) return;
  cliOpening = true;
  ++cliStateRevision;
  setCliConnected(cliConnected);
  const button = $("cli-open");
  button.disabled = true;
  button.textContent = "Conectando…";
  $("cli-result").textContent = "";
  try {
    const response = await fetch("/api/cli/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", cache: "no-store" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível abrir o console serial.");
    ++cliStateRevision;
    cliToken = result.token;
    cliStateUncertain = false;
    sessionStorage.setItem("qm_cli_token", cliToken);
    appendCliOutput(result.output, "");
    setCliConnected(true, `Conectado a ${result.port} · ${result.baud_rate} baud. Se aparecer Username/Password, digite um valor por linha.`);
    $("cli-command").focus();
  } catch (error) {
    $("cli-result").classList.add("error");
    $("cli-result").textContent = error.message;
    setCliConnected(false);
  } finally {
    cliOpening = false;
    ++cliStateRevision;
    button.textContent = "Conectar ao console";
    setCliConnected(cliConnected);
  }
});

async function closeCli({keepalive = false} = {}) {
  if (!cliToken || cliClosing) return false;
  const tokenToClose = cliToken;
  cliClosing = true;
  ++cliStateRevision;
  setCliConnected(cliConnected);
  try {
    const response = await fetch("/api/cli/close", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: tokenToClose }), cache: "no-store", keepalive, signal: AbortSignal.timeout(8000) });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível encerrar a sessão.");
    if (cliToken === tokenToClose) {
      forgetCliToken();
      setCliConnected(false);
    }
    $("cli-result").classList.remove("error");
    $("cli-result").textContent = result.message;
    return true;
  } catch (error) {
    cliStateUncertain = true;
    $("cli-result").classList.add("error");
    $("cli-result").textContent = error.message + " O encerramento não foi confirmado; tente Encerrar novamente.";
    return false;
  } finally {
    cliClosing = false;
    ++cliStateRevision;
    setCliConnected(cliConnected);
  }
}

$("cli-close").addEventListener("click", () => closeCli());

$("cli-command-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!cliConnected || !cliToken || cliStateUncertain || cliClosing || cliOpening || cliSending) return;
  const typed = $("cli-command").value.trim();
  if (!typed) return;
  const command = typed === "<ENTER>" ? "" : typed;
  cliSending = true;
  ++cliStateRevision;
  setCliConnected(cliConnected);
  const sendButton = $("cli-send");
  sendButton.disabled = true;
  sendButton.textContent = "Aguardando…";
  $("cli-result").classList.remove("error");
  $("cli-result").textContent = "";
  try {
    const response = await fetch("/api/cli/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: cliToken, command }), cache: "no-store" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível enviar o comando.");
    appendCliOutput(result.output, command || "↵ Enter");
    if (appStatus.mode === "virtual") loadVirtualState();
    if (result.timed_out) $("cli-result").textContent = "O tempo de leitura terminou antes do prompt. Mostrei a saída parcial e não reenviei o comando; verifique a tela do switch antes de continuar.";
    $("cli-command").value = "";
  } catch (error) {
    $("cli-result").classList.add("error");
    $("cli-result").textContent = error.message;
    if (/sessão expirou|encerrada|falhou/i.test(error.message)) {
      forgetCliToken();
      setCliConnected(false);
    }
  } finally {
    cliSending = false;
    ++cliStateRevision;
    sendButton.textContent = "Enviar";
    setCliConnected(cliConnected);
    $("cli-command").focus();
  }
});

$("cli-enter").addEventListener("click", () => {
  if (!cliConnected || cliSending || cliOpening || cliClosing || cliStateUncertain) return;
  $("cli-command").value = "<ENTER>";
  $("cli-command-form").requestSubmit();
});

$("cli-command").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (cliSending || cliOpening || cliClosing || cliStateUncertain) return;
    $("cli-command-form").requestSubmit();
  }
});

$("cli-clear").addEventListener("click", () => {
  $("cli-output").replaceChildren();
  $("cli-result").textContent = "Saída removida somente da tela. Nenhum comando foi enviado ao switch.";
});

window.addEventListener("pagehide", () => {
  // A missing reply leaves the token available for recovery on the next page.
  if (cliStatusTimer) window.clearTimeout(cliStatusTimer);
  if (cliToken) closeCli({keepalive: true});
});
window.addEventListener("pageshow", () => {
  if (appStatus.mode !== "demo" && (pageKind === "cli" || cliToken)) loadCliStatus();
});

// Console login for switches that ask for credentials. The password is sent once
// and the field is cleared; nothing is kept in the browser (no storage, no autofill).
function showConsoleLoginMessage(text, error = false) {
  const message = $("console-login-message");
  message.textContent = text;
  message.classList.toggle("error", error);
}

$("console-login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = JSON.stringify({ username: form.username.value, password: form.password.value });
  form.password.value = "";
  try {
    const response = await fetch("/api/console/credentials", {
      method: "POST", headers: { "Content-Type": "application/json" }, body, cache: "no-store",
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível enviar as credenciais.");
    showConsoleLoginMessage(result.message);
    loadPhysicalCache();
  } catch (error) {
    showConsoleLoginMessage(error.message, true);
  }
});

$("console-confirm").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/console/credentials/confirm", { method: "POST", cache: "no-store" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível confirmar.");
    showConsoleLoginMessage(result.message);
    loadPhysicalCache();
  } catch (error) {
    showConsoleLoginMessage(error.message, true);
  }
});

$("console-forget").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/console/credentials/forget", { method: "POST", cache: "no-store" });
    const result = await response.json();
    showConsoleLoginMessage(result.message || "Credenciais descartadas.");
  } catch (error) {
    showConsoleLoginMessage(error.message, true);
  }
});
