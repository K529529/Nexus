/* Regression harness for the V0.2.1 create/delete UI.
 *
 * Runs the REAL web/app.js inside a minimal DOM + fetch stub so a Python test
 * can verify:
 *   - creation no longer uses window.prompt but an in-app dialog,
 *   - the exact typed name is submitted (no extension is added),
 *   - validation blocks submission and the server stays authoritative,
 *   - delete requires an explicit confirmation and posts /api/delete,
 *   - failures keep the dialog open and never report success,
 *   - the row delete button does not fall through to opening the file.
 *
 * Usage:  node create_delete_harness.js <path/to/app.js>   ("-" = stdin)
 * Prints one JSON object of per-scenario observations.
 */
const fs = require("fs");
const vm = require("vm");

const APP_JS = process.argv[2];
const SANDBOX = "D:\\WorkSpace\\FilePilot\\Sandbox";
const OUTSIDE = "D:\\WorkSpace";

const FILE_ENTRY = {
  name: "a.txt", kind: "file", path: SANDBOX + "\\a.txt",
  size: 12, modified: 100, is_dir: false, is_symlink: false,
};
const DIR_ENTRY = {
  name: "docs", kind: "dir", path: SANDBOX + "\\docs",
  size: 0, modified: 100, is_dir: true, is_symlink: false,
};
const CONFIG = {
  start_path: SANDBOX,
  roots: [SANDBOX],
  sort_keys: ["name", "type", "size", "modified"],
  max_read_bytes: 4096,
  max_write_bytes: 4096,
};

// Ids that index.html (or the editor markup built by app.js) already provides.
// Any other id must be absent until app.js creates that element -- real
// getElementById returns null, and addTopbarButton relies on that check.
const STATIC_IDS = new Set([
  "app", "breadcrumbs", "count", "drives", "empty", "mode-badge", "refresh",
  "rows", "search", "status", "table",
  "modal-body", "modal-cancel", "modal-close", "modal-save", "modal-status",
  "modal-title", "editor", "toast",
]);

