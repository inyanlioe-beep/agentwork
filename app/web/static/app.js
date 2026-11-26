(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => document.querySelectorAll(selector);
  const state = {
    agents: [],
    tasks: [],
    selectedTask: null,
    messages: [],
    steps: [],
    streamingNodes: new Map(),
    messageFilter: "all",
    socket: null,
    pollHandle: null,
    toastHandle: null,
    taskBusy: false,
    providers: null,
    editingAgent: null,
  };
  const statusLabels = {
    queued: "Menunggu",
    planning: "Menyusun rencana",
    running: "Sedang berjalan",
    review: "Review hasil",
    waiting_input: "Menunggu jawaban",
    done: "Selesai",
    failed: "Gagal",
    cancelled: "Dibatalkan",
  };
  const kindLabels = {
    instruction: "Instruksi",
    assignment: "Delegasi",
    response: "Hasil",
    question: "Pertanyaan",
    review: "Review",
    summary: "Ringkasan",
    error: "Error",
    system: "Sistem",
  };

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(state.toastHandle);
    state.toastHandle = setTimeout(() => toast.classList.remove("visible"), 4500);
  }

  async function api(path, options = {}) {
    let response;
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] ||= "application/json";
    try {
      response = await fetch(path, {
        ...options,
        headers,
      });
    } catch {
      throw new Error("Tidak dapat terhubung ke server. Pastikan aplikasi masih berjalan.");
    }
    if (!response.ok) {
      let detail = `Permintaan gagal (${response.status})`;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch {
        // Keep the HTTP status when the server does not return JSON.
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function agentById(id) {
    return state.agents.find((agent) => agent.id === id);
  }

  function initials(agent) {
    if (!agent) return "?";
    return (agent.name || agent.id).trim().slice(0, 1).toUpperCase();
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return new Intl.DateTimeFormat("id-ID", { hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function formatDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("id-ID", { day: "numeric", month: "short", year: "numeric" }).format(new Date(value));
  }

  function renderTasks() {
    const list = $("#task-list");
    $("#task-count").textContent = String(state.tasks.length);
    if (!state.tasks.length) {
      list.innerHTML = '<div class="no-tasks">Tugas yang kamu buat akan muncul di sini.</div>';
      return;
    }
    list.replaceChildren(...state.tasks.map((task) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `task-item${task.id === state.selectedTask?.id ? " selected" : ""}`;
      button.title = task.title;
      button.innerHTML = `<span class="mini-status ${escapeClass(task.status)}"></span><span class="task-name"></span>`;
      button.querySelector(".task-name").textContent = task.title || "Tugas tanpa judul";
      button.addEventListener("click", () => selectTask(task.id));
      return button;
    }));
  }

  function renderAgents() {
    $("#agent-count").textContent = String(state.agents.length);
    const container = $("#agent-list");
    container.replaceChildren(...state.agents.map((agent) => {
      const card = document.createElement("div");
      card.className = "agent-card";
      card.innerHTML = `<span class="agent-avatar" data-role="${escapeAttr(agent.role)}" data-id="${escapeAttr(agent.id)}"></span>
        <span class="agent-details"><span class="agent-name"></span><span class="agent-role"></span></span>
        <span class="agent-state" title="Siap"></span>`;
      card.querySelector(".agent-avatar").textContent = initials(agent);
      card.querySelector(".agent-name").textContent = agent.name;
      card.querySelector(".agent-role").textContent = agent.role === "orchestrator" ? "Orchestrator" : "Spesialis";
      return card;
    }));
  }

  function renderAgentFilters() {
    const container = $("#agent-filters");
    const filters = [{ id: "all", name: "Semua" }, ...state.agents];
    container.replaceChildren(...filters.map((agent) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `agent-filter${state.messageFilter === agent.id ? " selected" : ""}`;
      button.textContent = agent.name || "Semua";
      button.setAttribute("aria-pressed", String(state.messageFilter === agent.id));
      button.addEventListener("click", () => {
        state.messageFilter = agent.id;
        renderAgentFilters();
        renderMessages();
      });
      return button;
    }));
  }

  function renderStatus(task) {
    const badge = $("#task-status");
    badge.className = `status-badge status-${escapeClass(task.status)}`;
    badge.querySelector("span:last-child").textContent = statusLabels[task.status] || task.status;
    const waitingForAnswer = task.status === "waiting_input";
    const finished = ["done", "failed", "cancelled"].includes(task.status);
    $("#question-panel").classList.toggle("hidden", !waitingForAnswer);
    $("#pending-question").textContent = task.pending_question || "";
    $("#cancel-task").classList.toggle("hidden", finished);
    $("#working-indicator").classList.toggle(
      "hidden",
      waitingForAnswer || finished,
    );
  }

  function renderSteps() {
    const container = $("#plan-list");
    if (!state.steps.length) {
      container.innerHTML = '<div class="empty-plan">Rencana muncul saat orkestrator mulai bekerja.</div>';
      return;
    }
    container.replaceChildren(...state.steps.map((step) => {
      const item = document.createElement("div");
      item.className = `plan-item ${escapeClass(step.status)}`;
      const marker = step.status === "done" ? "✓" : step.status === "failed" ? "!" : step.index;
      const agent = agentById(step.agent_id);
      item.innerHTML = `<span class="plan-marker"></span><span><span class="plan-description"></span><span class="plan-agent"></span></span>`;
      item.querySelector(".plan-marker").textContent = marker;
      item.querySelector(".plan-description").textContent = step.description;
      item.querySelector(".plan-agent").textContent = agent ? `Dikerjakan ${agent.name}` : step.agent_id;
      return item;
    }));
  }

  function renderMessages() {
    const list = $("#message-list");
    const visibleMessages = state.messageFilter === "all" ? state.messages : state.messages.filter(
      (message) => message.sender === state.messageFilter || message.receiver === state.messageFilter,
    );
    $("#activity-count").textContent = state.messageFilter === "all"
      ? `${state.messages.length} pesan`
      : `${visibleMessages.length} dari ${state.messages.length} pesan`;
    if (!visibleMessages.length) {
      const hasAnyMessages = state.messages.length > 0;
      list.innerHTML = hasAnyMessages
        ? '<div class="empty-messages"><strong>Tidak ada pesan dari agent ini</strong><span>Pilih agent lain untuk melihat aktivitasnya.</span></div>'
        : '<div class="empty-messages"><strong>Ruang tugas siap</strong><span>Aktivitas orchestrator dan tim akan tampil di sini.</span></div>';
      refreshStreamingNodes(list);
      return;
    }
    const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
    list.replaceChildren(...visibleMessages.map((message) => messageElement(message)));
    refreshStreamingNodes(list);
    keepLatestVisible(list, wasNearBottom);
  }

  function refreshStreamingNodes(list) {
    for (const [id] of state.streamingNodes) {
      const node = [...list.querySelectorAll(".message[data-message-id]")]
        .find((message) => message.dataset.messageId === id);
      state.streamingNodes.set(id, node || null);
    }
  }

  function keepLatestVisible(list, wasNearBottom) {
    if (wasNearBottom) {
      list.scrollTop = list.scrollHeight;
      $("#jump-latest").classList.add("hidden");
    } else {
      $("#jump-latest").classList.remove("hidden");
    }
  }

  function beginStreamingMessage(payload) {
    const index = state.messages.findIndex((message) => message.id === payload.id);
    const message = { ...payload, streaming: true };
    if (index >= 0) state.messages[index] = message;
    else state.messages.push(message);
    state.streamingNodes.set(message.id, null);
    $("#message-list").classList.add("is-streaming");
    renderMessages();
  }

  function appendMessageDelta(payload) {
    let message = state.messages.find((item) => item.id === payload.id);
    if (!message && payload.sender) {
      message = {
        id: payload.id,
        task_id: payload.task_id,
        sender: payload.sender,
        receiver: payload.receiver,
        kind: payload.kind,
        created_at: payload.created_at,
        content: "",
        streaming: true,
      };
      state.messages.push(message);
      state.streamingNodes.set(message.id, null);
      $("#message-list").classList.add("is-streaming");
      renderMessages();
    }
    if (!message) return;
    message.content += payload.content;
    const list = $("#message-list");
    const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
    const node = state.streamingNodes.get(payload.id);
    const body = node?.querySelector(".message-body");
    const cursor = body?.querySelector(".typing-cursor");
    if (body && cursor) body.insertBefore(document.createTextNode(payload.content), cursor);
    keepLatestVisible(list, wasNearBottom);
  }

  function resetStreamingMessage(payload) {
    const message = state.messages.find((item) => item.id === payload.id);
    if (message) message.content = "";
    const body = state.streamingNodes.get(payload.id)?.querySelector(".message-body");
    const cursor = body?.querySelector(".typing-cursor");
    if (body && cursor) body.replaceChildren(cursor);
    const list = $("#message-list");
    list.scrollTop = list.scrollHeight;
  }

  function discardStreamingMessage(payload) {
    state.messages = state.messages.filter((message) => message.id !== payload.id);
    state.streamingNodes.delete(payload.id);
    renderMessages();
    if (![...state.streamingNodes.keys()].length) $("#message-list").classList.remove("is-streaming");
  }

  function finishStreamingMessage(message) {
    const index = state.messages.findIndex((item) => item.id === message.id);
    if (index >= 0) state.messages[index] = message;
    else state.messages.push(message);
    state.streamingNodes.delete(message.id);
    renderMessages();
    if (![...state.streamingNodes.keys()].length) $("#message-list").classList.remove("is-streaming");
  }

  function syncPersistedMessages(messages) {
    const persistedIds = new Set(messages.map((message) => message.id));
    const activeStreams = state.messages.filter(
      (message) => message.streaming && !persistedIds.has(message.id),
    );
    state.messages = [...messages, ...activeStreams];
    state.streamingNodes.clear();
    for (const message of activeStreams) state.streamingNodes.set(message.id, null);
    $("#message-list").classList.toggle("is-streaming", activeStreams.length > 0);
    renderMessages();
  }

  function clearStreamingMessages() {
    state.messages = state.messages.filter((message) => !message.streaming);
    state.streamingNodes.clear();
    $("#message-list").classList.remove("is-streaming");
    renderMessages();
  }

  function messageElement(message) {
    const sender = agentById(message.sender);
    const labels = { human: "Kamu", system: "Workspace" };
    const displayName = sender?.name || labels[message.sender] || message.sender;
    const avatarClass = sender?.role === "orchestrator" ? "orion"
      : message.sender === "human" ? "human"
        : message.sender === "system" ? "system"
          : sender ? "worker" : "";
    const messageNode = document.createElement("article");
    messageNode.className = "message";
    messageNode.dataset.messageId = message.id;
    const route = message.receiver && message.receiver !== "human"
      ? `<span class="message-route">→ ${escapeHtml(agentById(message.receiver)?.name || message.receiver)}</span>` : "";
    messageNode.innerHTML = `<div class="message-avatar ${avatarClass}"></div>
      <div class="message-content"><div class="message-head"><span class="message-name"></span>${route}
      <time class="message-time"></time></div><div class="message-body"></div></div>`;
    messageNode.querySelector(".message-avatar").textContent = sender
      ? initials(sender) : message.sender === "human" ? "K" : "•";
    messageNode.querySelector(".message-name").textContent = displayName;
    messageNode.querySelector(".message-time").textContent = formatTime(message.created_at);
    const body = messageNode.querySelector(".message-body");
    body.textContent = message.content;
    if (message.streaming) {
      messageNode.classList.add("is-streaming");
      body.setAttribute("aria-live", "off");
      const cursor = document.createElement("span");
      cursor.className = "typing-cursor";
      cursor.setAttribute("aria-hidden", "true");
      body.append(cursor);
    }
    if (message.kind === "error") body.classList.add("is-error");
    if (message.kind === "summary") body.classList.add("is-summary");
    if (kindLabels[message.kind]) {
      const tag = document.createElement("span");
      tag.className = "message-kind";
      tag.textContent = kindLabels[message.kind];
      messageNode.querySelector(".message-head").append(tag);
    }
    return messageNode;
  }

  async function loadTasks() {
    state.tasks = await api("/api/tasks");
    renderTasks();
    return state.tasks;
  }

  async function loadAgents() {
    state.agents = await api("/api/agents");
    renderAgents();
    renderAgentFilters();
    renderMessages();
    return state.agents;
  }

  function activateTask(task) {
    if (state.selectedTask?.id !== task.id) {
      state.messages = [];
      state.steps = [];
      state.streamingNodes.clear();
      $("#message-list").classList.remove("is-streaming");
    }
    state.selectedTask = task;
    $("#welcome-view").classList.add("hidden");
    $("#conversation-view").classList.remove("hidden");
    $("#conversation-title").textContent = task.title;
    $("#breadcrumb-task").textContent = task.title;
    $("#task-created").textContent = formatDate(task.created_at);
    $("#task-id-short").textContent = task.id.slice(0, 8);
    renderStatus(task);
    renderSteps();
    renderMessages();
    renderTasks();
  }

  async function selectTask(taskId) {
    try {
      const knownTask = state.tasks.find((task) => task.id === taskId);
      if (knownTask) activateTask(knownTask);
      const [detail, messages] = await Promise.all([
        api(`/api/tasks/${encodeURIComponent(taskId)}`),
        api(`/api/tasks/${encodeURIComponent(taskId)}/messages`),
      ]);
      state.selectedTask = detail.task;
      state.steps = detail.steps;
      syncPersistedMessages(messages);
      $("#conversation-title").textContent = detail.task.title;
      $("#breadcrumb-task").textContent = detail.task.title;
      $("#task-created").textContent = formatDate(detail.task.created_at);
      $("#task-id-short").textContent = detail.task.id.slice(0, 8);
      renderStatus(detail.task);
      renderSteps();
      renderTasks();
      const list = $("#message-list");
      if (list.scrollHeight - list.scrollTop - list.clientHeight < 90) {
        list.scrollTop = list.scrollHeight;
      }
    } catch (error) {
      showToast(error.message);
    }
  }

  async function submitTask(form, input) {
    const instruction = input.value.trim();
    if (!instruction || state.taskBusy) return;
    const button = form.querySelector('button[type="submit"]');
    state.taskBusy = true;
    if (button) button.disabled = true;
    try {
      const task = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({ instruction }),
      });
      input.value = "";
      activateTask(task);
      await Promise.all([loadTasks(), selectTask(task.id)]);
      startPolling();
    } catch (error) {
      showToast(error.message);
    } finally {
      state.taskBusy = false;
      if (button) button.disabled = false;
    }
  }

  async function submitHumanAnswer(event) {
    event.preventDefault();
    if (!state.selectedTask) return;
    const form = event.currentTarget;
    const input = $("#human-answer");
    const button = form.querySelector('button[type="submit"]');
    const answer = input.value.trim();
    if (!answer || button.disabled) return;
    button.disabled = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(state.selectedTask.id)}/answer`, {
        method: "POST",
        body: JSON.stringify({ answer }),
      });
      input.value = "";
      await refreshSelectedTask();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function cancelSelectedTask() {
    if (!state.selectedTask || !window.confirm("Batalkan eksekusi tugas ini?")) return;
    const button = $("#cancel-task");
    button.disabled = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(state.selectedTask.id)}/cancel`, {
        method: "POST",
      });
      await refreshSelectedTask();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function exportSelectedTranscript() {
    if (!state.selectedTask) return;
    const button = $("#export-transcript");
    const taskId = state.selectedTask.id;
    button.disabled = true;
    try {
      let response;
      try {
        response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/export`);
      } catch {
        throw new Error("Tidak dapat terhubung ke server. Pastikan aplikasi masih berjalan.");
      }
      if (!response.ok) {
        let detail = `Ekspor transcript gagal (${response.status})`;
        try {
          detail = (await response.json()).detail || detail;
        } catch {
          // Keep the HTTP status when the server does not return JSON.
        }
        throw new Error(detail);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `agentwork-${taskId}.md`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function refreshSelectedTask() {
    if (!state.selectedTask) return;
    try {
      const [detail, messages] = await Promise.all([
        api(`/api/tasks/${encodeURIComponent(state.selectedTask.id)}`),
        api(`/api/tasks/${encodeURIComponent(state.selectedTask.id)}/messages`),
      ]);
      state.selectedTask = detail.task;
      state.steps = detail.steps;
      if (messages.length !== state.messages.filter((message) => !message.streaming).length) {
        syncPersistedMessages(messages);
      }
      renderStatus(detail.task);
      renderSteps();
      renderMessages();
      await loadTasks();
      if (["done", "failed", "cancelled"].includes(detail.task.status)) {
        stopPolling();
      }
    } catch (error) {
      console.error("Task refresh failed", error);
    }
  }

  function startPolling() {
    clearInterval(state.pollHandle);
    state.pollHandle = setInterval(refreshSelectedTask, 1500);
    refreshSelectedTask();
  }

  function stopPolling() {
    clearInterval(state.pollHandle);
    state.pollHandle = null;
  }

  function connectSocket() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/ws`);
    state.socket = socket;
    socket.addEventListener("open", () => {
      $("#connection-dot").className = "connection-dot connected";
      $("#connection-label").textContent = "Terhubung";
    });
    socket.addEventListener("message", (event) => {
      let item;
      try {
        item = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!state.selectedTask || item.task_id !== state.selectedTask.id) return;
      if (item.type === "message") {
        finishStreamingMessage(item.payload);
      } else if (item.type === "message_start") {
        beginStreamingMessage(item.payload);
      } else if (item.type === "message_delta") {
        appendMessageDelta({ ...item.payload, task_id: item.task_id });
      } else if (item.type === "message_reset") {
        resetStreamingMessage(item.payload);
      } else if (item.type === "message_discard") {
        discardStreamingMessage(item.payload);
      } else if (item.type === "step_update") {
        const index = state.steps.findIndex((step) => step.id === item.payload.id);
        if (index >= 0) state.steps[index] = item.payload;
        else state.steps.push(item.payload);
        renderSteps();
      } else if (item.type === "task_update") {
        state.selectedTask = item.payload;
        if (["done", "failed", "cancelled"].includes(item.payload.status)) {
          stopPolling();
          if (state.streamingNodes.size) clearStreamingMessages();
        }
        renderStatus(item.payload);
        loadTasks().catch((error) => console.error("Task list refresh failed", error));
      }
    });
    socket.addEventListener("close", () => {
      $("#connection-dot").className = "connection-dot disconnected";
      $("#connection-label").textContent = "Menyambung ulang...";
      setTimeout(connectSocket, 2000);
    });
    socket.addEventListener("error", () => socket.close());
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }

  function escapeClass(value) {
    return /^[a-z0-9_-]+$/i.test(value || "") ? value : "";
  }

  async function loadProviders() {
    if (state.providers) return state.providers;
    state.providers = await api("/api/providers");
    return state.providers;
  }

  function openAgentDialog(agent = null) {
    state.editingAgent = agent;
    const dialog = $("#agent-dialog");
    const form = $("#agent-form");
    const title = $("#agent-dialog-title");
    const submitBtn = $("#agent-submit");
    const idInput = $("#agent-id");

    title.textContent = agent ? "Edit Agent" : "Tambah Agent";
    submitBtn.textContent = agent ? "Simpan Perubahan" : "Simpan Agent";
    idInput.disabled = !!agent;

    if (agent) {
      form.id.value = agent.id;
      form.name.value = agent.name;
      form.emoji.value = agent.emoji;
      form.provider.value = agent.provider;
      form.model.value = agent.model;
      form.role.value = agent.role;
      form.persona.value = agent.persona;
      form.color.value = agent.color;
    } else {
      form.reset();
      form.color.value = "#8b5cf6";
    }

    loadProviders().then((data) => {
      const providerSelect = $("#agent-provider");
      const modelSelect = $("#agent-model");
      providerSelect.innerHTML = '<option value="">Pilih provider...</option>' +
        data.providers.map((p) => `<option value="${escapeAttr(p.id)}">${escapeHtml(p.name)}</option>`).join("");
      if (agent) {
        providerSelect.value = agent.provider;
        updateModelOptions(agent.provider, agent.model);
      }
      providerSelect.addEventListener("change", () => updateModelOptions(providerSelect.value));
    });

    dialog.showModal();
  }

  function updateModelOptions(provider, selectedModel = "") {
    const modelSelect = $("#agent-model");
    if (!provider || !state.providers) {
      modelSelect.innerHTML = '<option value="">Pilih model...</option>';
      return;
    }
    const models = state.providers.models[provider] || [];
    if (!models.length) {
      modelSelect.innerHTML = '<option value="">Custom model (input manual)</option>';
      modelSelect.disabled = false;
      const customInput = document.createElement("input");
      customInput.type = "text";
      customInput.id = "agent-model-custom";
      customInput.name = "model";
      customInput.placeholder = "nama-model";
      customInput.required = true;
      customInput.maxLength = 200;
      customInput.value = selectedModel;
      modelSelect.replaceWith(customInput);
      return;
    }
    const existingCustom = $("#agent-model-custom");
    if (existingCustom) {
      const select = document.createElement("select");
      select.id = "agent-model";
      select.name = "model";
      select.required = true;
      existingCustom.replaceWith(select);
    }
    modelSelect.innerHTML = '<option value="">Pilih model...</option>' +
      models.map((m) => `<option value="${escapeAttr(m)}">${escapeHtml(m)}</option>`).join("");
    if (selectedModel) modelSelect.value = selectedModel;
  }

  async function saveAgent(formData) {
    const agentData = {
      id: formData.get("id"),
      name: formData.get("name"),
      emoji: formData.get("emoji") || "🤖",
      provider: formData.get("provider"),
      model: formData.get("model"),
      role: formData.get("role"),
      persona: formData.get("persona"),
      color: formData.get("color") || "#8b5cf6",
    };
    if (state.editingAgent) {
      delete agentData.id;
      await api(`/api/agents/${encodeURIComponent(state.editingAgent.id)}`, {
        method: "PUT",
        body: JSON.stringify(agentData),
      });
      showToast(`Agent ${state.editingAgent.name} berhasil diupdate`);
    } else {
      await api("/api/agents", {
        method: "POST",
        body: JSON.stringify(agentData),
      });
      showToast(`Agent ${agentData.name} berhasil dibuat`);
    }
    await loadAgents();
  }

  function confirmDeleteAgent(agent) {
    const dialog = $("#agent-delete-dialog");
    $("#delete-agent-name").textContent = agent.name;
    $("#confirm-delete-agent").onclick = async () => {
      try {
        await api(`/api/agents/${encodeURIComponent(agent.id)}`, { method: "DELETE" });
        showToast(`Agent ${agent.name} berhasil dihapus`);
        dialog.close();
        await loadAgents();
      } catch (error) {
        showToast(error.message);
      }
    };
    dialog.showModal();
  }

  async function toggleAgent(agent) {
    try {
      await api(`/api/agents/${encodeURIComponent(agent.id)}/toggle`, { method: "PATCH" });
      showToast(`Agent ${agent.name} ${agent.enabled ? "dinonaktifkan" : "diaktifkan"}`);
      await loadAgents();
    } catch (error) {
      showToast(error.message);
    }
  }

  function showAgentManagement() {
    const list = state.agents.map((agent) => {
      const isOrch = agent.role === "orchestrator";
      return `
        <div class="agent-card" style="border-color: ${escapeAttr(agent.color)}">
          <div class="agent-card-header">
            <div class="agent-card-avatar" style="background: ${escapeAttr(agent.color)}">
              ${escapeHtml(agent.emoji)}
            </div>
            <div class="agent-card-info">
              <strong>${escapeHtml(agent.name)}</strong>
              <small>${escapeHtml(agent.provider)} · ${escapeHtml(agent.model)}</small>
            </div>
            <div class="agent-card-actions">
              ${!isOrch ? `<button type="button" class="icon-button" data-action="toggle" data-id="${escapeAttr(agent.id)}" title="${agent.enabled ? "Nonaktifkan" : "Aktifkan"}">
                <svg viewBox="0 0 24 24"><path d="${agent.enabled ? "M17 8l-10 8V8z" : "M8 5v14l11-7z"}"/></svg>
              </button>` : ""}
              <button type="button" class="icon-button" data-action="edit" data-id="${escapeAttr(agent.id)}" title="Edit">
                <svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
              </button>
              ${!isOrch ? `<button type="button" class="icon-button danger" data-action="delete" data-id="${escapeAttr(agent.id)}" title="Hapus">
                <svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6v14H5V6"/></svg>
              </button>` : ""}
            </div>
          </div>
          <p class="agent-card-persona">${escapeHtml(agent.persona.slice(0, 150))}${agent.persona.length > 150 ? "..." : ""}</p>
          ${agent.enabled ? "" : '<div class="agent-card-badge">Nonaktif</div>'}
        </div>
      `;
    }).join("");

    const dialog = document.createElement("dialog");
    dialog.className = "agent-management-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <div class="dialog-header">
          <h2>Kelola Agent</h2>
          <button type="button" class="dialog-close" aria-label="Tutup">×</button>
        </div>
        <div class="dialog-body agent-management-body">
          <button type="button" class="send-button" id="add-agent-btn">
            <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
            <span>Tambah Agent Baru</span>
          </button>
          <div class="agent-card-list">${list}</div>
        </div>
      </form>
    `;
    document.body.appendChild(dialog);
    dialog.querySelector(".dialog-close").addEventListener("click", () => {
      dialog.close();
      dialog.remove();
    });
    dialog.querySelector("#add-agent-btn").addEventListener("click", () => openAgentDialog());
    dialog.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const agentId = btn.dataset.id;
        const agent = state.agents.find((a) => a.id === agentId);
        if (!agent) return;
        if (btn.dataset.action === "edit") openAgentDialog(agent);
        else if (btn.dataset.action === "delete") confirmDeleteAgent(agent);
        else if (btn.dataset.action === "toggle") toggleAgent(agent);
      });
    });
    dialog.showModal();
  }

  function setup() {
    $("#welcome-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitTask(event.currentTarget, $("#welcome-input"));
    });
    $("#conversation-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitTask(event.currentTarget, $("#conversation-input"));
    });
    $("#answer-form").addEventListener("submit", submitHumanAnswer);
    $("#cancel-task").addEventListener("click", cancelSelectedTask);
    $("#export-transcript").addEventListener("click", exportSelectedTranscript);
    $("#manage-agents-button").addEventListener("click", showAgentManagement);
    $("#agent-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const formData = new FormData(event.currentTarget);
      saveAgent(formData)
        .then(() => $("#agent-dialog").close())
        .catch((error) => showToast(error.message));
    });
    $$("#agent-dialog .dialog-close, #agent-dialog [data-action='cancel']").forEach((btn) => {
      btn.addEventListener("click", () => $("#agent-dialog").close());
    });
    $$("#agent-delete-dialog .dialog-close, #agent-delete-dialog [data-action='cancel']").forEach((btn) => {
      btn.addEventListener("click", () => $("#agent-delete-dialog").close());
    });
    for (const input of [$("#welcome-input"), $("#conversation-input")]) {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          input.form.requestSubmit();
        }
      });
    }
    document.querySelectorAll(".suggestion").forEach((button) => {
      button.addEventListener("click", () => {
        $("#welcome-input").value = button.dataset.prompt || "";
        $("#welcome-input").focus();
      });
    });
    $("#new-task-button").addEventListener("click", () => {
      state.selectedTask = null;
      $("#conversation-view").classList.add("hidden");
      $("#welcome-view").classList.remove("hidden");
      $("#breadcrumb-task").textContent = "Tugas baru";
      $("#welcome-input").focus();
      renderTasks();
    });
    $("#refresh-tasks").addEventListener("click", () => {
      Promise.all([loadAgents(), loadTasks()]).catch((error) => showToast(error.message));
    });
    $("#jump-latest").addEventListener("click", () => {
      const list = $("#message-list");
      list.scrollTop = list.scrollHeight;
      $("#jump-latest").classList.add("hidden");
    });
    $("#message-list").addEventListener("scroll", (event) => {
      const list = event.currentTarget;
      if (list.scrollHeight - list.scrollTop - list.clientHeight < 90) $("#jump-latest").classList.add("hidden");
    });
    $("#about-button").addEventListener("click", () => $("#about-dialog").showModal());
    $("#theme-toggle").addEventListener("click", () => {
      document.body.classList.toggle("light-theme");
      localStorage.setItem("agentwork-theme", document.body.classList.contains("light-theme") ? "light" : "dark");
    });
    if (localStorage.getItem("agentwork-theme") === "light") document.body.classList.add("light-theme");
    document.addEventListener("keydown", (event) => {
      if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey
          && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
        event.preventDefault();
        $("#new-task-button").click();
      }
    });
    Promise.all([loadAgents(), loadTasks()])
      .then(async ([, tasks]) => {
        const activeTask = tasks.find((task) => !["done", "failed", "cancelled"].includes(task.status));
        if (activeTask) {
          await selectTask(activeTask.id);
          startPolling();
        } else if (tasks.length) {
          await selectTask(tasks[0].id);
        }
      })
      .catch((error) => showToast(error.message));
    connectSocket();
  }

  setup();
})();
