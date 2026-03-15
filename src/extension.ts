import * as fs from "fs";
import * as path from "path";
import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as readline from "readline";
import * as vscode from "vscode";

const SELECTED_PORT_KEY = "selectedPort";
const POLL_INTERVAL_MS = 1000;
const BACKEND_TIMEOUT_BUFFER_SEC = 30;
const SESSION_RETRY_BACKOFF_MS = 3000;

type DeviceInfo = {
  port: string;
  product: string;
  description: string;
};

type ScanResult = {
  ok: boolean;
  devices?: DeviceInfo[];
  error?: string;
};

type SoftResetResult = {
  ok: boolean;
  promptSeen: boolean;
  rebootSeen?: boolean;
  port: string;
  output: string;
  error?: string;
};

type RunFileResult = {
  ok: boolean;
  port: string;
  localFile: string;
  output: string;
  cancelled?: boolean;
  error?: string;
};

type RunCancelResult = {
  ok: boolean;
  active?: boolean;
  cancelled?: boolean;
  requestId?: string;
  error?: string;
};

type SessionState = {
  connected: boolean;
  port?: string | null;
  error?: string;
  reason?: string;
};

type SessionResult = SessionState & {
  ok: boolean;
};

type TerminalWriteResult = {
  ok: boolean;
  error?: string;
};

type HybridStatus = {
  connected: boolean;
  active: boolean;
  port?: string | null;
  transport?: string;
  mode?: boolean;
  keyDebounceMs?: number | null;
  graphFastMs?: number | null;
  error?: string;
  reason?: string;
};

type HybridState = {
  frame_id?: number;
  fb_seq?: number;
  fb?: string;
  fb_full?: boolean;
  fb_seen?: boolean;
  nav?: string;
  lines?: string[];
  mode?: boolean;
  capture_enabled?: boolean;
};

type HybridSnapshotResult = {
  ok: boolean;
  status: HybridStatus;
  state: HybridState;
  error?: string;
};

type HybridKeyResult = {
  ok: boolean;
  ack?: string;
  error?: string;
};

type BackendReadyMessage = {
  type: "ready";
};

type BackendStreamMessage = {
  id: string;
  type: "stream";
  stream: "stdout" | "stderr";
  line: string;
};

type BackendResultMessage = {
  id: string;
  type: "result";
  payload: unknown;
};

type BackendTerminalOutputEventMessage = {
  type: "event";
  event: "terminal-output";
  data: string;
};

type BackendSessionEventMessage = {
  type: "event";
  event: "session";
  payload: SessionState;
};

type BackendHybridStatusEventMessage = {
  type: "event";
  event: "hybrid";
  payload: {
    type: "status";
  } & HybridStatus;
};

type BackendHybridStateEventMessage = {
  type: "event";
  event: "hybrid";
  payload: {
    type: "state";
    state: HybridState;
  };
};

type BackendHybridEventPayload = BackendHybridStatusEventMessage["payload"] | BackendHybridStateEventMessage["payload"];

type BackendMessage =
  | BackendReadyMessage
  | BackendStreamMessage
  | BackendResultMessage
  | BackendTerminalOutputEventMessage
  | BackendSessionEventMessage
  | BackendHybridStatusEventMessage
  | BackendHybridStateEventMessage;

type PendingBackendRequest<T> = {
  resolve: (payload: T) => void;
  reject: (error: Error) => void;
  onStream?: (line: string, isError: boolean) => void;
};

type ProcessResult = {
  code: number;
  stdout: string;
  stderr: string;
};

type PollOptions = {
  forceSessionConnect?: boolean;
  showTerminalOnConnect?: boolean;
};

type EnsureSessionOptions = {
  force?: boolean;
  notifyOnError?: boolean;
  showTerminal?: boolean;
};

const HYBRID_KEYPAD_LABELS: string[][] = [
  ["ON", "ALPHA", "BETA", "HOME", "WIFI"],
  ["BL", "BACK", "TB", "d/dx", "ln"],
  ["<", "v", ">", "OK", "^"],
  ["|x|", "BT", "sin", "cos", "tan"],
  ["int", "pi", "e", "sum", "a/b"],
  ["log", "x^y", "sqrt", "x^2", "S<->D"],
  ["7", "8", "9", "DEL", "AC"],
  ["4", "5", "6", "*", "/"],
  ["1", "2", "3", "+", "-"],
  [".", "0", ",", "ANS", "EXE"],
];

function mergeHybridState(base: HybridState, update: HybridState): HybridState {
  return {
    ...base,
    ...update,
    lines: update.lines ?? base.lines,
    fb: update.fb ?? base.fb,
  };
}

class BackendServiceClient implements vscode.Disposable {
  private readonly storagePath: string;
  private readonly venvPath: string;
  private readonly backendScriptPath: string;

  private readonly terminalOutputEmitter = new vscode.EventEmitter<string>();
  private readonly sessionStateEmitter = new vscode.EventEmitter<SessionState>();
  private readonly hybridEventEmitter = new vscode.EventEmitter<BackendHybridEventPayload>();

  private venvPythonPath: string | undefined;
  private serviceProcess: ChildProcessWithoutNullStreams | undefined;
  private serviceReader: readline.Interface | undefined;
  private serviceStartPromise: Promise<void> | undefined;
  private serviceStderr = "";

  private nextRequestId = 1;
  private readonly pendingRequests = new Map<string, PendingBackendRequest<unknown>>();

  public readonly onTerminalOutput = this.terminalOutputEmitter.event;
  public readonly onSessionState = this.sessionStateEmitter.event;
  public readonly onHybridEvent = this.hybridEventEmitter.event;

