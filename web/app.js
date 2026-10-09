"use strict";

const form = document.getElementById("deck-form");
const input = document.getElementById("deck-code");
const clearButton = document.getElementById("clear-code");
const generateButton = document.getElementById("generate-button");
const exampleButton = document.getElementById("example-button");
const status = document.getElementById("status");
const fieldError = document.getElementById("field-error");
const feedback = document.getElementById("feedback");
const loadingCover = document.getElementById("loading-cover");
const imageStage = document.getElementById("image-stage");
const radios = [...document.querySelectorAll('input[name="locale"]')];
let busy = false;
let requestVersion = 0;
let activeController;

function saveDraft() {
  try {
    sessionStorage.setItem("deckview-code", input.value);
    sessionStorage.setItem("deckview-locale", radios.find(radio => radio.checked).value);
  } catch { /* Storage may be unavailable; generation still works. */ }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(280, Math.max(150, input.scrollHeight + 2))}px`;
  clearButton.hidden = !input.value;
}

function setStatus(message, state = "idle") {
  status.textContent = message;
  feedback.dataset.state = state;
  document.getElementById("loading-detail").textContent = message;
}

function setBusy(value) {
  busy = value;
  generateButton.disabled = value;
  exampleButton.disabled = value;
  clearButton.disabled = value;
  input.disabled = value;
  radios.forEach(radio => { radio.disabled = value; });
  loadingCover.hidden = !value;
  imageStage.setAttribute("aria-busy", String(value));
  generateButton.setAttribute("aria-busy", String(value));
  document.title = value ? "Создаём изображение — DeckViewer" : "DeckViewer — изображение колоды";
}

async function requestJson(url, options = {}) {
  const controller = new AbortController();
  activeController = controller;
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось выполнить запрос. Повтори попытку.");
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Локальное приложение не ответило. Проверь, что оно запущено, и повтори попытку.");
    if (error instanceof TypeError) throw new Error("Нет связи с локальным приложением. Перезапусти DeckViewer и повтори попытку.");
    throw error;
  } finally {
    clearTimeout(timeout);
    if (activeController === controller) activeController = undefined;
  }
}

const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function pollJob(id, version) {
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    if (version !== requestVersion) return null;
    const job = await requestJson(`/api/jobs/${encodeURIComponent(id)}`);
    if (version !== requestVersion) return null;
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.message);
    setStatus(job.message, "loading");
    await pause(650);
  }
  throw new Error("Загрузка заняла больше пяти минут. Проверь интернет и повтори попытку. Уже загруженные карты сохранены.");
}

function showResult(result) {
  const image = document.getElementById("deck-image");
  image.src = result.imageUrl;
  image.alt = `Колода: ${result.className}, ${result.cardCount} карт${result.sideboardCount ? `, сайдборд: ${result.sideboardCount} карт` : ""}`;
  document.getElementById("sample-tag").hidden = true;
  document.getElementById("preview-subtitle").textContent = `${result.className} · ${result.locale === "ruRU" ? "Карты на русском" : "Карты на английском"}`;
  document.getElementById("result-actions").hidden = false;
  const download = document.getElementById("download-image");
  download.href = `${result.imageUrl}?download=1`;
  download.download = result.filename;
  document.getElementById("open-image").href = result.imageUrl;
  const meta = document.getElementById("result-meta");
  const pieces = [`${result.cardCount} карт`, `${new Intl.NumberFormat("ru-RU").format(result.dust)} пыли`, `${result.width} × ${result.height} px`];
  if (result.sideboardCount) pieces.splice(1, 0, `+${result.sideboardCount} в сайдборде`);
  meta.replaceChildren();
  pieces.forEach((text, index) => {
    if (index) {
      const divider = document.createElement("span");
      divider.textContent = "·";
      divider.setAttribute("aria-hidden", "true");
      meta.append(divider);
    }
    const span = document.createElement("span");
    span.textContent = text;
    span.className = "stat";
    meta.append(span);
  });
  const warning = document.getElementById("result-warning");
  warning.textContent = (result.warnings || []).join(" ");
  warning.hidden = !warning.textContent;
}

async function submitDeck() {
  if (busy) return;
  fieldError.textContent = "";
  input.removeAttribute("aria-invalid");
  if (!input.value.trim()) {
    fieldError.textContent = "Вставь код колоды из Hearthstone.";
    input.setAttribute("aria-invalid", "true");
    setStatus("", "error");
    input.focus();
    return;
  }
  saveDraft();
  const version = ++requestVersion;
  setBusy(true);
  setStatus("Подготавливаем колоду…", "loading");
  try {
    const job = await requestJson("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: input.value, locale: radios.find(radio => radio.checked).value })
    });
    const result = await pollJob(job.id, version);
    if (result && version === requestVersion) {
      showResult(result);
      setStatus("Изображение готово. Можно скачать PNG.", "success");
    }
  } catch (error) {
    if (version !== requestVersion) return;
    fieldError.textContent = error.message;
    if (/код|колод|экспорт/i.test(error.message)) input.setAttribute("aria-invalid", "true");
    setStatus("", "error");
  } finally {
    if (version === requestVersion) {
      setBusy(false);
      if (fieldError.textContent) input.focus();
    }
  }
}

form.addEventListener("submit", event => { event.preventDefault(); submitDeck(); });
input.addEventListener("input", () => {
  resizeInput();
  saveDraft();
  fieldError.textContent = "";
  input.removeAttribute("aria-invalid");
});
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && event.ctrlKey && !event.isComposing) {
    event.preventDefault();
    submitDeck();
  }
});
radios.forEach(radio => radio.addEventListener("change", saveDraft));
clearButton.addEventListener("click", () => {
  if (busy) return;
  input.value = "";
  input.removeAttribute("aria-invalid");
  fieldError.textContent = "";
  resizeInput();
  saveDraft();
  setStatus("Готово к работе");
  input.focus();
});
exampleButton.addEventListener("click", async () => {
  if (busy) return;
  const version = ++requestVersion;
  setBusy(true);
  setStatus("Выбираем колоду для примера…", "loading");
  try {
    const example = await requestJson("/api/example");
    if (version !== requestVersion) return;
    input.value = example.code;
    resizeInput();
    saveDraft();
    setBusy(false);
    await submitDeck();
  } catch (error) {
    if (version !== requestVersion) return;
    fieldError.textContent = error.message;
    setStatus("", "error");
  } finally { if (version === requestVersion) setBusy(false); }
});
window.addEventListener("pagehide", () => {
  ++requestVersion;
  activeController?.abort();
  if (busy) {
    setBusy(false);
    setStatus("Можно создать изображение ещё раз.");
  }
});
document.getElementById("deck-image").addEventListener("error", () => {
  if (document.getElementById("sample-tag").hidden) setStatus("Не удалось открыть PNG. Попробуй ссылку «Открыть PNG» или создай изображение ещё раз.", "error");
});

try {
  input.value = sessionStorage.getItem("deckview-code") || "";
  const locale = sessionStorage.getItem("deckview-locale");
  radios.forEach(radio => { if (radio.value === locale) radio.checked = true; });
} catch { /* Storage is optional. */ }
// Enable auxiliary controls only after their handlers have been registered.
setBusy(false);
resizeInput();
