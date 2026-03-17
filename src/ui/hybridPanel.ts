import * as vscode from "vscode";

import { HYBRID_KEYPAD_LABELS, mergeHybridState, type HybridState, type HybridStatus, type SessionState } from "../core/shared";

export type HybridPanelHandlers = {
  onEnable: () => Promise<void>;
  onDisable: () => Promise<void>;
  onSyncFull: () => Promise<void>;
  onSyncFolder: () => Promise<void>;
  onKeyPress: (col: number, row: number) => Promise<void>;
  onDispose: () => void;
};

export class CalSciHybridPanel implements vscode.Disposable {
  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];
  private ready = false;
  private disposed = false;

  private sessionState: SessionState = { connected: false };
  private hybridStatus: HybridStatus = { connected: false, active: false };
  private hybridState: HybridState = {};

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly handlers: HybridPanelHandlers,
  ) {
    this.panel = vscode.window.createWebviewPanel(
      "calsciHybrid",
      "CalSci Hybrid",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      },
    );
    this.panel.webview.html = this.renderHtml();

    this.disposables.push(
      this.panel.onDidDispose(() => {
        if (this.disposed) {
          return;
        }
        this.disposed = true;
        this.handlers.onDispose();
        this.dispose();
      }),
      this.panel.webview.onDidReceiveMessage((message: unknown) => {
        if (!message || typeof message !== "object") {
          return;
        }
        const payload = message as Record<string, unknown>;
        const type = typeof payload.type === "string" ? payload.type : "";
        if (type === "ready") {
          this.ready = true;
          this.postMessage({
            type: "snapshot",
            session: this.sessionState,
            hybridStatus: this.hybridStatus,
            hybridState: this.hybridState,
          });
          return;
        }
        if (type === "toggleHybrid") {
          if (payload.enabled === true) {
            void this.handlers.onEnable();
          } else if (payload.enabled === false) {
            void this.handlers.onDisable();
          }
          return;
        }
        if (type === "syncFull") {
          void this.handlers.onSyncFull();
          return;
        }
        if (type === "syncFolder") {
          void this.handlers.onSyncFolder();
          return;
        }
        if (type === "key") {
          const col = Number(payload.col);
          const row = Number(payload.row);
          if (Number.isInteger(col) && Number.isInteger(row)) {
            void this.handlers.onKeyPress(col, row);
          }
        }
      }),
    );
  }

  public reveal(): void {
    this.panel.reveal(vscode.ViewColumn.Beside, true);
  }

  public updateSnapshot(session: SessionState, hybridStatus: HybridStatus, hybridState: HybridState): void {
    this.sessionState = session;
    this.hybridStatus = hybridStatus;
    this.hybridState = hybridState;
    this.postMessage({
      type: "snapshot",
      session,
      hybridStatus,
      hybridState,
    });
  }

  public updateSessionState(session: SessionState): void {
    this.sessionState = session;
    this.postMessage({ type: "session", session });
  }

  public updateHybridStatus(status: HybridStatus): void {
    this.hybridStatus = status;
    this.postMessage({ type: "hybridStatus", hybridStatus: status });
  }

  public updateHybridState(state: HybridState): void {
    this.hybridState = mergeHybridState(this.hybridState, state);
    this.postMessage({ type: "hybridState", hybridState: state });
  }

  public dispose(): void {
    if (!this.disposed) {
      this.disposed = true;
      try {
        this.panel.dispose();
      } catch {
        // Best effort during shutdown.
      }
    }
    for (const disposable of this.disposables.splice(0)) {
      disposable.dispose();
    }
  }

  private postMessage(message: Record<string, unknown>): void {
    if (!this.ready || this.disposed) {
      return;
    }
    void this.panel.webview.postMessage(message);
  }

  private renderHtml(): string {
    const keypadJson = JSON.stringify(HYBRID_KEYPAD_LABELS);
    const nonce = String(Date.now());
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CalSci Hybrid</title>
  <style>
    :root {
      --ink: #1f2329;
      --muted: #68707a;
      --paper: #f3eee5;
      --paper-2: #e4dccb;
      --panel: rgba(255, 252, 246, 0.9);
      --line: rgba(31, 35, 41, 0.14);
      --accent: #0b6e4f;
      --accent-soft: rgba(11, 110, 79, 0.14);
      --warn: #995700;
      --error: #a1260d;
      --display: #d9ead3;
      --display-ink: #111716;
      --shadow: 0 16px 44px rgba(25, 23, 17, 0.16);
      --radius: 20px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(11, 110, 79, 0.12), transparent 24%),
        radial-gradient(circle at top right, rgba(153, 87, 0, 0.14), transparent 18%),
        linear-gradient(180deg, #fbf8f2 0%, #efe7da 100%);
      font-family: "Trebuchet MS", "Segoe UI", sans-serif;
    }
    .shell {
      max-width: 1100px;
      margin: 0 auto;
      padding: 22px;
      display: grid;
      gap: 18px;
    }
    .hero, .surface {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }
    .hero {
      padding: 20px 22px;
      display: grid;
      gap: 14px;
    }
    .hero-top {
      display: flex;
      gap: 14px;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .title {
      display: grid;
      gap: 4px;
    }
    .title h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.1;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .title p {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }
    .chips {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .chip {
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.75);
    }
    .chip.good {
      color: var(--accent);
      background: var(--accent-soft);
      border-color: rgba(11, 110, 79, 0.18);
    }
    .chip.warn {
      color: var(--warn);
      background: rgba(153, 87, 0, 0.12);
    }
    .chip.bad {
      color: var(--error);
      background: rgba(161, 38, 13, 0.1);
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button.action {
      border: 1px solid rgba(31, 35, 41, 0.14);
      background: white;
      color: var(--ink);
      padding: 10px 16px;
      border-radius: 999px;
      font: inherit;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    button.action.primary {
      background: var(--ink);
      color: white;
    }
    button.action:disabled {
      cursor: default;
      opacity: 0.5;
    }
    .body {
      display: grid;
      grid-template-columns: minmax(300px, 1.15fr) minmax(320px, 1fr);
      gap: 18px;
    }
    .surface {
      padding: 18px;
    }
    .surface h2 {
      margin: 0 0 12px;
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .display-wrap {
      display: grid;
      gap: 12px;
    }
    .display-frame {
      background:
        linear-gradient(180deg, rgba(255,255,255,0.35), rgba(255,255,255,0)),
        var(--display);
      border: 1px solid rgba(17, 23, 22, 0.18);
      border-radius: 24px;
      padding: 16px;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.35);
    }
    canvas {
      width: 100%;
      aspect-ratio: 2 / 1;
      border-radius: 12px;
      image-rendering: pixelated;
      background: #dcead6;
      border: 1px solid rgba(17, 23, 22, 0.14);
      display: block;
    }
    .meta {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .meta strong {
      color: var(--ink);
    }
    .keypad {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }
    .key {
      min-height: 52px;
      border-radius: 16px;
      border: 1px solid rgba(31, 35, 41, 0.14);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.95), rgba(237,232,224,0.92));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);
      color: var(--ink);
      font: inherit;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.02em;
      cursor: pointer;
      transition: transform 90ms ease, box-shadow 90ms ease, background 120ms ease;
    }
    .key:hover:enabled {
      transform: translateY(-1px);
      box-shadow: 0 8px 16px rgba(25, 23, 17, 0.08);
    }
    .key:active:enabled {
      transform: translateY(1px);
      box-shadow: inset 0 2px 4px rgba(25, 23, 17, 0.15);
    }
    .key:disabled {
      opacity: 0.45;
      cursor: default;
    }
    .status-text {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
    }
    @media (max-width: 900px) {
      .body {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-top">
        <div class="title">
          <h1>CalSci Hybrid</h1>
          <p>Helper-poll display mirror and keypad over the extension-owned serial session.</p>
        </div>
        <div class="chips">
          <div class="chip" id="sessionChip">Session offline</div>
          <div class="chip" id="hybridChip">Hybrid off</div>
          <div class="chip" id="transportChip">helper-poll</div>
        </div>
      </div>
      <div class="actions">
        <button class="action primary" id="toggleButton">Enable Hybrid</button>
        <button class="action" id="syncButton">Sync Full</button>
        <button class="action" id="folderSyncButton">Sync Folder</button>
      </div>
      <div class="status-text" id="statusText">Waiting for backend session.</div>
    </section>

    <section class="body">
      <section class="surface">
        <h2>Display Mirror</h2>
        <div class="display-wrap">
          <div class="display-frame">
            <canvas id="displayCanvas" width="128" height="64"></canvas>
          </div>
          <div class="meta">
            <div><strong>Port:</strong> <span id="portValue">-</span></div>
            <div><strong>Frame ID:</strong> <span id="frameValue">-</span></div>
            <div><strong>Nav:</strong> <span id="navValue">-</span></div>
            <div><strong>Key debounce:</strong> <span id="debounceValue">-</span></div>
          </div>
        </div>
      </section>

      <section class="surface">
        <h2>Keypad</h2>
        <div class="keypad" id="keypad"></div>
      </section>
    </section>
  </main>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const keypadLayout = ${keypadJson};
    const state = {
      session: { connected: false },
      hybridStatus: { connected: false, active: false },
      hybridState: {},
    };

    const sessionChip = document.getElementById("sessionChip");
    const hybridChip = document.getElementById("hybridChip");
    const transportChip = document.getElementById("transportChip");
    const statusText = document.getElementById("statusText");
    const toggleButton = document.getElementById("toggleButton");
    const syncButton = document.getElementById("syncButton");
    const folderSyncButton = document.getElementById("folderSyncButton");
    const portValue = document.getElementById("portValue");
    const frameValue = document.getElementById("frameValue");
    const navValue = document.getElementById("navValue");
    const debounceValue = document.getElementById("debounceValue");
    const keypad = document.getElementById("keypad");
    const canvas = document.getElementById("displayCanvas");
    const ctx = canvas.getContext("2d");

    function setChip(el, text, tone) {
      el.textContent = text;
      el.className = "chip" + (tone ? " " + tone : "");
    }

    function mergeHybridState(update) {
      state.hybridState = {
        ...state.hybridState,
        ...update,
        lines: update.lines !== undefined ? update.lines : state.hybridState.lines,
        fb: update.fb !== undefined ? update.fb : state.hybridState.fb,
      };
    }

    function drawDisplay() {
      ctx.fillStyle = "#dcead6";
      ctx.fillRect(0, 0, 128, 64);
      const fb = state.hybridState.fb;
      if (typeof fb === "string" && fb.length > 0) {
        const raw = Uint8Array.from(atob(fb), (ch) => ch.charCodeAt(0));
        ctx.fillStyle = "#101716";
        for (let page = 0; page < 8; page += 1) {
          const base = page * 128;
          for (let col = 0; col < 128; col += 1) {
            const value = raw[base + col] || 0;
            if (!value) {
              continue;
            }
            for (let bit = 0; bit < 8; bit += 1) {
              if (value & (1 << bit)) {
                ctx.fillRect(col, (page * 8) + bit, 1, 1);
              }
            }
          }
        }
        return;
      }

      const lines = Array.isArray(state.hybridState.lines) ? state.hybridState.lines : [];
      ctx.fillStyle = "#111716";
      ctx.font = "7px ui-monospace, 'Cascadia Mono', Consolas, monospace";
      lines.slice(0, 7).forEach((line, index) => {
        ctx.fillText(String(line).slice(0, 28), 4, 10 + (index * 8));
      });
    }

    function render() {
      const connected = Boolean(state.session.connected);
      const active = Boolean(state.hybridStatus.active);
      const error = typeof state.hybridStatus.error === "string" ? state.hybridStatus.error : "";
      setChip(sessionChip, connected ? "Session online" : "Session offline", connected ? "good" : "bad");
      setChip(hybridChip, active ? "Hybrid on" : "Hybrid off", active ? "good" : connected ? "warn" : "bad");
      transportChip.textContent = state.hybridStatus.transport || "helper-poll";

      toggleButton.textContent = active ? "Disable Hybrid" : "Enable Hybrid";
      toggleButton.disabled = !connected;
      syncButton.disabled = !active;
      folderSyncButton.disabled = !connected;

      portValue.textContent = state.session.port || state.hybridStatus.port || "-";
      frameValue.textContent = state.hybridState.frame_id !== undefined ? String(state.hybridState.frame_id) : "-";
      navValue.textContent = state.hybridState.nav || "-";
      debounceValue.textContent = state.hybridStatus.keyDebounceMs ? String(state.hybridStatus.keyDebounceMs) + " ms" : "-";

      if (error) {
        statusText.textContent = error;
      } else if (!connected) {
        statusText.textContent = "Select a device to open the persistent session.";
      } else if (!active) {
        statusText.textContent = "Hybrid panel is ready. Enable helper mode to mirror the display and inject keypad presses.";
      } else {
        statusText.textContent = "Hybrid helper polling is active on the extension-owned session.";
      }

      drawDisplay();
      keypad.querySelectorAll("button").forEach((button) => {
        button.disabled = !active;
      });
    }

    function buildKeypad() {
      keypadLayout.forEach((row, rowIndex) => {
        row.forEach((label, colIndex) => {
          const button = document.createElement("button");
          button.className = "key";
          button.type = "button";
          button.textContent = label;
          button.title = "col " + colIndex + ", row " + rowIndex;
          button.addEventListener("click", () => {
            vscode.postMessage({ type: "key", col: colIndex, row: rowIndex });
          });
          keypad.appendChild(button);
        });
      });
    }

    toggleButton.addEventListener("click", () => {
      vscode.postMessage({ type: "toggleHybrid", enabled: !state.hybridStatus.active });
    });
    syncButton.addEventListener("click", () => {
      vscode.postMessage({ type: "syncFull" });
    });
    folderSyncButton.addEventListener("click", () => {
      vscode.postMessage({ type: "syncFolder" });
    });

    window.addEventListener("message", (event) => {
      const message = event.data || {};
      if (message.type === "snapshot") {
        state.session = message.session || { connected: false };
        state.hybridStatus = message.hybridStatus || { connected: false, active: false };
        state.hybridState = message.hybridState || {};
        render();
        return;
      }
      if (message.type === "session") {
        state.session = message.session || { connected: false };
        render();
        return;
      }
      if (message.type === "hybridStatus") {
        state.hybridStatus = { ...state.hybridStatus, ...(message.hybridStatus || {}) };
        render();
        return;
      }
      if (message.type === "hybridState") {
        mergeHybridState(message.hybridState || {});
        render();
      }
    });

    buildKeypad();
    render();
    vscode.postMessage({ type: "ready" });
  </script>
</body>
</html>`;
  }
}