  constructor(private readonly context: vscode.ExtensionContext) {
    this.storagePath = context.globalStorageUri.fsPath;
    this.venvPath = path.join(this.storagePath, "pyenv");
    this.backendScriptPath = path.join(context.extensionPath, "backend", "calsci_backend.py");
  }

  public async ensureReady(): Promise<void> {
    await fs.promises.mkdir(this.storagePath, { recursive: true });

    const systemPython = await this.resolveSystemPython();
    const venvPython = this.getVenvPythonPath();
    const hasVenvPython = await this.fileExists(venvPython);

    if (!hasVenvPython) {
      const created = await this.runProcess(systemPython[0], [...systemPython.slice(1), "-m", "venv", this.venvPath], 120000);
      if (created.code !== 0) {
        throw new Error(this.joinStdStreams(created, "Failed to create private Python environment."));
      }
    }

    this.venvPythonPath = venvPython;
    await this.ensurePyserialInstalled();
    await this.startService();
  }

  public dispose(): void {
    this.stopService("CalSci backend disposed");
    this.terminalOutputEmitter.dispose();
    this.sessionStateEmitter.dispose();
    this.hybridEventEmitter.dispose();
  }

  public async restartService(): Promise<void> {
    this.stopService("CalSci backend restart requested");
    await this.startService();
  }

  public async scan(): Promise<ScanResult> {
    return this.request<ScanResult>("scan", {});
  }

  public async openSession(port: string): Promise<SessionResult> {
    return this.normalizeSessionResult(await this.request<SessionResult>("session.open", { port }));
  }

  public async closeSession(): Promise<SessionResult> {
    return this.normalizeSessionResult(await this.request<SessionResult>("session.close", {}));
  }

  public async getSessionState(): Promise<SessionResult> {
    return this.normalizeSessionResult(await this.request<SessionResult>("session.state", {}));
  }

  public async sendTerminalInput(data: string): Promise<void> {
    const result = await this.request<TerminalWriteResult>("terminal.write", { data });
    if (!result.ok) {
      throw new Error(result.error ?? "Failed to write to CalSci REPL.");
    }
  }

  public async startHybrid(port?: string): Promise<HybridSnapshotResult> {
    return this.request<HybridSnapshotResult>("hybrid.start", port ? { port } : {});
  }

  public async stopHybrid(): Promise<HybridSnapshotResult> {
    return this.request<HybridSnapshotResult>("hybrid.stop", {});
  }

  public async getHybridSnapshot(): Promise<HybridSnapshotResult> {
    return this.request<HybridSnapshotResult>("hybrid.snapshot", {});
  }

  public async syncHybridFull(): Promise<HybridSnapshotResult> {
    return this.request<HybridSnapshotResult>("hybrid.sync-full", {});
  }

  public async sendHybridKey(col: number, row: number): Promise<HybridKeyResult> {
    return this.request<HybridKeyResult>("hybrid.key", { col, row });
  }

  public async softReset(port: string, timeoutSeconds: number): Promise<SoftResetResult> {
    return this.request<SoftResetResult>("soft-reset", {
      port,
      timeout: timeoutSeconds,
    });
  }

  public async runFileStreaming(
    port: string,
    localFile: string,
    timeoutSeconds: number,
    onOutputLine: (line: string, isError: boolean) => void,
    cancelToken: vscode.CancellationToken,
  ): Promise<RunFileResult> {
    const normalizedTimeout = Math.max(0, timeoutSeconds);
    const backendTimeoutMs = normalizedTimeout > 0 ? (normalizedTimeout + BACKEND_TIMEOUT_BUFFER_SEC) * 1000 : undefined;

    return new Promise((resolve) => {
      let settled = false;
      let cancelRequested = false;

      const finish = (result: RunFileResult): void => {
        if (settled) {
          return;
        }
        settled = true;
        if (timeoutHandle) {
          clearTimeout(timeoutHandle);
        }
        cancelDisposable.dispose();
        resolve(result);
      };

      const requestCancel = (reason: string): void => {
        if (cancelRequested || settled) {
          return;
        }
        cancelRequested = true;
        onOutputLine(`[CalSci] ${reason}`, false);
        void this.request<RunCancelResult>("run.cancel", {}).catch(() => undefined);
      };

      const timeoutHandle = backendTimeoutMs
        ? setTimeout(() => {
            requestCancel(`Run exceeded ${backendTimeoutMs}ms guard timeout; sending interrupt...`);
          }, backendTimeoutMs)
        : undefined;

      const cancelDisposable = cancelToken.onCancellationRequested(() => {
        requestCancel("Cancel requested; interrupting device run...");
      });

      this.request<RunFileResult>(
        "run-file",
        {
          port,
          localFile,
          timeout: normalizedTimeout,
        },
        {
          stream: true,
          onStream: onOutputLine,
        },
      ).then(
        (payload) => finish(payload),
        (error) => finish({ ok: false, port, localFile, output: "", error: error instanceof Error ? error.message : String(error) }),
      );
    });
  }

  private async request<T>(
    command: string,
    args: Record<string, unknown>,
    options?: {
      stream?: boolean;
      onStream?: (line: string, isError: boolean) => void;
    },
  ): Promise<T> {
    await this.startService();

    const service = this.serviceProcess;
    if (!service) {
      throw new Error("CalSci backend service is unavailable.");
    }

    const requestId = String(this.nextRequestId++);
    const payload = JSON.stringify({
      id: requestId,
      command,
      args,
      stream: Boolean(options?.stream),
    });

    return new Promise<T>((resolve, reject) => {
      this.pendingRequests.set(requestId, {
        resolve: (result) => resolve(result as T),
        reject,
        onStream: options?.onStream,
      });

      service.stdin.write(payload + "\n", "utf8", (error?: Error | null) => {
        if (!error) {
          return;
        }
        this.pendingRequests.delete(requestId);
        this.stopService(error.message);
        reject(error);
      });
    });
  }