function makeEl(tag, id, dom) {
  const node = {
    tagName: (tag || "div").toUpperCase(),
    _id: id || "",
    _on: {},
    _parent: null,
    attrs: {},
    children: [],
    style: {},
    dataset: {},
    value: "",
    innerHTML: "",
    hidden: false,
    disabled: false,
    readOnly: false,
    className: "",
    type: "",
    title: "",
    placeholder: "",
    spellcheck: true,
    autocomplete: "",
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c) { return this._s.has(c) ? this._s.delete(c) : this._s.add(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener(type, fn) { (this._on[type] || (this._on[type] = [])).push(fn); },
    appendChild(n) { n._parent = this; this.children.push(n); return n; },
    append() { for (const n of arguments) this.appendChild(n); },
    insertBefore(n, before) {
      n._parent = this;
      const i = this.children.indexOf(before);
      if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
      return n;
    },
    removeChild(n) {
      const i = this.children.indexOf(n);
      if (i >= 0) this.children.splice(i, 1);
      return n;
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    focus() { if (dom) dom.focused = this; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(node, "firstChild", { get() { return this.children[0] || null; } });
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = v === null || v === undefined ? "" : String(v); this.children.length = 0; },
  });
  Object.defineProperty(node, "id", {
    get() { return this._id; },
    set(v) {
      this._id = v;
      if (!dom || !v) return;
      dom.registry[v] = this;
      if (v === "modal") dom.modalHolder.current = this;
    },
  });
  return node;
}

function newDom() {
  const registry = {};
  const modalHolder = { current: null };
  const topbar = makeEl("div", "topbar-actions", null);
  const dom = {
    registry, modalHolder, topbar,
    listLog: [],       // /api/list paths, in order
    postLog: [],       // {url, body} for every create/delete POST
    viewerCalls: 0,    // /api/file?path= requests (the editor)
    promptCalls: [],   // window.prompt invocations
    focused: null,
  };
  const body = makeEl("body", "body", dom);
  dom.doc = {
    body,
    _on: {},
    addEventListener(type, fn) { (this._on[type] || (this._on[type] = [])).push(fn); },
    getElementById(id) {
      if (id === "modal") return modalHolder.current;
      if (!registry[id] && STATIC_IDS.has(id)) registry[id] = makeEl("div", id, dom);
      return registry[id] || null;
    },
    createElement(tag) { return makeEl(tag, "", dom); },
    createDocumentFragment() { return makeEl("#fragment", "", dom); },
    querySelector(sel) { return sel === ".topbar-actions" ? topbar : null; },
    querySelectorAll() { return []; },
  };
  return dom;
}

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

function route(dom, opts, input, init) {
  const url = String(input);
  if (url.startsWith("/api/config")) return Promise.resolve(response(200, CONFIG));
  if (url.startsWith("/api/list")) {
    const q = new URLSearchParams(url.split("?")[1] || "");
    const p = q.get("path") || "";
    dom.listLog.push(p);
    const entries = opts.entries || [FILE_ENTRY];
    const total = p === SANDBOX ? entries.length : (opts.childTotal || 0);
    return Promise.resolve(response(200, {
      path: p, parent: OUTSIDE,
      breadcrumbs: [{ label: "Sandbox", path: SANDBOX }],
      total, matched: total,
      entries: p === SANDBOX ? entries : [],
    }));
  }
  if (url.startsWith("/api/file/create") || url.startsWith("/api/dir/create")) {
    const body = JSON.parse((init && init.body) || "{}");
    dom.postLog.push({ url, body });
    if (opts.create) return Promise.resolve(response(opts.create.status, opts.create.body));
    return Promise.resolve(response(200, { path: SANDBOX + "\\" + body.name }));
  }
  if (url.startsWith("/api/delete")) {
    const body = JSON.parse((init && init.body) || "{}");
    dom.postLog.push({ url, body });
    if (opts.del) return Promise.resolve(response(opts.del.status, opts.del.body));
    return Promise.resolve(response(200, { path: body.path }));
  }
  if (url.startsWith("/api/file/save")) {
    return Promise.resolve(response(200, { path: SANDBOX + "\\a.txt", modified_ns: "2", size: 5 }));
  }
  if (url.startsWith("/api/file?")) {
    dom.viewerCalls += 1;
    return Promise.resolve(response(200, {
      path: SANDBOX + "\\a.txt", content: "hello", modified_ns: "1", writable: true,
    }));
  }
  return Promise.resolve(response(404, {}));
}

async function boot(src, opts = {}) {
  const dom = newDom();
  const context = {
    document: dom.doc,
    window: {
      confirm: () => true,
      prompt: (msg) => { dom.promptCalls.push(String(msg)); return opts.promptAnswer || ""; },
      addEventListener() {},
    },
    fetch: (u, o) => route(dom, opts, u, o),
    URLSearchParams, encodeURIComponent, console, Date, Math, JSON,
    setTimeout: (fn, ms) => {
      const t = setTimeout(fn, ms);
      if (t && t.unref) t.unref(); // toasts must not keep node alive
      return t;
    },
    clearTimeout,
  };
  vm.createContext(context);
  vm.runInContext(src, context, { filename: "app.js" });
  await flush();
  return dom;
}

const flush = () => new Promise((r) => setImmediate(() => setImmediate(() => setImmediate(r))));

// Faithful-enough event dispatch: handlers run on the target and every
// ancestor unless stopPropagation() was called (the delete button relies on it).
function fireEvent(node, type, extra) {
  const event = Object.assign({
    target: node,
    key: "",
    ctrlKey: false,
    defaultPrevented: false,
    _stopped: false,
    stopPropagation() { this._stopped = true; },
    preventDefault() { this.defaultPrevented = true; },
  }, extra || {});
  let cur = node;
  while (cur) {
    for (const fn of (cur._on[type] || [])) fn(event);
    if (event._stopped) break;
    cur = cur._parent;
  }
  return event;
}

const byId = (dom, id) => dom.registry[id];
const text = (dom, id) => {
  const n = byId(dom, id);
  return n ? n.textContent : null;
};
const isOpen = (dom) => {
  const d = byId(dom, "fp-dialog");
  return !!(d && d.classList.contains("open"));
};
const posts = (dom) => dom.postLog.map((p) => ({ url: p.url, body: p.body }));

function findRow(dom, name) {
  const rows = byId(dom, "rows");
  if (!rows) return null;
  for (const frag of rows.children) {
    for (const tr of frag.children || []) {
      const nm = tr.children[0] && tr.children[0].children[1];
      if (nm && nm.textContent === name) return tr;
    }
  }
  return null;
}

function actionButton(dom, name) {
  const tr = findRow(dom, name);
  if (!tr) return null;
  const cell = tr.children[4];
  return cell && cell.children[0] ? cell.children[0] : null;
}

async function clickCreate(dom, kind) {
  const btn = byId(dom, kind === "folder" ? "new-folder" : "new-file");
  if (!btn) return false; // e.g. an older app.js without the topbar actions
  fireEvent(btn, "click");
  await flush();
  return true;
}

async function typeName(dom, value) {
  const input = byId(dom, "fp-dialog-input");
  if (!input) return false; // no dialog in this app.js version
  input.value = value;
  fireEvent(input, "input");
  await flush();
  return true;
}

const clickConfirm = async (dom) => {
  const btn = byId(dom, "fp-dialog-confirm");
  if (!btn) return false;
  fireEvent(btn, "click");
  await flush();
  return true;
};
const clickCancel = async (dom) => {
  const btn = byId(dom, "fp-dialog-cancel");
  if (!btn) return false;
  fireEvent(btn, "click");
  await flush();
  return true;
};

async function openDelete(dom, name) {
  const btn = actionButton(dom, name);
  if (btn) { fireEvent(btn, "click"); await flush(); }
  return btn;
}

async function main() {
  const src = APP_JS === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(APP_JS, "utf8");
  const S = {};

  { // No dialog DOM exists until the user asks for one.
    const d = await boot(src);
    S.initial_state = {
      prompt_calls: d.promptCalls.length,
      dialog_absent: !byId(d, "fp-dialog"),
    };
  }

  { // New file opens an in-app dialog instead of window.prompt.
    const d = await boot(src);
    await clickCreate(d, "file");
    const input = byId(d, "fp-dialog-input");
    S.new_file_dialog = {
      dialog_open: isOpen(d),
      prompt_calls: d.promptCalls.length,
      title: text(d, "fp-dialog-title"),
      sub: text(d, "fp-dialog-sub"),
      badge: text(d, "fp-dialog-badge"),
      hint: text(d, "fp-dialog-hint"),
      confirm_disabled: !!(byId(d, "fp-dialog-confirm") || {}).disabled,
      confirm_label: text(d, "fp-dialog-confirm"),
      input_tag: input ? input.tagName : null,
      placeholder: input ? input.placeholder : null,
      chips: byId(d, "fp-dialog-chips") ? byId(d, "fp-dialog-chips").children.map((c) => c.textContent) : [],
      posts: posts(d),
    };
  }

  { // Exact typed name, including its extension, goes to the server.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "notes.md");
    const badge = text(d, "fp-dialog-badge");
    const hint = text(d, "fp-dialog-hint");
    await clickConfirm(d);
    S.create_file_exact_name = {
      badge, hint,
      posts: posts(d),
      dialog_open: isOpen(d),
      list_requests: d.listLog.length,
      toast: text(d, "toast"),
      viewer_calls: d.viewerCalls,
    };
  }

  { // A name without any extension is created as typed (no ".txt" appended).
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "README");
    const badge = text(d, "fp-dialog-badge");
    const hint = text(d, "fp-dialog-hint");
    await clickConfirm(d);
    S.create_file_no_extension = { badge, hint, posts: posts(d), dialog_open: isOpen(d) };
  }

  { // Unknown extensions are allowed, with an honest "text only" note.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "archive.zip");
    const badge = text(d, "fp-dialog-badge");
    const hint = text(d, "fp-dialog-hint");
    await clickConfirm(d);
    S.create_non_text_extension = { badge, hint, posts: posts(d) };
  }

  { // Enter submits, like the confirm button.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "todo.md");
    fireEvent(byId(d, "fp-dialog-input"), "keydown", { key: "Enter" });
    await flush();
    S.create_with_enter = { posts: posts(d), dialog_open: isOpen(d) };
  }

  { // Folder creation sends no file content field.
    const d = await boot(src);
    await clickCreate(d, "folder");
    const title = text(d, "fp-dialog-title");
    await typeName(d, "Docs");
    await clickConfirm(d);
    S.create_folder = { title, posts: posts(d), dialog_open: isOpen(d) };
  }

  { // A duplicate keeps the dialog and the typed value: no silent rename.
    const dup = await boot(src, {
      create: { status: 409, body: { error: "already_exists", message: "Entry already exists" } },
    });
    await clickCreate(dup, "file");
    await typeName(dup, "notes.md");
    await clickConfirm(dup);
    S.create_duplicate = {
      posts: posts(dup),
      dialog_open: isOpen(dup),
      input_value: (byId(dup, "fp-dialog-input") || {}).value || null,
      error: text(dup, "fp-dialog-error"),
      list_requests: dup.listLog.length,
      toast: text(dup, "toast"),
    };
  }

  { // Client-side validation blocks the request entirely.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "bad/name");
    await clickConfirm(d);
    S.create_invalid_name = {
      posts: posts(d),
      dialog_open: isOpen(d),
      error: text(d, "fp-dialog-error"),
      confirm_disabled: !!(byId(d, "fp-dialog-confirm") || {}).disabled,
    };
  }

  { // Windows device names are rejected before hitting the server.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "CON");
    await clickConfirm(d);
    S.create_reserved_name = {
      posts: posts(d),
      dialog_open: isOpen(d),
      error: text(d, "fp-dialog-error"),
    };
  }

  { // Surrounding whitespace is trimmed, and the UI previews that name.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, " notes.md ");
    const hint = text(d, "fp-dialog-hint");
    await clickConfirm(d);
    S.create_trailing_space = { hint, posts: posts(d) };
  }

  { // Cancel / Escape never send anything.
    const d = await boot(src);
    await clickCreate(d, "file");
    await typeName(d, "x.txt");
    await clickCancel(d);
    S.create_cancel = { posts: posts(d), dialog_open: isOpen(d) };

    const e = await boot(src);
    await clickCreate(e, "file");
    await typeName(e, "x.txt");
    fireEvent(byId(e, "fp-dialog"), "keydown", { key: "Escape" });
    await flush();
    S.create_escape = { posts: posts(e), dialog_open: isOpen(e) };
  }

  { // Delete: the row exposes an action, the dialog asks first, no request yet.
    const d = await boot(src);
    const btn = await openDelete(d, "a.txt");
    S.delete_dialog = {
      has_action_button: !!btn,
      action_title: btn ? btn.title : null,
      action_aria: btn ? btn.getAttribute("aria-label") : null,
      dialog_open: isOpen(d),
      title: text(d, "fp-dialog-title"),
      card_name: text(d, "fp-dialog-card-name"),
      card_meta: text(d, "fp-dialog-card-meta"),
      warning: text(d, "fp-dialog-warning"),
      confirm_label: text(d, "fp-dialog-confirm"),
      posts: posts(d),
      viewer_calls: d.viewerCalls, // must stay 0: the button is not a row click
    };
  }

  { // Confirmative delete posts the exact path and reloads the directory.
    const d = await boot(src);
    await openDelete(d, "a.txt");
    await clickConfirm(d);
    S.delete_confirm = {
      posts: posts(d),
      dialog_open: isOpen(d),
      toast: text(d, "toast"),
      last_list_path: d.listLog[d.listLog.length - 1],
    };
  }

  { // Cancel deletes nothing and does not reload.
    const d = await boot(src);
    const before = d.listLog.length;
    await openDelete(d, "a.txt");
    await clickCancel(d);
    S.delete_cancel = {
      posts: posts(d),
      dialog_open: isOpen(d),
      extra_lists: d.listLog.length - before,
    };
  }

  { // A server-side failure keeps the dialog open with an honest message.
    const d = await boot(src, {
      del: { status: 500, body: { error: "recycle_failed", message: "Shell refused" } },
    });
    await openDelete(d, "a.txt");
    await clickConfirm(d);
    S.delete_failure = {
      posts: posts(d),
      dialog_open: isOpen(d),
      error: text(d, "fp-dialog-error"),
      confirm_label: text(d, "fp-dialog-confirm"),
      toast: text(d, "toast"),
      extra_lists: d.listLog.length - 1,
    };
  }

  { // Deleting a non-empty folder warns with its child count.
    const d = await boot(src, { entries: [FILE_ENTRY, DIR_ENTRY], childTotal: 3 });
    await openDelete(d, "docs");
    S.delete_folder = {
      posts: posts(d),
      dialog_open: isOpen(d),
      card_meta: text(d, "fp-dialog-card-meta"),
      warning: text(d, "fp-dialog-warning"),
    };
  }

  console.log(JSON.stringify(S));
}

main().catch((e) => { console.error(e); process.exit(3); });
