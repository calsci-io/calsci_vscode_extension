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

type BackendMessage =
  | BackendReadyMessage
  | BackendStreamMessage
  | BackendResultMessage
  | BackendTerminalOutputEventMessage
  | BackendSessionEventMessage;

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

class BackendServiceClient implements vscode.Disposable {
  private readonly storagePath: string;
  private readonly venvPath: string;
  private readonly backendScriptPath: string;

  private readonly terminalOutputEmitter = new vscode.EventEmitter<string>();
  private readonly sessionStateEmitter = new vscode.EventEmitter<SessionState>();

  private venvPythonPath: string | undefined;
  private serviceProcess: ChildProcessWithoutNullStreams | undefined;
  private serviceReader: readline.Interface | undefined;
  private serviceStartPromise: Promise<void> | undefined;
  private serviceStderr = "";

  private nextRequestId = 1;
  private readonly pendingRequests = new Map<string, PendingBackendRequest<unknown>>();

  public readonly onTerminalOutput = this.terminalOutputEmitter.event;
  public readonly onSessionState = this.sessionStateEmitter.event;

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
    const normalizedTimeout = Math.max(1, timeoutSeconds);
    const backendTimeoutMs = (normalizedTimeout + BACKEND_TIMEOUT_BUFFER_SEC) * 1000;

    return new Promise((resolve) => {
      let settled = false;

      const finish = (result: RunFileResult): void => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeoutHandle);
        cancelDisposable.dispose();
        resolve(result);
      };

      const timeoutHandle = setTimeout(() => {
        const reason = `Run timed out after ${backendTimeoutMs}ms`;
        this.stopService(reason);
        finish({ ok: false, port, localFile, output: "", error: reason });
      }, backendTimeoutMs);

      const cancelDisposable = cancelToken.onCancellationRequested(() => {
        const reason = "Run cancelled by user";
        this.stopService(reason);
        finish({ ok: false, port, localFile, output: "", error: reason });
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

      const timeout = vscode.workspace.getConfiguration("calsci").get<number>("runTimeoutSeconds", 300);
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
    } else if (this.sessionState.error) {
      this.lastSessionError = this.sessionState.error;
    }
    this.refreshStatus();
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