  private async startService(): Promise<void> {
    if (this.serviceProcess && !this.serviceProcess.killed) {
      return;
    }
    if (this.serviceStartPromise) {
      return this.serviceStartPromise;
    }

    const python = this.requireVenvPython();

    this.serviceStartPromise = new Promise<void>((resolve, reject) => {
      const child = spawn(python, [this.backendScriptPath, "serve"], { shell: false });
      let ready = false;

      this.serviceProcess = child;
      this.serviceStderr = "";

      this.serviceReader = readline.createInterface({
        input: child.stdout,
        crlfDelay: Infinity,
      });

      this.serviceReader.on("line", (line: string) => {
        let message: BackendMessage;
        try {
          message = JSON.parse(line) as BackendMessage;
        } catch {
          const error = new Error(`Invalid backend output: ${line}`);
          if (!ready) {
            reject(error);
          }
          this.stopService(error.message);
          return;
        }

        if (message.type === "ready") {
          ready = true;
          resolve();
          return;
        }

        this.handleServiceMessage(message);
      });

      child.stderr.on("data", (chunk: Buffer) => {
        this.serviceStderr += chunk.toString("utf8");
      });

      child.on("error", (error) => {
        if (!ready) {
          reject(error);
        }
        this.stopService(error.message);
      });

      child.on("close", (code) => {
        const detail = this.serviceStderr.trim();
        const reason = detail.length > 0 ? detail : `Backend service exited with code ${code ?? 1}`;
        if (!ready) {
          reject(new Error(reason));
        }
        this.stopService(reason);
      });
    });

    try {
      await this.serviceStartPromise;
    } finally {
      this.serviceStartPromise = undefined;
    }
  }

  private handleServiceMessage(message: BackendMessage): void {
    if (message.type === "stream") {
      const pending = this.pendingRequests.get(message.id);
      if (pending?.onStream) {
        pending.onStream(message.line, message.stream === "stderr");
      }
      return;
    }

    if (message.type === "result") {
      const pending = this.pendingRequests.get(message.id);
      if (!pending) {
        return;
      }
      this.pendingRequests.delete(message.id);
      pending.resolve(message.payload);
      return;
    }

    if (message.type === "event" && message.event === "terminal-output") {
      this.terminalOutputEmitter.fire(message.data);
      return;
    }

    if (message.type === "event" && message.event === "session") {
      this.sessionStateEmitter.fire(this.normalizeSessionState(message.payload));
      return;
    }

    if (message.type === "event" && message.event === "hybrid") {
      this.hybridEventEmitter.fire(message.payload);
    }
  }

  private stopService(reason: string): void {
    const process = this.serviceProcess;
    this.serviceProcess = undefined;

    if (this.serviceReader) {
      this.serviceReader.close();
      this.serviceReader = undefined;
    }

    if (process && !process.killed) {
      try {
        process.kill();
      } catch {
        // Ignore teardown errors.
      }
    }

    this.sessionStateEmitter.fire({ connected: false, error: reason });
    this.hybridEventEmitter.fire({ type: "status", connected: false, active: false, error: reason });

    for (const [requestId, pending] of this.pendingRequests) {
      this.pendingRequests.delete(requestId);
      pending.reject(new Error(reason));
    }
  }

  private requireVenvPython(): string {
    if (!this.venvPythonPath) {
      throw new Error("CalSci backend is not initialized.");
    }
    return this.venvPythonPath;
  }

  private async ensurePyserialInstalled(): Promise<void> {
    const python = this.requireVenvPython();

    const check = await this.runProcess(python, ["-c", "import serial"], 10000);
    if (check.code === 0) {
      return;
    }

    const install = await this.runProcess(
      python,
      ["-m", "pip", "install", "--disable-pip-version-check", "pyserial"],
      180000,
    );
    if (install.code !== 0) {
      throw new Error(this.joinStdStreams(install, "Failed to install pyserial in CalSci runtime."));
    }
  }

  private async resolveSystemPython(): Promise<string[]> {
    const candidates = process.platform === "win32" ? [["py", "-3"], ["python"], ["python3"]] : [["python3"], ["python"]];

    for (const candidate of candidates) {
      const result = await this.runProcess(candidate[0], [...candidate.slice(1), "--version"], 10000);
      if (result.code === 0) {
        return candidate;
      }
    }

    throw new Error("No usable system Python found.");
  }

  private getVenvPythonPath(): string {
    if (process.platform === "win32") {
      return path.join(this.venvPath, "Scripts", "python.exe");
    }
    return path.join(this.venvPath, "bin", "python");
  }

