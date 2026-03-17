import * as fs from "fs";
import * as path from "path";
import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as readline from "readline";
import * as vscode from "vscode";

import {
  BACKEND_TIMEOUT_BUFFER_SEC,
  type BackendHybridEventPayload,
  type BackendMessage,
  type PendingBackendRequest,
  type ProcessResult,
  type RunCancelResult,
  type RunFileResult,
  type RunInteractiveFileResult,
  type ScanResult,
  type SessionResult,
  type SessionState,
  type SoftResetResult,
  type SyncFolderResult,
  type TerminalWriteResult,
  type HybridSnapshotResult,
  type HybridKeyResult,
} from "../core/shared";

export class BackendServiceClient implements vscode.Disposable {
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
      throw new Error(result.error ?? "Failed to write to CalSci.");
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

  public async runFileInteractive(port: string, localFile: string): Promise<RunInteractiveFileResult> {
    return this.request<RunInteractiveFileResult>("run-file-interactive", {
      port,
      localFile,
    });
  }

  public async syncFolder(
    port: string,
    localFolder: string,
    remoteFolder: string,
    deleteExtraneous: boolean,
    onOutputLine: (line: string, isError: boolean) => void,
  ): Promise<SyncFolderResult> {
    return this.request<SyncFolderResult>(
      "sync-folder",
      {
        port,
        localFolder,
        remoteFolder,
        deleteExtraneous,
      },
      {
        stream: true,
        onStream: onOutputLine,
      },
    );
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
