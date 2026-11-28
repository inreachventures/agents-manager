/// `window.wm`, the SDK plugin pages use to talk to the app. PluginWebView injects it at document start, before the
/// page's own scripts, followed by the page's first `wm.context` and theme.
///
/// It lives here as a string, not as a bundled wm.js: a SwiftPM resource needs its own bundle beside the app, which
/// build-app.sh would have to copy and sign, and which a missing copy turns into a crash at launch.
///
/// Protocol (the page side of PluginWebView.Coordinator):
/// - page → app: `webkit.messageHandlers.wm.postMessage({id, call, args})`, `args` as JSON text, `call` one of
///   request, send, openDiff, panel, reveal, show, toast.
/// - app → page: `__wm.resolve(id, value)` / `__wm.reject(id, message)` settle the call's promise;
///   `__wm.emit(event, data)` calls the page's `wm.on(event, fn)` handlers; `__wm.setContext(context)` replaces
///   `wm.context` (before "workstream" is emitted); `__wm.setTheme(vars, theme)` sets the `--wm-*` CSS variables.
enum PluginSDK {
    static let source = #"""
(() => {
  "use strict";
  if (window.wm) return;

  const pending = new Map();  // call id -> {resolve, reject}
  const handlers = new Map();  // event -> Set of functions
  let nextID = 1;
  let context = Object.freeze({});
  let themeVars = {};

  function call(name, args) {
    return new Promise((resolve, reject) => {
      const id = nextID++;
      let text;
      try {
        text = JSON.stringify(args);
      } catch (e) {
        reject(new Error(`wm.${name}: the arguments aren't JSON (${e.message})`));
        return;
      }
      pending.set(id, { resolve, reject });
      try {
        window.webkit.messageHandlers.wm.postMessage({ id, call: name, args: text });
      } catch (e) {
        pending.delete(id);
        reject(new Error("wm isn't available on this page"));
      }
    });
  }

  function applyTheme() {
    const root = document.documentElement;
    if (!root) return false;
    for (const [name, value] of Object.entries(themeVars)) root.style.setProperty(name, value);
    root.style.colorScheme = context.theme || "";
    return true;
  }

  // At document start there may be no <html> element yet: set the variables as soon as there is one.
  function applyThemeSoon() {
    if (applyTheme()) return;
    const watch = new MutationObserver(() => { if (applyTheme()) watch.disconnect(); });
    watch.observe(document, { childList: true });
  }

  const host = {
    resolve(id, value) {
      const p = pending.get(id);
      if (!p) return;
      pending.delete(id);
      p.resolve(value);
    },
    reject(id, message) {
      const p = pending.get(id);
      if (!p) return;
      pending.delete(id);
      p.reject(new Error(message));
    },
    emit(event, data) {
      for (const fn of [...(handlers.get(event) || [])]) {
        try {
          fn(data);
        } catch (e) {
          console.error(`wm.on("${event}") handler failed`, e);
        }
      }
    },
    setContext(next) {
      context = Object.freeze({ ...next });
      applyTheme();
    },
    setTheme(vars, theme) {
      themeVars = { ...vars };
      if (theme) context = Object.freeze({ ...context, theme });
      applyThemeSoon();
    },
  };
  Object.defineProperty(window, "__wm", { value: Object.freeze(host) });

  const events = new Set(["workstream", "state", "changed", "theme", "repo"]);

  const wm = {
    /** {plugin, view, key, folder, repos: [{repo, branch, base, worktree}], repo, theme} */
    get context() { return context; },

    /** Call this plugin's own `api.request(method, fn)` handler with `params`; resolves to what it returns. */
    request(method, params) {
      return call("request", { method: String(method), params: params === undefined ? {} : params });
    },

    /** Type `text` into the selected workstream's Claude session, then Enter. */
    send(text) { return call("send", { text: String(text) }); },

    /** Open the side panel on Changes, narrowed to `repo` and scrolled to `path` (relative to the repo). */
    openDiff(repo, path, line) {
      return call("openDiff", { repo: String(repo), path: String(path), line: line == null ? null : Number(line) });
    },

    /** Show (true) or hide (false) the side panel. */
    panel(open) { return call("panel", { open: !!open }); },

    /** Reveal a file in Finder: only inside the workstream's folder; a relative path is taken from there. */
    reveal(path) { return call("reveal", { path: String(path) }); },

    /** Switch to another view of this plugin by its id, or to "terminal" or "changes". */
    show(view) { return call("show", { view: String(view) }); },

    /** The app's toast; `level` is "info", "warning" or "error". */
    toast(message, level) { return call("toast", { message: String(message), level: level || "info" }); },

    /** Listen for "workstream", "state", "changed", "theme" or "repo". Returns a function that stops listening. */
    on(event, fn) {
      if (!events.has(event)) throw new Error(`wm.on: no event "${event}"`);
      if (typeof fn !== "function") throw new Error("wm.on: the handler must be a function");
      if (!handlers.has(event)) handlers.set(event, new Set());
      handlers.get(event).add(fn);
      return () => wm.off(event, fn);
    },

    off(event, fn) {
      const set = handlers.get(event);
      if (set) set.delete(fn);
    },
  };
  Object.defineProperty(window, "wm", { value: Object.freeze(wm) });
})();
"""#
}