  private async fileExists(targetPath: string): Promise<boolean> {
    try {
      await fs.promises.access(targetPath, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }

  private joinStdStreams(result: ProcessResult, fallback: string): string {
    const details = [result.stderr.trim(), result.stdout.trim()].filter((value) => value.length > 0).join("\n");
    return details.length > 0 ? details : fallback;
  }

  private normalizeSessionState(state: SessionState): SessionState {
    return {
      connected: state.connected,
      port: state.port ?? undefined,
      error: state.error?.trim() || undefined,
      reason: state.reason?.trim() || undefined,
    };
  }

  private normalizeSessionResult(result: SessionResult): SessionResult {
    return {
      ok: result.ok,
      connected: result.connected,
      port: result.port ?? undefined,
      error: result.error?.trim() || undefined,
      reason: result.reason?.trim() || undefined,
    };
  }

  private runProcess(command: string, args: string[], timeoutMs: number): Promise<ProcessResult> {
    return new Promise((resolve, reject) => {
      const child = spawn(command, args, { shell: false });
      let stdout = "";
      let stderr = "";
      let timedOut = false;

      const timeoutHandle = setTimeout(() => {
        timedOut = true;
        child.kill();
      }, timeoutMs);

      child.stdout.on("data", (chunk: Buffer) => {
        stdout += chunk.toString("utf8");
      });

      child.stderr.on("data", (chunk: Buffer) => {
        stderr += chunk.toString("utf8");
      });

      child.on("error", (error) => {
        clearTimeout(timeoutHandle);
        reject(error);
      });

      child.on("close", (code) => {
        clearTimeout(timeoutHandle);
        if (timedOut) {
          resolve({ code: 1, stdout, stderr: `${stderr}\nProcess timed out after ${timeoutMs}ms`.trim() });
          return;
        }
        resolve({ code: code ?? 1, stdout, stderr });
      });
    });
  }
}

class CalSciReplPseudoterminal implements vscode.Pseudoterminal, vscode.Disposable {
  private readonly writeEmitter = new vscode.EventEmitter<string>();
  private readonly disposables: vscode.Disposable[] = [];
  private readonly pendingWrites: string[] = [];

  private opened = false;
  private connectedPort: string | undefined;
  private disconnectedKey: string | undefined;
  private lastInputError: string | undefined;

  public readonly onDidWrite = this.writeEmitter.event;

  constructor(private readonly backend: BackendServiceClient) {
    this.disposables.push(
      this.backend.onTerminalOutput((data: string) => {
        this.lastInputError = undefined;
        this.write(data);
      }),
      this.backend.onSessionState((state: SessionState) => {
        this.handleSessionState(state);
      }),
    );
  }

  public open(): void {
    this.opened = true;
    this.flushPendingWrites();
    this.writeLocalLine("CalSci REPL ready.");
  }

  public close(): void {
    this.opened = false;
  }

  public handleInput(data: string): void {
    void this.backend.sendTerminalInput(data).then(
      () => {
        this.lastInputError = undefined;
      },
      (error: unknown) => {
        const message = error instanceof Error && error.message.trim().length > 0 ? error.message : "Failed to write to CalSci REPL.";
        if (message !== this.lastInputError) {
          this.lastInputError = message;
          this.writeLocalLine(`[Input rejected: ${message}]`);
        }
      },
    );
  }

  public dispose(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.writeEmitter.dispose();
  }

  private handleSessionState(state: SessionState): void {
    const port = state.port ?? undefined;
    if (state.connected) {
      this.disconnectedKey = undefined;
      this.lastInputError = undefined;
      if (this.connectedPort !== port) {
        this.connectedPort = port;
        this.writeLocalLine(`[Connected to ${port ?? "CalSci"}]`);
      }
      return;
    }

    const error = state.error?.trim() || undefined;
    const key = `${port ?? ""}|${error ?? ""}|${state.reason ?? ""}`;
    if (this.connectedPort !== undefined || error) {
      if (this.disconnectedKey !== key) {
        this.writeLocalLine(`[Session closed${error ? `: ${error}` : ""}]`);
        this.disconnectedKey = key;
      }
    }
    this.connectedPort = undefined;
  }

  private writeLocalLine(text: string): void {
    this.write(`${text}\n`);
  }

  private write(text: string): void {
    const normalized = this.normalizeTerminalText(text);
    if (!this.opened) {
      this.pendingWrites.push(normalized);
      return;
    }
    this.writeEmitter.fire(normalized);
  }

  private flushPendingWrites(): void {
    if (!this.opened || this.pendingWrites.length === 0) {
      return;
    }
    for (const chunk of this.pendingWrites.splice(0)) {
      this.writeEmitter.fire(chunk);
    }
  }

  private normalizeTerminalText(text: string): string {
    return text.replace(/\r?\n/g, "\r\n");
  }
}

type HybridPanelHandlers = {
  onEnable: () => Promise<void>;
  onDisable: () => Promise<void>;
  onSyncFull: () => Promise<void>;
  onKeyPress: (col: number, row: number) => Promise<void>;
  onDispose: () => void;
};

class CalSciHybridPanel implements vscode.Disposable {
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

class CalSciExtensionController implements vscode.Disposable {
  private readonly backend: BackendServiceClient;
  private readonly statusItem: vscode.StatusBarItem;
  private readonly runItem: vscode.StatusBarItem;
  private readonly runOutput: vscode.OutputChannel;

  private pollTimer: NodeJS.Timeout | undefined;
  private pollInFlight = false;
  private operationInFlight = 0;
  private runInFlight = false;
  private sessionOpenInFlight = false;
  private backendReady = false;

  private devices: DeviceInfo[] = [];
  private selectedPort: string | undefined;
  private sessionState: SessionState = { connected: false };

  private replTerminal: vscode.Terminal | undefined;
  private replPty: CalSciReplPseudoterminal | undefined;
  private replShown = false;
  private hybridPanel: CalSciHybridPanel | undefined;
  private hybridStatus: HybridStatus = { connected: false, active: false };
  private hybridState: HybridState = {};

  private lastSessionAttemptAt = 0;
  private lastSessionAttemptPort: string | undefined;
  private lastSessionError: string | undefined;

  constructor(private readonly context: vscode.ExtensionContext) {
    this.backend = new BackendServiceClient(context);

    this.statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.statusItem.command = "calsci.selectDevice";
    this.statusItem.show();

    this.runItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 99);
    this.runItem.command = "calsci.runCurrentFile";
    this.runItem.text = "$(play) CalSci Run";
    this.runItem.tooltip = "Run active Python file on CalSci";

    this.runOutput = vscode.window.createOutputChannel("CalSci Run");
    this.selectedPort = this.context.globalState.get<string>(SELECTED_PORT_KEY);
    this.setRunVisible(false);

    this.context.subscriptions.push(
      this.backend.onSessionState((state: SessionState) => {
        this.handleSessionStateChange(state);
      }),
      this.backend.onHybridEvent((event: BackendHybridEventPayload) => {
        this.handleHybridEvent(event);
      }),
      vscode.window.onDidCloseTerminal((terminal: vscode.Terminal) => {
        if (terminal !== this.replTerminal) {
          return;
        }
        this.replTerminal = undefined;
        if (this.replPty) {
          this.replPty.dispose();
          this.replPty = undefined;
        }
      }),
    );
  }

  public async start(): Promise<void> {
    this.context.subscriptions.push(this.statusItem, this.runItem, this.runOutput);
    this.registerCommands();
    this.setInitializingStatus();

    try {
      await this.backend.ensureReady();
      this.backendReady = true;
      this.sessionState = await this.backend.getSessionState();
      const hybridSnapshot = await this.backend.getHybridSnapshot();
      this.hybridStatus = hybridSnapshot.status;
      this.hybridState = hybridSnapshot.state;
    } catch (error) {
      this.setNoDeviceStatus("CalSci backend setup failed");
      void vscode.window.showErrorMessage(this.errorMessage(error, "CalSci backend setup failed."));
      return;
    }

    this.refreshStatus();
    await this.pollDevices({ forceSessionConnect: true, showTerminalOnConnect: true });
    this.pollTimer = setInterval(() => {
      void this.pollDevices();
    }, POLL_INTERVAL_MS);
  }

  public dispose(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = undefined;
    }

    if (this.replTerminal) {
      this.replTerminal.dispose();
      this.replTerminal = undefined;
    }
    if (this.replPty) {
      this.replPty.dispose();
      this.replPty = undefined;
    }
    if (this.hybridPanel) {
      this.hybridPanel.dispose();
      this.hybridPanel = undefined;
    }

    this.backend.dispose();
    this.statusItem.dispose();
    this.runItem.dispose();
    this.runOutput.dispose();
  }

  private registerCommands(): void {
    this.context.subscriptions.push(
      vscode.commands.registerCommand("calsci.selectDevice", async () => {
        await this.selectDevice();
      }),
      vscode.commands.registerCommand("calsci.softResetDevice", async () => {
        await this.softResetDevice();
      }),
      vscode.commands.registerCommand("calsci.runCurrentFile", async () => {
        await this.runCurrentFile();
      }),
      vscode.commands.registerCommand("calsci.openHybridPanel", async () => {
        await this.openHybridPanel();
      }),
    );
  }

  private async selectDevice(): Promise<void> {
    if (!this.backendReady) {
      return;
    }

    await this.pollDevices();
    if (this.devices.length === 0) {
      return;
    }

    if (this.devices.length === 1) {
      await this.persistSelectedPort(this.devices[0].port);
      await this.ensureSessionForPort(this.devices[0].port, { force: true, notifyOnError: true, showTerminal: true });
      await this.pollDevices();
      return;
    }

    const picks = this.devices.map((device) => ({
      label: device.port,
      detail: device.description || device.product,
      port: device.port,
    }));

    const choice = await vscode.window.showQuickPick(picks, {
      title: "Select CalSci Device",
      placeHolder: "Choose a CalSci serial port",
      ignoreFocusOut: true,
    });

    if (!choice) {
      return;
    }

    await this.persistSelectedPort(choice.port);
    await this.ensureSessionForPort(choice.port, { force: true, notifyOnError: true, showTerminal: true });
    await this.pollDevices();
    void vscode.window.showInformationMessage(`CalSci device selected: ${choice.port}`);
  }

  private async softResetDevice(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }
    if (this.operationInFlight > 0) {
      void vscode.window.showWarningMessage("CalSci is busy with another operation.");
      return;
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "No CalSci device selected."));
      return;
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
    if (!connected) {
      return;
    }

