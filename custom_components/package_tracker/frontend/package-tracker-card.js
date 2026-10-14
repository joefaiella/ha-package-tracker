/*
 * Package tracker card: count tiles that filter the package list, and rows that open each
 * package's tracking page. Ships with the Package tracker integration.
 *
 *   type: custom:package-tracker-card
 *   mail_url: https://informeddelivery.usps.com/   # optional: where "Mail today" goes
 *
 * Plain web component, no build step. Every string from an email is escaped before it reaches
 * the page, and only https links are used.
 */
const TAG = "package-tracker-card";
const DEFAULT_MAIL_URL = "https://informeddelivery.usps.com/";
const TILES = [
  {
    id: "arriving_today",
    name: "Arriving today",
    icon: "mdi:truck-delivery",
    color: "amber",
    empty: "Nothing is arriving today",
  },
  {
    id: "on_the_way",
    name: "On the way",
    icon: "mdi:package-variant",
    color: "blue",
    empty: "Nothing is on the way",
  },
  {
    id: "delivered_today",
    name: "Delivered today",
    icon: "mdi:package-variant-closed-check",
    color: "green",
    empty: "Nothing was delivered today",
  },
];
const MAIL_TILE = { id: "mail", name: "Mail today", icon: "mdi:mailbox-open-up", color: "indigo" };
const DAY = 86400000;
const LOGGER = "custom_components.package_tracker.card";

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

const safeUrl = (u) => {
  try {
    const url = new URL(u);
    return url.protocol === "https:" ? url.href : null;
  } catch (_err) {
    return null;
  }
};

// Write to Home Assistant's log, so a problem in someone's browser shows up where it can be read.
const report = (hass, level, message) => {
  try {
    hass
      ?.callWS?.({
        type: "system_log/write",
        logger: LOGGER,
        level,
        message: String(message).slice(0, 2000),
      })
      ?.catch?.(() => {});
  } catch (_err) {
    // nothing else to do
  }
};

const rootHass = () => document.querySelector("home-assistant")?.hass;

// "2026-10-09..." -> midnight UTC of that day in ms, or null.
const isoDay = (s) => {
  if (typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(s)) return null;
  return Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
};

class PackageTrackerCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._filter = null;
    this._error = null;
    this.shadowRoot.addEventListener("click", (ev) => {
      const tile = ev.target.closest("button[data-filter]");
      if (!tile) return; // rows and the mail tile are plain links
      this._filter = this._filter === tile.dataset.filter ? null : tile.dataset.filter;
      this._render();
    });
    this.shadowRoot.addEventListener("change", (ev) => this._onTick(ev));
  }

  setConfig(config) {
    if (config.mail_url && !safeUrl(config.mail_url)) {
      throw new Error("mail_url must be an https:// link");
    }
    this._config = { ...config };
    this._guard("setConfig", () => this._render());
  }

  set hass(hass) {
    this._guard("hass", () => {
      const connectionChanged = this._hass?.connection !== hass?.connection;
      this._hass = hass;
      if (connectionChanged) this._watchConnection();
      // Also retries after a failed attempt.
      if (this.isConnected && !this._unsub) this._subscribe();
      if (this._fatal && !this._fatalReported) {
        this._fatalReported = true;
        report(hass, "error", `card failed in ${this._fatal.where}: ${this._fatal.message}`);
      }
    });
  }

  get hass() {
    return this._hass;
  }

  // Run fn; on an exception, log it to Home Assistant and show it in the card.
  _guard(where, fn) {
    try {
      return fn();
    } catch (err) {
      this._fatal = { where, message: `${err?.message ?? err}\n${err?.stack ?? ""}` };
      if (this._hass) {
        this._fatalReported = true;
        report(this._hass, "error", `card failed in ${where}: ${this._fatal.message}`);
      }
      try {
        const style =
          "padding:12px;color:var(--error-color,#db4437);white-space:pre-wrap;font:12px monospace";
        this.shadowRoot.innerHTML =
          `<div style="${style}">Package tracker card error (${esc(where)}):\n` +
          `${esc(this._fatal.message)}</div>`;
      } catch (_err) {
        // the shadow root itself is broken; HA will show its own error
      }
      return undefined;
    }
  }

  connectedCallback() {
    this._guard("connected", () => {
      if (this._hass) {
        this._watchConnection();
        this._subscribe();
      }
      this._render();
    });
  }

  disconnectedCallback() {
    this._unwatch?.();
    this._unwatch = undefined;
    clearTimeout(this._retry);
    this._retry = undefined;
    this._drop();
  }

  getCardSize() {
    return 3 + (this._data?.packages?.length ?? 1);
  }

  getGridOptions() {
    return { columns: "full" };
  }

  // After Home Assistant restarts, the websocket reconnects ("ready"); subscribe again then.
  _watchConnection() {
    this._unwatch?.();
    this._unwatch = undefined;
    const conn = this._hass?.connection;
    if (!conn?.addEventListener) return;
    const onReady = () => {
      this._drop();
      this._failures = 0;
      if (this.isConnected) this._subscribe();
    };
    conn.addEventListener("ready", onReady);
    this._unwatch = () => conn.removeEventListener?.("ready", onReady);
  }

  _drop() {
    const unsub = this._unsub;
    this._unsub = undefined;
    unsub?.then((off) => off()).catch(() => {});
  }

  _subscribe() {
    if (this._unsub || this._retry || !this._hass?.connection) return;
    const attempt = this._hass.connection.subscribeMessage(
      (data) => {
        this._data = data;
        this._error = null;
        this._failures = 0;
        this._guard("render", () => this._render());
      },
      { type: "package_tracker/subscribe" },
      { resubscribe: false },
    );
    this._unsub = attempt;
    attempt.catch((err) => {
      if (this._unsub !== attempt) return; // a newer subscription took over
      const reason = `${err?.code ?? ""} ${err?.message ?? err}`;
      report(this._hass, "warning", `card could not subscribe: ${reason}`);
      this._unsub = undefined;
      // "unknown_command" means the integration isn't loaded yet (HA starting): keep trying.
      this._failures = (this._failures ?? 0) + 1;
      const starting = err?.code === "unknown_command";
      // While starting, keep showing what we had.
      this._error = starting ? null : (err?.message ?? String(err));
      if (this.isConnected) {
        const wait = Math.min(60000, 2000 * 2 ** Math.min(this._failures, 5));
        this._retry = setTimeout(() => {
          this._retry = undefined;
          this._subscribe();
        }, wait);
      }
      this._render();
    });
  }

  async _onTick(ev) {
    const box = ev.target.closest('input[type="checkbox"][data-id]');
    if (!box) return;
    const haptic = new Event("haptic", { bubbles: true, composed: true });
    this.dispatchEvent(Object.assign(haptic, { detail: "light" }));
    try {
      await this._hass.callWS({
        type: "package_tracker/set_received",
        package: box.dataset.id,
        received: box.checked,
      });
    } catch (err) {
      box.checked = !box.checked;
      const toast = new Event("hass-notification", { bubbles: true, composed: true });
      const message = `Couldn't update that package: ${err?.message ?? err}`;
      this.dispatchEvent(Object.assign(toast, { detail: { message } }));
    }
  }

  // ---- formatting ------------------------------------------------------
  _dayLabel(iso) {
    const day = isoDay(iso);
    const today = isoDay(this._data?.as_of);
    if (day === null || today === null) return "";
    const diff = Math.round((day - today) / DAY);
    if (diff === 0) return "Today";
    if (diff === 1) return "Tomorrow";
    if (diff === -1) return "Yesterday";
    const lang = this._hass?.locale?.language || this._hass?.language || "en";
    const date = new Date(day);
    if (diff > 1 && diff < 7) {
      return date.toLocaleDateString(lang, { weekday: "short", timeZone: "UTC" });
    }
    return date.toLocaleDateString(lang, { month: "short", day: "numeric", timeZone: "UTC" });
  }

  _when(p) {
    if (p.done) {
      if (p.ticked) return "Got it";
      if (!p.delivered_at) return "Delivered";
      const locale = this._hass?.locale ?? {};
      const lang = locale.language || this._hass?.language || "en";
      const opts = { hour: "numeric", minute: "2-digit" };
      if (locale.time_format === "24") opts.hourCycle = "h23";
      if (locale.time_format === "12") opts.hourCycle = "h12";
      if (locale.time_zone !== "local" && this._hass?.config?.time_zone) {
        opts.timeZone = this._hass.config.time_zone;
      }
      try {
        const time = new Date(p.delivered_at).toLocaleTimeString(lang, opts);
        return `Delivered ${time}`;
      } catch (_err) {
        return "Delivered";
      }
    }
    if (!p.expected) return "";
    const start = this._dayLabel(p.expected);
    const hasEnd = p.expected_end && p.expected_end !== p.expected;
    const end = hasEnd ? this._dayLabel(p.expected_end) : "";
    return end ? `${start} – ${end}` : start;
  }

  // ---- rendering -------------------------------------------------------
  _rows() {
    const all = this._data?.packages ?? [];
    if (!this._filter) return all.filter((p) => !p.done);
    return all.filter((p) => p.groups?.includes(this._filter));
  }

  _tile(t, count, pressed) {
    const title = pressed ? "Show all packages" : `Show only: ${esc(t.name)}`;
    return `<button type="button" class="tile" data-filter="${t.id}" aria-pressed="${pressed}"
        style="--tile-color: var(--${t.color}-color)" title="${title}">
        <span class="icon"><ha-icon icon="${t.icon}"></ha-icon></span>
        <span class="info">
          <span class="name">${esc(t.name)}</span><span class="state">${esc(count)}</span>
        </span>
      </button>`;
  }

  _mailTile(count) {
    const url = safeUrl(this._config?.mail_url || DEFAULT_MAIL_URL);
    return `<a class="tile" href="${esc(url)}" target="_blank" rel="noopener noreferrer"
        style="--tile-color: var(--${MAIL_TILE.color}-color)" title="Open USPS Informed Delivery">
        <span class="icon"><ha-icon icon="${MAIL_TILE.icon}"></ha-icon></span>
        <span class="info">
          <span class="name">${esc(MAIL_TILE.name)}</span><span class="state">${esc(count)}</span>
        </span>
      </a>`;
  }

  _row(p) {
    const url = safeUrl(p.url);
    const mail = safeUrl(p.email_url);
    const status = p.ticked ? "ticked off" : p.status;
    const sub = [p.carrier, status, p.done ? null : p.window, p.exception]
      .filter(Boolean)
      .map(esc)
      .join(" · ");
    const when = this._when(p);
    const late = p.late && !p.done;
    const whenText = late ? `${when ? `${when} · ` : ""}late` : when;
    const body = `<span class="text">
        <span class="item">${esc(p.item)}</span><span class="sub">${sub}</span>
      </span>
      <span class="when${late ? " late" : ""}">${esc(whenText)}</span>`;
    const link = 'target="_blank" rel="noopener noreferrer"';
    const main = url
      ? `<a class="row-link" href="${esc(url)}" ${link} title="Track this package">${body}</a>`
      : `<span class="row-link plain">${body}</span>`;
    const mailLink = mail
      ? `<a class="mail" href="${esc(mail)}" ${link} title="Open the email"
          aria-label="Open the email about ${esc(p.item)}">
          <ha-icon icon="mdi:email-outline"></ha-icon></a>`
      : "";
    const tickLabel = p.done ? "Not received yet" : "Got it";
    const tick =
      p.can_tick === false
        ? `<span class="delivered" title="Delivered by the carrier" aria-label="Delivered">
            <ha-icon icon="mdi:check"></ha-icon></span>`
        : `<input type="checkbox" data-id="${esc(p.id)}" ${p.done ? "checked" : ""}
            aria-label="${tickLabel}: ${esc(p.item)}" title="${tickLabel}">`;
    return `<li class="${p.done ? "done" : ""}">${tick}${main}${mailLink}</li>`;
  }

  // A selector for the focused element, so focus survives a re-render.
  _focusKey() {
    const el = this.shadowRoot.activeElement;
    if (!el) return null;
    if (el.dataset?.filter) return `button[data-filter="${el.dataset.filter}"]`;
    if (el.dataset?.id) return `input[data-id="${el.dataset.id}"]`;
    const li = el.closest("li");
    if (li) {
      const id = li.querySelector("input[data-id]")?.dataset.id;
      const part = el.classList.contains("mail") ? "a.mail" : ".row-link";
      if (id) return `li:has(input[data-id="${id}"]) ${part}`;
    }
    return el.classList.contains("tile") ? "a.tile" : null;
  }

  _render() {
    const root = this.shadowRoot;
    const d = this._data;
    const refocus = this._focusKey();
    let content;
    if (this._error) {
      const error = esc(this._error);
      content = `<div class="message">The Package tracker card can't load: ${error}</div>`;
    } else if (!d) {
      const waiting = this._failures
        ? "Waiting for Package tracker to start…"
        : "Loading packages…";
      content = `<div class="message">${waiting}</div>`;
    } else if (!d.loaded) {
      content =
        '<div class="message">Package tracker isn\'t running. ' +
        "Check Settings → Devices &amp; services.</div>";
    } else {
      const counts = d.counts ?? {};
      const tiles = TILES.map((t) => this._tile(t, counts[t.id] ?? 0, this._filter === t.id));
      const mail = this._mailTile(d.mail_today ?? "–");
      const rows = this._rows();
      const empty = this._filter
        ? TILES.find((t) => t.id === this._filter)?.empty
        : "No packages on the way";
      const list = rows.length
        ? `<ul>${rows.map((p) => this._row(p)).join("")}</ul>`
        : `<div class="message">${esc(empty)}</div>`;
      const problem = d.last_error
        ? `<div class="warning">Couldn't check Gmail: ${esc(d.last_error)}</div>`
        : "";
      content = `<div class="tiles">${tiles.join("")}${mail}</div>${problem}${list}`;
    }
    root.innerHTML = `<style>${STYLE}</style><ha-card>${content}</ha-card>`;
    if (refocus) {
      try {
        root.querySelector(refocus)?.focus({ preventScroll: true });
      } catch (_err) {
        // selector not supported (very old browser): focus simply isn't restored
      }
    }
  }
}

