import * as vscode from "vscode";

import { mergeHybridState, type HybridState, type HybridStatus, type SessionState } from "../core/shared";

export type HybridPanelHandlers = {
  onEnable: () => Promise<void>;
  onDisable: () => Promise<void>;
  onSoftReset: () => Promise<void>;
  onSyncFull: () => Promise<void>;
  onSyncFolder: () => Promise<void>;
  onFirmwareUpload: () => Promise<void>;
  onKeyPress: (col: number, row: number) => Promise<void>;
  onDispose: () => void;
};

type HybridUiButton = {
  label: string;
  mapped: boolean;
  row?: number;
  col?: number;
  shape: "rect" | "circle";
  main?: boolean;
  alpha?: string;
  beta?: string;
  slot?: "up" | "down" | "left" | "right" | "ok";
  action?: "softReset";
};

const HYBRID_KEY_LAYOUT_DEFAULT = [
  ["on", "alpha", "beta", "home", "wifi"],
  ["backlight", "back", "toolbox", "diff()", "ln()"],
  ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
  ["module", "bluetooth", "sin()", "cos()", "tan()"],
  ["igtn()", "pi", "e", "summation", "fraction"],
  ["log", "pow(,)", "pow( ,0.5)", "pow( ,2)", "S_D"],
  ["7", "8", "9", "nav_b", "AC"],
  ["4", "5", "6", "*", "/"],
  ["1", "2", "3", "+", "-"],
  [".", "0", ",", "ans", "exe"],
] as const;

const HYBRID_KEY_LAYOUT_ALPHA = [
  ["on", "alpha", "beta", "home", "wifi"],
  ["backlight", "back", "caps", "f", "l"],
  ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
  ["a", "b", "c", "d", "e"],
  ["g", "h", "i", "j", "k"],
  ["m", "n", "o", "p", "q"],
  ["r", "s", "t", "nav_b", "AC"],
  ["u", "v", "w", "*", "/"],
  ["x", "y", "z", "+", "-"],
  [" ", "off", "tab", "ans", "exe"],
] as const;

const HYBRID_KEY_LAYOUT_BETA = [
  ["on", "alpha", "beta", "home", "wifi"],
  ["backlight", "back", "undo", "=", "$"],
  ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
  ["copy", "paste", "asin(", "acos(", "atan("],
  ["&", "`", "\"", "'", "shot"],
  ["^", "~", "!", "<", ">"],
  ["[", "]", "%", "nav_b", "AC"],
  ["{", "}", ":", "*", "/"],
  ["(", ")", ";", "+", "-"],
  ["@", "?", "\"", "ans", "exe"],
] as const;

const HYBRID_DISPLAY_LABELS: Record<string, string> = {
  on: "ON",
  alpha: "a",
  beta: "b",
  home: "HOME",
  wifi: "WIFI",
  backlight: "BL",
  back: "BACK",
  toolbox: "TB",
  "diff()": "d/dx",
  "ln()": "ln",
  nav_l: "<",
  nav_d: "v",
  nav_r: ">",
  nav_u: "^",
  ok: "OK",
  module: "|x|",
  bluetooth: "BT",
  "sin()": "sin",
  "cos()": "cos",
  "tan()": "tan",
  "igtn()": "int",
  pi: "pi",
  summation: "sum",
  fraction: "a/b",
  "pow(,)": "x^y",
  "pow( ,0.5)": "sqrt",
  "pow( ,2)": "x^2",
  S_D: "S<->D",
  nav_b: "DEL",
  ans: "ANS",
  exe: "EXE",
  caps: "caps",
  undo: "undo",
  copy: "copy",
  paste: "paste",
  "asin(": "asin",
  "acos(": "acos",
  "atan(": "atan",
  off: "off",
  tab: "tab",
  shot: "shot",
  " ": "SP",
};

const HYBRID_CORNER_LABEL_SKIP = new Set([
  "on",
  "alpha",
  "beta",
  "home",
  "wifi",
  "backlight",
  "back",
  "nav_l",
  "nav_d",
  "nav_r",
  "nav_u",
  "ok",
  "nav_b",
  "AC",
  "ans",
  "exe",
]);

function hybridDisplayLabel(key: string): string {
  return HYBRID_DISPLAY_LABELS[key] ?? key;
}