    const timeout = vscode.workspace.getConfiguration("calsci").get<number>("resetTimeoutSeconds", 5);

    let result: SoftResetResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await this.backend.softReset(port, timeout);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Soft reset failed."));
      return;
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    if (!result.ok) {
      const recovered = await this.restartBackendAndReconnect(port);
      if (!recovered) {
        const detail = result.error ? ` ${result.error}` : "";
        void vscode.window.showErrorMessage(`Soft reset failed on ${port}.${detail}`);
        return;
      }

      try {
        this.operationInFlight += 1;
        this.refreshStatus();
        result = await this.backend.softReset(port, timeout);
      } catch (error) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "Soft reset recovery retry failed."));
        return;
      } finally {
        this.operationInFlight = Math.max(0, this.operationInFlight - 1);
        this.refreshStatus();
        await this.pollDevices();
      }
    }

    if (result.ok) {
      if (result.promptSeen) {
        void vscode.window.showInformationMessage(`Soft reset complete on ${port}. CalSci prompt verified.`);
      } else if (result.rebootSeen) {
        void vscode.window.showInformationMessage(`Soft reset complete on ${port}. Device reboot detected.`);
      } else {
        void vscode.window.showInformationMessage(`Soft reset complete on ${port}.`);
      }
      return;
    }

    const detail = result.error ? ` ${result.error}` : "";
    void vscode.window.showErrorMessage(`Soft reset failed on ${port}.${detail}`);
  }

  private async runCurrentFile(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }
    if (this.runInFlight) {
      void vscode.window.showWarningMessage("A CalSci run is already in progress.");
      return;
    }
    if (this.operationInFlight > 0) {
      void vscode.window.showWarningMessage("CalSci is busy with another operation.");
      return;
    }

    this.runInFlight = true;
    this.setRunButtonBusy(true);

    try {
      let localFile: string | undefined;
      try {
        localFile = await this.resolveLocalFileForRun();
      } catch (error) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "Run aborted."));
        return;
      }
      if (!localFile) {
        return;
      }

      let port: string;
      try {
        port = await this.resolvePortForOperation();
      } catch (error) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "No CalSci device selected."));
        return;
      }

      const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
      if (!connected) {
        return;
      }

      const timeout = vscode.workspace.getConfiguration("calsci").get<number>("runTimeoutSeconds", 0);
      let result: RunFileResult;

      try {
        this.operationInFlight += 1;
        this.refreshStatus();
        result = await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: `CalSci: Running ${path.basename(localFile)} on ${port}`,
            cancellable: true,
          },
          async (_progress, cancelToken) => {
            this.runOutput.clear();
            this.runOutput.appendLine(`CalSci run on ${port}`);
            this.runOutput.appendLine(`File: ${localFile}`);
            this.runOutput.appendLine("");
            this.runOutput.show(false);

            const pendingLines: string[] = [];
            let flushTimer: NodeJS.Timeout | undefined;

            const flush = (): void => {
              if (pendingLines.length === 0) {
                return;
              }
              const batch = pendingLines.splice(0);
              for (const line of batch) {
                this.runOutput.appendLine(line);
              }
            };

            flushTimer = setInterval(flush, 16);

            try {
              return await this.backend.runFileStreaming(
                port,
                localFile,
                timeout,
                (line: string, isError: boolean) => {
                  pendingLines.push(isError ? `[ERROR] ${line}` : line);
                },
                cancelToken,
              );
            } finally {
              if (flushTimer) {
                clearInterval(flushTimer);
              }
              flush();
            }
          },
        );
      } catch (error) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "Run failed."));
        return;
      } finally {
        this.operationInFlight = Math.max(0, this.operationInFlight - 1);
        this.refreshStatus();
      }

      await this.pollDevices();
      this.runOutput.show(false);

      if (result.cancelled) {
        void vscode.window.showInformationMessage(`Run cancelled on ${port}: ${path.basename(localFile)}`);
        return;
      }

      if (!result.ok) {
        const detail = result.error ? ` ${result.error}` : "";
        void vscode.window.showErrorMessage(`Run failed on ${port}.${detail}`);
        return;
      }

      void vscode.window.showInformationMessage(`Run complete on ${port}: ${path.basename(localFile)}`);
    } finally {
      this.runInFlight = false;
      this.setRunButtonBusy(false);
    }
  }

  private async openHybridPanel(): Promise<void> {
    if (this.hybridPanel) {
      this.hybridPanel.reveal();
      this.hybridPanel.updateSnapshot(this.sessionState, this.hybridStatus, this.hybridState);
      return;
    }

    let panel: CalSciHybridPanel;
    panel = new CalSciHybridPanel(this.context, {
      onEnable: async () => {
        await this.startHybridFromPanel();
      },
      onDisable: async () => {
        await this.stopHybridFromPanel(false);
      },
      onSyncFull: async () => {
        await this.syncHybridFullFromPanel();
      },
      onKeyPress: async (col: number, row: number) => {
        await this.sendHybridKeyFromPanel(col, row);
      },
      onDispose: () => {
        if (this.hybridPanel === panel) {
          this.hybridPanel = undefined;
        }
        if (this.hybridStatus.active) {
          void this.stopHybridFromPanel(true);
        }
      },
    });

    this.hybridPanel = panel;
    panel.updateSnapshot(this.sessionState, this.hybridStatus, this.hybridState);
  }

  private async startHybridFromPanel(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "No CalSci device selected."));
      return;
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: false });
    if (!connected) {
      return;
    }

    try {
      const snapshot = await this.backend.startHybrid(port);
      this.applyHybridSnapshot(snapshot);
      if (!snapshot.ok) {
        void vscode.window.showErrorMessage(snapshot.error ?? "Failed to enable hybrid helper mode.");
      }
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Failed to enable hybrid helper mode."));
    }
  }

  private async stopHybridFromPanel(silent: boolean): Promise<void> {
    try {
      const snapshot = await this.backend.stopHybrid();
      this.applyHybridSnapshot(snapshot);
      if (!snapshot.ok && !silent) {
        void vscode.window.showErrorMessage(snapshot.error ?? "Failed to stop hybrid helper mode.");
      }
    } catch (error) {
      if (!silent) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "Failed to stop hybrid helper mode."));
      }
    }
  }

  private async syncHybridFullFromPanel(): Promise<void> {
    try {
      const snapshot = await this.backend.syncHybridFull();
      this.applyHybridSnapshot(snapshot);
      if (!snapshot.ok) {
        void vscode.window.showErrorMessage(snapshot.error ?? "Hybrid full sync failed.");
      }
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Hybrid full sync failed."));
    }
  }

  private async sendHybridKeyFromPanel(col: number, row: number): Promise<void> {
    try {
      const result = await this.backend.sendHybridKey(col, row);
      if (!result.ok) {
        void vscode.window.showErrorMessage(result.error ?? `Hybrid key failed for c${col},r${row}.`);
      }
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Hybrid key failed for c${col},r${row}.`));
    }
  }

  private applyHybridSnapshot(snapshot: HybridSnapshotResult): void {
    this.hybridStatus = { ...snapshot.status };
    this.hybridState = { ...snapshot.state };
    this.hybridPanel?.updateSnapshot(this.sessionState, this.hybridStatus, this.hybridState);
  }

  private async resolveLocalFileForRun(): Promise<string | undefined> {
    const editor = vscode.window.activeTextEditor;

    if (editor && editor.document.uri.scheme === "file" && path.extname(editor.document.fileName).toLowerCase() === ".py") {
      if (editor.document.isDirty) {
        const saved = await editor.document.save();
        if (!saved) {
          throw new Error("File save cancelled. Run aborted.");
        }
      }
      return editor.document.fileName;
    }

    const picked = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false,
      openLabel: "Run on CalSci",
      title: "CalSci: Select Python File",
      filters: { Python: ["py"] },
    });

    if (!picked || picked.length === 0) {
      return undefined;
    }
    return picked[0].fsPath;
  }

  private async resolvePortForOperation(): Promise<string> {
    await this.pollDevices();

    if (this.devices.length === 0) {
      throw new Error("No CalSci device found.");
    }

    const selected = this.devices.find((device) => device.port === this.selectedPort);
    if (selected) {
      return selected.port;
    }

    if (this.devices.length === 1) {
      await this.persistSelectedPort(this.devices[0].port);
      return this.devices[0].port;
    }

    const picks = this.devices.map((device) => ({
      label: device.port,
      detail: device.description || device.product,
      port: device.port,
    }));

    const choice = await vscode.window.showQuickPick(picks, {
      title: "Select CalSci Device",
      placeHolder: "Choose device for this operation",
      ignoreFocusOut: true,
    });

    if (!choice) {
      throw new Error("Operation cancelled. No device selected.");
    }

    await this.persistSelectedPort(choice.port);
    return choice.port;
  }

  private async pollDevices(options?: PollOptions): Promise<void> {
    if (!this.backendReady || this.pollInFlight || this.operationInFlight > 0 || this.sessionOpenInFlight) {
      return;
    }

    this.pollInFlight = true;
    try {
      const result = await this.backend.scan();
      if (!result.ok) {
        this.devices = [];
        if (!this.sessionState.connected) {
          await this.persistSelectedPort(undefined);
        }
        this.refreshStatus();
        return;
      }

      this.devices = result.devices ?? [];
      await this.reconcileSelectedPort();

      if (!this.selectedPort) {
        if (this.sessionState.connected) {
          await this.closeSessionSilently();
        }
        this.refreshStatus();
        return;
      }

      if (options?.forceSessionConnect || this.shouldAttemptSession(this.selectedPort)) {
        await this.ensureSessionForSelection({
          force: Boolean(options?.forceSessionConnect),
          notifyOnError: false,
          showTerminal: Boolean(options?.showTerminalOnConnect),
        });
      }

      this.refreshStatus();
    } catch {
      this.devices = [];
      this.refreshStatus();
    } finally {
      this.pollInFlight = false;
    }
  }

  private async reconcileSelectedPort(): Promise<void> {
    if (this.selectedPort) {
      const stillAvailable = this.devices.some((device) => device.port === this.selectedPort);
      if (stillAvailable) {
        return;
      }
      await this.persistSelectedPort(undefined);
    }

    if (this.devices.length > 0) {
      await this.persistSelectedPort(this.devices[0].port);
    }
  }

  private async persistSelectedPort(port: string | undefined): Promise<void> {
    if (this.selectedPort === port) {
      return;
    }
    this.selectedPort = port;
    await this.context.globalState.update(SELECTED_PORT_KEY, port);
    this.refreshStatus();
  }

  private async ensureSessionForPort(port: string, options: EnsureSessionOptions): Promise<boolean> {
    if (this.selectedPort !== port) {
      await this.persistSelectedPort(port);
    }
    return this.ensureSessionForSelection(options);
  }

  private async ensureSessionForSelection(options: EnsureSessionOptions): Promise<boolean> {
    if (!this.backendReady || !this.selectedPort) {
      return false;
    }

    const selectedAvailable = this.devices.some((device) => device.port === this.selectedPort);
    if (!selectedAvailable) {
      this.refreshStatus();
      return false;
    }

    if (!options.force && this.sessionState.connected && this.sessionState.port === this.selectedPort) {
      if (options.showTerminal) {
        this.showReplTerminal(true);
      }
      return true;
    }

    if (this.sessionOpenInFlight) {
      return false;
    }

    this.sessionOpenInFlight = true;
    this.lastSessionAttemptAt = Date.now();
    this.lastSessionAttemptPort = this.selectedPort;
    this.ensureReplTerminal();
    if (options.showTerminal || !this.replShown) {
      this.showReplTerminal(true);
    }
    this.refreshStatus();

    try {
      const result = await this.backend.openSession(this.selectedPort);
      this.sessionState = result;
      if (result.ok && result.connected && result.port === this.selectedPort) {
        this.lastSessionError = undefined;
        this.refreshStatus();
        return true;
      }

      const message = result.error ?? `Failed to open CalSci session on ${this.selectedPort}.`;
      this.sessionState = { connected: false, error: message, reason: result.reason };
      this.lastSessionError = message;
      if (options.notifyOnError) {
        void vscode.window.showErrorMessage(message);
      }
      this.refreshStatus();
      return false;
    } catch (error) {
      const message = this.errorMessage(error, `Failed to open CalSci session on ${this.selectedPort}.`);
      this.sessionState = { connected: false, error: message };
      this.lastSessionError = message;
      if (options.notifyOnError) {
        void vscode.window.showErrorMessage(message);
      }
      this.refreshStatus();
      return false;
    } finally {
      this.sessionOpenInFlight = false;
      this.refreshStatus();
    }
  }

  private shouldAttemptSession(port: string): boolean {
    if (this.sessionOpenInFlight) {
      return false;
    }
    if (this.sessionState.connected) {
      return this.sessionState.port !== port;
    }
    if (this.lastSessionAttemptPort !== port) {
      return true;
    }
    return Date.now() - this.lastSessionAttemptAt >= SESSION_RETRY_BACKOFF_MS;
  }

  private async restartBackendAndReconnect(port: string): Promise<boolean> {
    try {
      await this.backend.restartService();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "CalSci backend restart failed."));
      return false;
    }

    this.sessionState = { connected: false };
    this.lastSessionError = undefined;
    this.refreshStatus();
    return this.ensureSessionForPort(port, { force: true, notifyOnError: false, showTerminal: true });
  }

  private async closeSessionSilently(): Promise<void> {
    try {
      await this.backend.closeSession();
    } catch {
      // Best effort only.
    }
    this.sessionState = { connected: false };
    this.refreshStatus();
  }

  private handleSessionStateChange(state: SessionState): void {
    this.sessionState = {
      connected: state.connected,
      port: state.port ?? undefined,
      error: state.error?.trim() || undefined,
      reason: state.reason?.trim() || undefined,
    };
    if (this.sessionState.connected) {
      this.lastSessionError = undefined;
      this.hybridStatus = {
        ...this.hybridStatus,
        connected: true,
        port: this.sessionState.port,
        error: undefined,
      };
    } else if (this.sessionState.error) {
      this.lastSessionError = this.sessionState.error;
      this.hybridStatus = {
        ...this.hybridStatus,
        connected: false,
        active: false,
        port: undefined,
        error: this.sessionState.error,
      };
    } else {
      this.hybridStatus = {
        ...this.hybridStatus,
        connected: false,
        active: false,
        port: undefined,
      };
    }
    this.hybridPanel?.updateSessionState(this.sessionState);
    this.hybridPanel?.updateHybridStatus(this.hybridStatus);
    this.refreshStatus();
  }

  private handleHybridEvent(event: BackendHybridEventPayload): void {
    if (event.type === "status") {
      const { type: _type, ...status } = event;
      this.hybridStatus = {
        ...this.hybridStatus,
        ...status,
      };
      this.hybridPanel?.updateHybridStatus(this.hybridStatus);
      return;
    }

    this.hybridState = mergeHybridState(this.hybridState, event.state);
    this.hybridPanel?.updateHybridState(event.state);
  }

  private ensureReplTerminal(): vscode.Terminal {
    if (this.replTerminal) {
      return this.replTerminal;
    }

    const pty = new CalSciReplPseudoterminal(this.backend);
    const terminal = vscode.window.createTerminal({
      name: "CalSci REPL",
      pty,
    });

    this.replPty = pty;
    this.replTerminal = terminal;
    this.context.subscriptions.push(pty, terminal);
    return terminal;
  }

  private showReplTerminal(preserveFocus: boolean): void {
    const terminal = this.ensureReplTerminal();
    terminal.show(preserveFocus);
    this.replShown = true;
  }

  private refreshStatus(): void {
    if (!this.backendReady) {
      this.setInitializingStatus();
      return;
    }

    if (this.selectedPort && this.sessionState.connected && this.sessionState.port === this.selectedPort) {
      this.setConnectedStatus(this.selectedPort);
      return;
    }

    if (!this.selectedPort) {
      this.setNoDeviceStatus("No CalSci device selected.");
      return;
    }

    const selectedAvailable = this.devices.some((device) => device.port === this.selectedPort);
    if (!selectedAvailable) {
      this.setNoDeviceStatus(`Selected device ${this.selectedPort} is not available.`);
      return;
    }

    if (this.sessionOpenInFlight || this.operationInFlight > 0) {
      this.setConnectingStatus(this.selectedPort);
      return;
    }

    const reason = this.lastSessionError ?? "Selected device is available. Opening persistent REPL session.";
    this.setSelectedStatus(this.selectedPort, reason);
  }

  private setInitializingStatus(): void {
    this.statusItem.text = "$(sync~spin) CalSci: initializing";
    this.statusItem.color = undefined;
    this.statusItem.tooltip = "Preparing CalSci runtime";
    this.setRunVisible(false);
  }

  private setConnectedStatus(port: string): void {
    this.statusItem.text = `$(plug) Connected: ${port}`;
    this.statusItem.color = new vscode.ThemeColor("terminal.ansiGreen");
    this.statusItem.tooltip = `CalSci REPL session active on ${port}`;
    this.setRunVisible(true);
  }

  private setConnectingStatus(port: string): void {
    this.statusItem.text = `$(sync~spin) Connecting: ${port}`;
    this.statusItem.color = undefined;
    this.statusItem.tooltip = `Opening CalSci REPL session on ${port}`;
    this.setRunVisible(true);
  }

  private setSelectedStatus(port: string, reason: string): void {
    this.statusItem.text = `$(plug) Selected: ${port}`;
    this.statusItem.color = new vscode.ThemeColor("terminal.ansiYellow");
    this.statusItem.tooltip = reason;
    this.setRunVisible(true);
  }

  private setNoDeviceStatus(reason: string): void {
    this.statusItem.text = "$(circle-slash) No device";
    this.statusItem.color = new vscode.ThemeColor("terminal.ansiRed");
    this.statusItem.tooltip = reason;
    this.setRunVisible(false);
  }

  private setRunVisible(visible: boolean): void {
    if (visible) {
      this.runItem.show();
      return;
    }
    this.runItem.hide();
  }

  private setRunButtonBusy(busy: boolean): void {
    if (busy) {
      this.runItem.text = "$(sync~spin) CalSci Running";
      this.runItem.tooltip = "CalSci run in progress";
      this.runItem.command = undefined;
      return;
    }
    this.runItem.text = "$(play) CalSci Run";
    this.runItem.tooltip = "Run active Python file on CalSci";
    this.runItem.command = "calsci.runCurrentFile";
  }

  private errorMessage(error: unknown, fallback: string): string {
    if (error instanceof Error && error.message.trim().length > 0) {
      return error.message;
    }
    return fallback;
  }
}

let controller: CalSciExtensionController | undefined;

export function activate(context: vscode.ExtensionContext): void {
  controller = new CalSciExtensionController(context);
  void controller.start();
}

export function deactivate(): void {
  if (controller) {
    controller.dispose();
    controller = undefined;
  }
}