const STYLE = `
  :host { display: block; }
  ha-card { display: block; overflow: hidden; container-type: inline-size; }
  ha-card:not(:defined) {
    background: var(--ha-card-background, var(--card-background-color, #fff));
    border: var(--ha-card-border-width, 1px) solid
      var(--ha-card-border-color, var(--divider-color, #e0e0e0));
    border-radius: var(--ha-card-border-radius, var(--ha-border-radius-lg, 12px));
    color: var(--primary-text-color);
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--ha-space-2, 8px);
    padding: 12px;
  }
  .tile {
    all: unset;
    box-sizing: border-box;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 10px var(--ha-space-2, 8px);
    min-height: 104px;
    min-width: 0;
    text-align: center;
    -webkit-tap-highlight-color: transparent;
    border: var(--ha-card-border-width, 1px) solid
      var(--ha-card-border-color, var(--divider-color, #e0e0e0));
    border-radius: var(--ha-card-border-radius, var(--ha-border-radius-lg, 12px));
    transition: box-shadow 180ms ease-in-out, border-color 180ms ease-in-out,
      background-color 180ms;
  }
  .tile:hover { background: color-mix(in srgb, var(--tile-color) 4%, transparent); }
  .tile:active { background: color-mix(in srgb, var(--tile-color) 12%, transparent); }
  .tile:focus-visible, .tile[aria-pressed="true"] {
    border-color: var(--tile-color);
    box-shadow: 0 0 0 1px var(--tile-color);
  }
  .tile[aria-pressed="true"] { background: color-mix(in srgb, var(--tile-color) 10%, transparent); }
  .icon {
    position: relative;
    flex: none;
    width: 36px;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--ha-tile-icon-border-radius, var(--ha-border-radius-pill, 9999px));
    overflow: hidden;
    color: var(--tile-color);
    --mdc-icon-size: 24px;
  }
  .icon::before {
    content: "";
    position: absolute;
    inset: 0;
    background: var(--tile-color);
    opacity: .2;
    transition: opacity 180ms;
  }
  .tile:hover .icon::before { opacity: .35; }
  .icon ha-icon { position: relative; display: flex; }
  .info {
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 0;
    max-width: 100%;
  }
  .name, .state { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
  .state { white-space: nowrap; }
  .name {
    font-size: var(--ha-font-size-m, 14px);
    font-weight: var(--ha-font-weight-medium, 500);
    line-height: 1.3;
    letter-spacing: .1px;
    color: var(--primary-text-color);
    overflow-wrap: normal;
    /* narrow: wrap, don't cut */
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }
  .state {
    font-size: var(--ha-font-size-s, 12px);
    line-height: var(--ha-line-height-condensed, 1.2);
    letter-spacing: .4px;
    color: var(--primary-text-color);
  }
  ul { list-style: none; margin: 0; padding: 0 12px 4px; }
  li {
    display: flex;
    align-items: center;
    gap: 4px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  li.done .item { color: var(--secondary-text-color); }
  .delivered {
    flex: none;
    display: flex;
    width: 20px;
    margin: 10px 8px 10px 4px;
    color: var(--success-color, #43a047);
    --mdc-icon-size: 20px;
  }
  input[type="checkbox"] {
    flex: none;
    width: 20px;
    height: 20px;
    margin: 10px 8px 10px 4px;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
  .row-link {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 6px;
    color: inherit;
    text-decoration: none;
    border-radius: 8px;
  }
  a.row-link:hover { background: color-mix(in srgb, var(--primary-color) 6%, transparent); }
  a.row-link:focus-visible, a.mail:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }
  .text { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .item, .sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .item, .sub { line-height: 1.4; }
  .item { font-size: var(--ha-font-size-m, 14px); color: var(--primary-text-color); }
  .sub { font-size: var(--ha-font-size-s, 12px); color: var(--secondary-text-color); }
  .when {
    flex: none;
    font-size: var(--ha-font-size-s, 12px);
    color: var(--secondary-text-color);
    white-space: nowrap;
  }
  .when.late { color: var(--error-color, #db4437); }
  a.mail {
    flex: none;
    display: flex;
    padding: 8px;
    color: var(--secondary-text-color);
    border-radius: 50%;
    --mdc-icon-size: 20px;
  }
  a.mail:hover {
    color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 8%, transparent);
  }
  .message {
    padding: 4px 16px 16px;
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-m, 14px);
  }
  .tiles + .message { padding-top: 4px; }
  .warning {
    margin: 0 12px 8px;
    padding: 6px 10px;
    border-radius: 8px;
    font-size: var(--ha-font-size-s, 12px);
    color: var(--warning-color, #ffa600);
    background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent);
  }
  ha-icon:not(:defined) { display: none; }
  /* Narrow (phones): smaller tile text so whole words fit. Must stay last to win. */
  @container (max-width: 420px) {
    .tiles { gap: 6px; padding: 10px; }
    .tile { padding: 8px 2px; gap: 8px; }
    .name { font-size: var(--ha-font-size-s, 12px); }
  }
`;

// Home Assistant swaps window.customElements for a scoped-registry polyfill when its app starts
// (in browsers without native support, e.g. Firefox). This file usually loads first, and an
// element defined in the original registry is invisible to dashboards ("Configuration error").
// So register once <home-assistant> is defined, in whichever registry is current then, or after
// a few seconds anyway for pages without the app (e.g. a cast device).
const register = () => {
  try {
    if (!window.customElements.get(TAG)) window.customElements.define(TAG, PackageTrackerCard);
    if (!(window.customCards || []).some((c) => c.type === TAG)) {
      window.customCards = window.customCards || [];
      window.customCards.push({
        type: TAG,
        name: "Package tracker",
        description:
          "Packages from your shipping emails: tap a tile to filter, tap a package to track it.",
        preview: false,
      });
    }
  } catch (err) {
    console.error("Package tracker card could not register", err);
    const details = `${err?.message ?? err}\n${err?.stack ?? ""}`;
    report(rootHass(), "error", `card could not register: ${details}`);
  }
};
Promise.race([
  window.customElements.whenDefined("home-assistant"),
  new Promise((resolve) => setTimeout(resolve, 5000)),
]).then(register, register);
