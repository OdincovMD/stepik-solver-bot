// Defaults for the bundled llama.cpp b12359 UI. Existing owner choices win.
// Tool schemas otherwise consume a substantial part of the 2048-token window.
try {
  const key = 'LlamaUi.disabledToolCategories';
  if (localStorage.getItem(key) === null) {
    localStorage.setItem(key, JSON.stringify(['browser', 'server', 'mcp', 'custom']));
  }
} catch (_) {
  // Chat remains usable when a browser does not allow preference storage.
}