function hybridCornerLabel(altKey: string, defaultKey: string): string {
  if (altKey === defaultKey || HYBRID_CORNER_LABEL_SKIP.has(altKey)) {
    return "";
  }
  const label = hybridDisplayLabel(altKey);
  return label.length > 5 ? label.slice(0, 5) : label;
}

function hybridMainButton(row: number, col: number): HybridUiButton {
  const defaultKey = HYBRID_KEY_LAYOUT_DEFAULT[row][col];
  const alphaKey = HYBRID_KEY_LAYOUT_ALPHA[row][col];
  const betaKey = HYBRID_KEY_LAYOUT_BETA[row][col];
  return {
    label: hybridDisplayLabel(defaultKey),
    mapped: true,
    row,
    col,
    shape: "rect",
    main: true,
    alpha: hybridCornerLabel(alphaKey, defaultKey),
    beta: hybridCornerLabel(betaKey, defaultKey),
  };
}

const HYBRID_UI_PROFILE = {
  systemRows: [
    [
      { label: hybridDisplayLabel("on"), mapped: true, row: 0, col: 0, shape: "rect" },
      { label: "RST", mapped: false, shape: "circle", action: "softReset" },
      { label: "Boot", mapped: false, shape: "circle" },
    ],
    [
      { label: hybridDisplayLabel("alpha"), mapped: true, row: 0, col: 1, shape: "rect" },
      { label: hybridDisplayLabel("beta"), mapped: true, row: 0, col: 2, shape: "rect" },
      { label: hybridDisplayLabel("home"), mapped: true, row: 0, col: 3, shape: "rect" },
    ],
    [
      { label: hybridDisplayLabel("back"), mapped: true, row: 1, col: 1, shape: "rect" },
      { label: hybridDisplayLabel("backlight"), mapped: true, row: 1, col: 0, shape: "rect" },
      { label: hybridDisplayLabel("wifi"), mapped: true, row: 0, col: 4, shape: "rect" },
    ],
  ] satisfies HybridUiButton[][],
  navButtons: [
    { label: hybridDisplayLabel("nav_u"), mapped: true, row: 2, col: 4, shape: "rect", slot: "up" },
    { label: hybridDisplayLabel("nav_l"), mapped: true, row: 2, col: 0, shape: "rect", slot: "left" },
    { label: hybridDisplayLabel("ok"), mapped: true, row: 2, col: 3, shape: "rect", slot: "ok" },
    { label: hybridDisplayLabel("nav_r"), mapped: true, row: 2, col: 2, shape: "rect", slot: "right" },
    { label: hybridDisplayLabel("nav_d"), mapped: true, row: 2, col: 1, shape: "rect", slot: "down" },
  ] satisfies HybridUiButton[],
  section1: [
    [hybridMainButton(1, 2), hybridMainButton(3, 0), hybridMainButton(3, 1), hybridMainButton(3, 2), hybridMainButton(3, 3), hybridMainButton(3, 4)],
    [hybridMainButton(1, 3), hybridMainButton(4, 0), hybridMainButton(4, 1), hybridMainButton(4, 2), hybridMainButton(4, 3), hybridMainButton(4, 4)],
    [hybridMainButton(1, 4), hybridMainButton(5, 0), hybridMainButton(5, 1), hybridMainButton(5, 2), hybridMainButton(5, 3), hybridMainButton(5, 4)],
  ] satisfies HybridUiButton[][],
  section2: [
    [hybridMainButton(6, 0), hybridMainButton(6, 1), hybridMainButton(6, 2), hybridMainButton(6, 3), hybridMainButton(6, 4)],
    [hybridMainButton(7, 0), hybridMainButton(7, 1), hybridMainButton(7, 2), hybridMainButton(7, 3), hybridMainButton(7, 4)],
    [hybridMainButton(8, 0), hybridMainButton(8, 1), hybridMainButton(8, 2), hybridMainButton(8, 3), hybridMainButton(8, 4)],
    [hybridMainButton(9, 0), hybridMainButton(9, 1), hybridMainButton(9, 2), hybridMainButton(9, 3), hybridMainButton(9, 4)],
  ] satisfies HybridUiButton[][],
} as const;

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
        if (type === "softReset") {
          void this.handlers.onSoftReset();
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
        if (type === "uploadFirmware") {
          void this.handlers.onFirmwareUpload();
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
    const profileJson = JSON.stringify(HYBRID_UI_PROFILE);
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
      --ink: #171717;
      --muted: #62666d;
      --line: rgba(23, 23, 23, 0.14);
      --good: #0a7a52;
      --warn: #9c6200;
      --bad: #b23a29;
      --bg-top: #f3f4f6;
      --bg-bottom: #dde2e8;
      --panel: rgba(255, 255, 255, 0.82);
      --panel-shadow: 0 20px 48px rgba(28, 33, 40, 0.15);
      --shell-edge: #9f9f9f;
      --shell-top: #ffffff;
      --shell-mid: #f2f2f2;
      --shell-bottom: #e4e4e4;
      --bezel-top: #ffffff;
      --bezel-bottom: #e9e9e9;
      --display-bg: #ffffff;
      --display-ink: #10181e;
      --key-top: #ffffff;
      --key-mid: #f2f2f2;
      --key-bottom: #e2e2e2;
      --key-press-top: #e4e4e4;
      --key-press-bottom: #d2d2d2;
      --key-shadow: #969696;
      --key-border: #5c5c5c;
      --key-muted: #ececec;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(255, 255, 255, 0.85), transparent 26%),
        linear-gradient(180deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
      font-family: "DejaVu Sans", "Segoe UI", sans-serif;
    }
    .page {
      max-width: 760px;
      margin: 0 auto;
      padding: 18px 18px 24px;
      display: grid;
      gap: 0;
      justify-items: center;
    }
    .switch-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.86);
    }
    .switch-row label {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .switch-input {
      appearance: none;
      -webkit-appearance: none;
      width: 36px;
      height: 20px;
      margin: 0;
      border: 1px solid rgba(23, 23, 23, 0.14);
      border-radius: 999px;
      background: #c7ccd3;
      position: relative;
      cursor: pointer;
      transition: background 120ms ease, opacity 120ms ease;
    }
    .switch-input::after {
      content: "";
      position: absolute;
      top: 1px;
      left: 1px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #ffffff;
      box-shadow: 0 2px 5px rgba(0, 0, 0, 0.18);
      transition: transform 120ms ease;
    }
    .switch-input:checked {
      background: var(--good);
    }
    .switch-input:checked::after {
      transform: translateX(16px);
    }
    .switch-input:disabled {
      cursor: default;
      opacity: 0.45;
    }
    .workspace {
      display: flex;
      justify-content: center;
      width: 100%;
    }
    .shell-wrap {
      display: flex;
      justify-content: center;
      width: min(450px, 100%);
    }
    .device-shell {
      position: relative;
      width: 100%;
      aspect-ratio: 450 / 950;
      border: 2px solid var(--shell-edge);
      border-radius: 32px;
      background: linear-gradient(180deg, var(--shell-top) 0%, var(--shell-mid) 55%, var(--shell-bottom) 100%);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.75),
        0 22px 42px rgba(49, 53, 58, 0.2);
      overflow: hidden;
    }
    .device-shell.live {
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.75),
        0 22px 42px rgba(49, 53, 58, 0.2),
        0 0 0 4px rgba(10, 122, 82, 0.08);
    }
    .brand-badge {
      position: absolute;
      left: 40%;
      top: 1.895%;
      width: 20%;
      height: 2.737%;
      min-height: 24px;
      border: 1px solid #8e8e8e;
      border-radius: 6px;
      background: linear-gradient(180deg, #ffffff 0%, #ececec 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: clamp(13px, 1.45vw, 16px);
      font-weight: 500;
      letter-spacing: 0.04em;
    }
    .shell-switch {
      position: absolute;
      left: 2.8%;
      top: 1.55%;
      min-height: 24px;
      max-width: 34%;
      z-index: 2;
    }
    .shell-switch .switch-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 2px 7px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.9);
      box-shadow: 0 6px 16px rgba(28, 33, 40, 0.08);
      white-space: nowrap;
    }
    .shell-switch .switch-row label {
      font-size: clamp(8px, 0.95vw, 10px);
    }
    .shell-mode-button {
      min-width: 38px;
      height: 21px;
      border: 1px solid #8f9398;
      border-radius: 999px;
      background: linear-gradient(180deg, #ffffff 0%, #eceff2 100%);
      color: #1c2128;
      font-size: clamp(9px, 1vw, 11px);
      font-weight: 600;
      cursor: pointer;
      padding: 0 9px;
      transition: transform 120ms ease, box-shadow 120ms ease;
    }
    .shell-mode-button:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 10px rgba(28, 33, 40, 0.12);
    }
    .shell-mode-button:active {
      transform: translateY(1px);
      box-shadow: none;
    }
    .shell-mode-button:focus-visible {
      outline: 2px solid #0a7a52;
      outline-offset: 2px;
    }
    .shell-switch .switch-input {
      width: 32px;
      height: 18px;
    }
    .shell-switch .switch-input::after {
      width: 14px;
      height: 14px;
    }
    .shell-switch .switch-input:checked::after {
      transform: translateX(14px);
    }
    .display-bezel {
      position: absolute;
      left: 3.778%;
      top: 7.263%;
      width: 92.444%;
      height: 23.579%;
      border: 2px solid #939393;
      border-radius: 14px;
      background: linear-gradient(180deg, var(--bezel-top) 0%, var(--bezel-bottom) 100%);
    }
    .display-window {
      position: absolute;
      left: 7.333%;
      top: 8.947%;
      width: 85.333%;
      height: 20.211%;
      background: var(--display-bg);
      display: flex;
      align-items: stretch;
      justify-content: stretch;
      overflow: hidden;
    }
    .display-stage {
      position: absolute;
      inset: 0;
      transform-origin: top left;
      will-change: transform;
      touch-action: none;
    }
    .display-toolbar {
      position: absolute;
      top: 12px;
      right: 12px;
      z-index: 3;
      display: none;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border-radius: 999px;
      background: rgba(12, 16, 20, 0.78);
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.28);
      backdrop-filter: blur(10px);
    }
    .display-tool-button {
      min-width: 34px;
      height: 30px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
      color: #f7fbff;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      padding: 0 10px;
    }
    .display-tool-button:disabled {
      cursor: default;
      opacity: 0.45;
    }
    .display-tool-button:not(:disabled):hover {
      background: rgba(255, 255, 255, 0.18);
    }
    .display-zoom-value {
      min-width: 52px;
      color: #f7fbff;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-align: center;
    }
    canvas {
      width: 100%;
      height: 100%;
      display: block;
      background: var(--display-bg);
      image-rendering: pixelated;
      image-rendering: crisp-edges;
    }
    .cluster {
      position: absolute;
      display: grid;
    }
    .system-cluster {
      left: 7.333%;
      top: 34%;
      width: 32%;
      height: 14.316%;
      grid-template-columns: repeat(3, 1fr);
      grid-template-rows: repeat(3, 1fr);
      column-gap: 8.333%;
      row-gap: 5.882%;
    }
    .nav-cluster {
      left: 56.222%;
      top: 32.632%;
      width: 35.111%;
      height: 16.632%;
      grid-template-columns: repeat(3, 1fr);
      grid-template-rows: repeat(3, 1fr);
      gap: 2.532%;
      grid-template-areas:
        ". up ."
        "left ok right"
        ". down .";
    }
    .section-one {
      left: 7.333%;
      top: 49.579%;
      width: 84.444%;
      height: 19.158%;
      grid-template-columns: repeat(6, 1fr);
      grid-template-rows: repeat(3, 1fr);
      column-gap: 4.211%;
      row-gap: 8.791%;
    }
    .section-two {
      left: 7.333%;
      top: 70.421%;
      width: 84.889%;
      height: 26.105%;
      grid-template-columns: repeat(5, 1fr);
      grid-template-rows: repeat(4, 1fr);
      column-gap: 8.639%;
      row-gap: 6.452%;
    }
    .shell-key {
      position: relative;
      width: 100%;
      height: 100%;
      border: 1px solid var(--key-border);
      border-bottom-width: 2px;
      border-radius: 8px;
      background: linear-gradient(180deg, var(--key-top) 0%, var(--key-mid) 60%, var(--key-bottom) 100%);
      color: #111111;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.95);
      cursor: pointer;
      padding: 0;
      font: inherit;
      overflow: hidden;
    }
    .shell-key.circle-key {
      border-radius: 999px;
    }
    .shell-key:disabled {
      cursor: default;
      opacity: 1;
    }
    .shell-key.static-key {
      background: linear-gradient(180deg, #f6f6f6 0%, #ededed 100%);
      color: #666666;
      border-color: #8f8f8f;
      border-bottom-color: #8a8a8a;
    }
    .shell-key[data-actionable="true"]:not(:disabled):hover {
      transform: translateY(-1px);
    }
    .shell-key[data-actionable="true"]:not(:disabled):active {
      transform: translate(1px, 1px);
      background: linear-gradient(180deg, var(--key-press-top) 0%, var(--key-press-bottom) 100%);
      border-bottom-width: 1px;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.45);
    }
    .plain-label {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding-top: 1px;
      font-size: clamp(8px, 1.45vw, 15px);
      font-weight: 600;
    }
    .main-key {
      border-radius: 8px;
      box-shadow:
        0 2px 0 var(--key-shadow),
        inset 0 1px 0 rgba(255, 255, 255, 0.95);
    }
    .main-key[data-actionable="true"]:not(:disabled):active {
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.45);
    }
    .main-key::before {
      content: "";
      position: absolute;
      left: 1px;
      right: 1px;
      top: 1px;
      height: 48%;
      background: rgba(255, 255, 255, 0.72);
      pointer-events: none;
    }
    .key-alpha,
    .key-beta {
      position: absolute;
      top: 6%;
      font-size: clamp(6px, 0.98vw, 7px);
      line-height: 1;
      color: #2f2f2f;
      font-family: "DejaVu Sans", "Segoe UI", sans-serif;
    }
    .key-alpha {
      left: 8%;
    }
    .key-beta {
      right: 8%;
      text-align: right;
    }
    .key-main {
      position: absolute;
      left: 8%;
      right: 8%;
      bottom: 8%;
      text-align: center;
      color: #111111;
      font-family: "DejaVu Sans Mono", Consolas, monospace;
      font-size: clamp(8px, 1.46vw, 11px);
      font-weight: 700;
      line-height: 1.05;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: clip;
    }
    body.display-focus {
      background: #0d1117;
    }
    body.display-focus .page {
      min-height: 100vh;
      padding: 0;
    }
    body.display-focus .workspace,
    body.display-focus .shell-wrap {
      width: 100%;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    body.display-focus .device-shell {
      width: min(96vw, calc(94vh * 2));
      aspect-ratio: 2 / 1;
      height: auto;
      border: none;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    body.display-focus .display-window {
      left: 0;
      top: 0;
      width: 100%;
      height: 100%;
      border: 2px solid #939393;
      border-radius: 18px;
      box-shadow: 0 24px 48px rgba(0, 0, 0, 0.35);
      touch-action: none;
    }
    body.display-focus .display-window.pannable {
      cursor: grab;
    }
    body.display-focus .display-window.panning {
      cursor: grabbing;
    }
    body.display-focus .display-toolbar {
      display: flex;
    }
    body.display-focus .brand-badge,
    body.display-focus .shell-switch,
    body.display-focus .display-bezel,
    body.display-focus .system-cluster,
    body.display-focus .nav-cluster,
    body.display-focus .section-one,
    body.display-focus .section-two {
      display: none;
    }
    @media (max-width: 920px) {
      .workspace {
        justify-content: center;
      }
    }
    @media (max-width: 560px) {
      .page {
        padding: 12px;
      }
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="workspace">
      <div class="shell-wrap">
        <section class="device-shell" id="deviceShell" tabindex="0">
          <div class="shell-switch">
            <div class="switch-row">
              <label for="toggleSwitch">Hybrid</label>
              <input class="switch-input" id="toggleSwitch" type="checkbox" />
              <button class="shell-mode-button" id="displayOnlyButton" type="button">Full</button>
            </div>
          </div>
          <div class="brand-badge">CalSci</div>
          <div class="display-bezel"></div>
          <div class="display-window" id="displayWindow">
            <div class="display-stage" id="displayStage">
              <canvas id="displayCanvas" width="128" height="64"></canvas>
            </div>
            <div class="display-toolbar">
              <button class="display-tool-button" id="zoomOutButton" type="button" title="Zoom out">-</button>
              <span class="display-zoom-value" id="displayZoomValue">100%</span>
              <button class="display-tool-button" id="zoomInButton" type="button" title="Zoom in">+</button>
              <button class="display-tool-button" id="zoomResetButton" type="button" title="Reset zoom and pan">Fit</button>
            </div>
          </div>
          <div class="cluster system-cluster" id="systemCluster"></div>
          <div class="cluster nav-cluster" id="navCluster"></div>
          <div class="cluster section-one" id="sectionOne"></div>
          <div class="cluster section-two" id="sectionTwo"></div>
        </section>
      </div>
    </section>
  </main>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const profile = ${profileJson};
    const state = {
      session: { connected: false },
      hybridStatus: { connected: false, active: false },
      hybridState: {},
      displayOnly: false,
      displayViewport: {
        scale: 1,
        offsetX: 0,
        offsetY: 0,
        panning: false,
        pointerId: null,
        startX: 0,
        startY: 0,
        startOffsetX: 0,
        startOffsetY: 0,
      },
    };

    const DISPLAY_SCALE_MIN = 0.5;
    const DISPLAY_FIT_SCALE = 1;
    const DISPLAY_SCALE_MAX = 6;
    const DISPLAY_SCALE_STEP = 0.25;

    const toggleSwitch = document.getElementById("toggleSwitch");
    const displayOnlyButton = document.getElementById("displayOnlyButton");
    const deviceShell = document.getElementById("deviceShell");
    const displayWindow = document.getElementById("displayWindow");
    const displayStage = document.getElementById("displayStage");
    const zoomOutButton = document.getElementById("zoomOutButton");
    const zoomInButton = document.getElementById("zoomInButton");
    const zoomResetButton = document.getElementById("zoomResetButton");
    const displayZoomValue = document.getElementById("displayZoomValue");
    const systemCluster = document.getElementById("systemCluster");
    const navCluster = document.getElementById("navCluster");
    const sectionOne = document.getElementById("sectionOne");
    const sectionTwo = document.getElementById("sectionTwo");
    const canvas = document.getElementById("displayCanvas");
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;

    function mergeState(update) {
      state.hybridState = {
        ...state.hybridState,
        ...update,
        lines: update.lines !== undefined ? update.lines : state.hybridState.lines,
        fb: update.fb !== undefined ? update.fb : state.hybridState.fb,
      };
    }

    function drawDisplay() {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, 128, 64);

      const fb = state.hybridState.fb;
      if (typeof fb === "string" && fb.length > 0) {
        const raw = Uint8Array.from(atob(fb), (ch) => ch.charCodeAt(0));
        ctx.fillStyle = "#10181e";
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
      ctx.fillStyle = "#10181e";
      ctx.font = "7px 'DejaVu Sans Mono', Consolas, monospace";
      lines.slice(0, 7).forEach((line, index) => {
        ctx.fillText(String(line).slice(0, 28), 4, 10 + (index * 8));
      });
    }

    function createButton(definition) {
      const button = document.createElement("button");
      button.type = "button";
      button.className =
        "shell-key" +
        (definition.main ? " main-key" : "") +
        (definition.shape === "circle" ? " circle-key" : " rect-key") +
        (definition.mapped ? "" : " static-key");
      button.dataset.mapped = definition.mapped ? "true" : "false";
      button.dataset.actionable = definition.mapped || definition.action ? "true" : "false";
      if (definition.slot) {
        button.style.gridArea = definition.slot;
      }
      if (definition.main) {
        const alpha = document.createElement("span");
        alpha.className = "key-alpha";
        alpha.textContent = definition.alpha || "";
        button.appendChild(alpha);

        const beta = document.createElement("span");
        beta.className = "key-beta";
        beta.textContent = definition.beta || "";
        button.appendChild(beta);

        const main = document.createElement("span");
        main.className = "key-main";
        main.textContent = definition.label;
        button.appendChild(main);
      } else {
        const label = document.createElement("span");
        label.className = "plain-label";
        label.textContent = definition.label;
        button.appendChild(label);
      }

      if (definition.mapped) {
        button.title = definition.label + " (col " + definition.col + ", row " + definition.row + ")";
        button.addEventListener("click", () => {
          vscode.postMessage({ type: "key", col: definition.col, row: definition.row });
        });
      } else if (definition.action === "softReset") {
        button.title = "Soft reset the selected CalSci device";
        button.addEventListener("click", () => {
          vscode.postMessage({ type: "softReset" });
        });
      } else {
        button.disabled = true;
      }
      return button;
    }

    function appendRows(container, rows) {
      rows.forEach((row) => {
        row.forEach((definition) => {
          container.appendChild(createButton(definition));
        });
      });
    }

    function buildShell() {
      appendRows(systemCluster, profile.systemRows);
      profile.navButtons.forEach((definition) => {
        navCluster.appendChild(createButton(definition));
      });
      appendRows(sectionOne, profile.section1);
      appendRows(sectionTwo, profile.section2);
    }

    function setDisplayOnly(enabled) {
      state.displayOnly = Boolean(enabled);
      document.body.classList.toggle("display-focus", state.displayOnly);
      if (state.displayOnly) {
        requestAnimationFrame(() => {
          resetDisplayViewport();
          deviceShell.focus();
          render();
        });
        return;
      }
      resetDisplayViewport();
    }

    function clampDisplayOffsets(offsetX, offsetY, scale = state.displayViewport.scale) {
      const width = displayWindow.clientWidth || 0;
      const height = displayWindow.clientHeight || 0;
      const scaledWidth = width * scale;
      const scaledHeight = height * scale;
      if (scale <= DISPLAY_FIT_SCALE + 0.001) {
        return {
          x: (width - scaledWidth) / 2,
          y: (height - scaledHeight) / 2,
        };
      }
      const minX = width - scaledWidth;
      const minY = height - scaledHeight;
      return {
        x: Math.min(0, Math.max(minX, offsetX)),
        y: Math.min(0, Math.max(minY, offsetY)),
      };
    }

    function applyDisplayTransform() {
      const viewport = state.displayViewport;
      const clamped = clampDisplayOffsets(viewport.offsetX, viewport.offsetY, viewport.scale);
      viewport.offsetX = clamped.x;
      viewport.offsetY = clamped.y;
      displayStage.style.transform =
        "translate(" + viewport.offsetX + "px, " + viewport.offsetY + "px) scale(" + viewport.scale + ")";
      displayWindow.classList.toggle("pannable", state.displayOnly && viewport.scale > DISPLAY_FIT_SCALE + 0.001);
      displayWindow.classList.toggle("panning", state.displayOnly && viewport.panning);
      displayZoomValue.textContent = Math.round(viewport.scale * 100) + "%";
    }

    function resetDisplayViewport() {
      state.displayViewport.scale = DISPLAY_FIT_SCALE;
      state.displayViewport.offsetX = 0;
      state.displayViewport.offsetY = 0;
      state.displayViewport.panning = false;
      state.displayViewport.pointerId = null;
      applyDisplayTransform();
    }

    function isDisplayViewportFit() {
      return (
        Math.abs(state.displayViewport.scale - DISPLAY_FIT_SCALE) < 0.001 &&
        Math.abs(state.displayViewport.offsetX) < 0.5 &&
        Math.abs(state.displayViewport.offsetY) < 0.5
      );
    }

    function setDisplayScale(nextScale) {
      const viewport = state.displayViewport;
      const scale = Math.max(DISPLAY_SCALE_MIN, Math.min(DISPLAY_SCALE_MAX, nextScale));
      if (Math.abs(scale - viewport.scale) < 0.001) {
        return;
      }
      const width = displayWindow.clientWidth || 0;
      const height = displayWindow.clientHeight || 0;
      const centerX = width / 2;
      const centerY = height / 2;
      const contentX = (centerX - viewport.offsetX) / viewport.scale;
      const contentY = (centerY - viewport.offsetY) / viewport.scale;

      viewport.scale = scale;
      viewport.offsetX = centerX - (contentX * scale);
      viewport.offsetY = centerY - (contentY * scale);
      applyDisplayTransform();
    }

    function stopDisplayPan(pointerId) {
      const viewport = state.displayViewport;
      if (viewport.pointerId !== pointerId) {
        return;
      }
      viewport.panning = false;
      viewport.pointerId = null;
      if (displayWindow.hasPointerCapture(pointerId)) {
        displayWindow.releasePointerCapture(pointerId);
      }
      applyDisplayTransform();
    }

    function render() {
      const connected = Boolean(state.session.connected);
      const active = Boolean(state.hybridStatus.active);

      toggleSwitch.checked = active;
      toggleSwitch.disabled = !connected;
      toggleSwitch.title = connected ? "Toggle hybrid mode" : "Connect a CalSci device to enable hybrid mode.";
      displayOnlyButton.textContent = state.displayOnly ? "Exit" : "Full";
      displayOnlyButton.title = state.displayOnly
        ? "Exit display-only mode."
        : "Show only the simulator display. Press Enter or Escape to exit.";
      deviceShell.classList.toggle("live", active);
      document.body.classList.toggle("display-focus", state.displayOnly);
      zoomOutButton.disabled = !state.displayOnly || state.displayViewport.scale <= DISPLAY_SCALE_MIN + 0.001;
      zoomInButton.disabled = !state.displayOnly || state.displayViewport.scale >= DISPLAY_SCALE_MAX - 0.001;
      zoomResetButton.disabled = !state.displayOnly || isDisplayViewportFit();

      drawDisplay();
      applyDisplayTransform();
      document.querySelectorAll(".shell-key").forEach((button) => {
        const element = button;
        if (element.dataset.mapped === "true") {
          element.disabled = !active;
          return;
        }
        if (element.dataset.actionable === "true") {
          element.disabled = !connected;
        }
      });
    }

    toggleSwitch.addEventListener("change", () => {
      if (toggleSwitch.disabled) {
        return;
      }
      vscode.postMessage({ type: "toggleHybrid", enabled: toggleSwitch.checked });
    });

    displayOnlyButton.addEventListener("click", () => {
      setDisplayOnly(!state.displayOnly);
      render();
    });

    zoomInButton.addEventListener("click", () => {
      setDisplayScale(state.displayViewport.scale + DISPLAY_SCALE_STEP);
      render();
    });

    zoomOutButton.addEventListener("click", () => {
      setDisplayScale(state.displayViewport.scale - DISPLAY_SCALE_STEP);
      render();
    });

    zoomResetButton.addEventListener("click", () => {
      resetDisplayViewport();
      render();
    });

    displayWindow.addEventListener("pointerdown", (event) => {
      if (!state.displayOnly || state.displayViewport.scale <= DISPLAY_FIT_SCALE + 0.001) {
        return;
      }
      state.displayViewport.panning = true;
      state.displayViewport.pointerId = event.pointerId;
      state.displayViewport.startX = event.clientX;
      state.displayViewport.startY = event.clientY;
      state.displayViewport.startOffsetX = state.displayViewport.offsetX;
      state.displayViewport.startOffsetY = state.displayViewport.offsetY;
      displayWindow.setPointerCapture(event.pointerId);
      applyDisplayTransform();
      event.preventDefault();
    });

    displayWindow.addEventListener("pointermove", (event) => {
      if (!state.displayViewport.panning || state.displayViewport.pointerId !== event.pointerId) {
        return;
      }
      state.displayViewport.offsetX =
        state.displayViewport.startOffsetX + (event.clientX - state.displayViewport.startX);
      state.displayViewport.offsetY =
        state.displayViewport.startOffsetY + (event.clientY - state.displayViewport.startY);
      applyDisplayTransform();
    });

    displayWindow.addEventListener("pointerup", (event) => {
      stopDisplayPan(event.pointerId);
    });

    displayWindow.addEventListener("pointercancel", (event) => {
      stopDisplayPan(event.pointerId);
    });

    displayWindow.addEventListener("wheel", (event) => {
      if (!state.displayOnly) {
        return;
      }
      event.preventDefault();
      const direction = event.deltaY < 0 ? DISPLAY_SCALE_STEP : -DISPLAY_SCALE_STEP;
      setDisplayScale(state.displayViewport.scale + direction);
      render();
    }, { passive: false });

    window.addEventListener("keydown", (event) => {
      if (!state.displayOnly) {
        return;
      }
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setDisplayScale(state.displayViewport.scale + DISPLAY_SCALE_STEP);
        render();
        return;
      }
      if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        setDisplayScale(state.displayViewport.scale - DISPLAY_SCALE_STEP);
        render();
        return;
      }
      if (event.key === "0") {
        event.preventDefault();
        resetDisplayViewport();
        render();
        return;
      }
      if (event.key === "Enter" || event.key === "Escape") {
        event.preventDefault();
        setDisplayOnly(false);
        render();
      }
    });

    window.addEventListener("resize", () => {
      if (!state.displayOnly) {
        return;
      }
      applyDisplayTransform();
      render();
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
        mergeState(message.hybridState || {});
        render();
      }
    });

    buildShell();
    render();
    vscode.postMessage({ type: "ready" });
  </script>
</body>
</html>`;
  }
}
