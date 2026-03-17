import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import {
  MAX_SYNC_FOLDER_HISTORY,
  POLL_INTERVAL_MS,
  SELECTED_PORT_KEY,
  SESSION_RETRY_BACKOFF_MS,
  SYNC_FOLDER_HISTORY_KEY,
  mergeHybridState,
  type BackendHybridEventPayload,
  type DeviceInfo,
  type EnsureSessionOptions,
  type HybridSnapshotResult,
  type HybridState,
  type HybridStatus,
  type PollOptions,
  type RunFileResult,
  type RunInteractiveFileResult,
  type SessionState,
  type SoftResetResult,
  type SyncFolderResult,
  type SyncFolderSelection,
} from "../core/shared";
import { BackendServiceClient } from "../backend/backendServiceClient";
import { CalSciReplPseudoterminal } from "../ui/replTerminal";
import { CalSciHybridPanel } from "../ui/hybridPanel";

export class CalSciExtensionController implements vscode.Disposable {
  private readonly backend: BackendServiceClient;
  private readonly statusItem: vscode.StatusBarItem;
  private readonly runItem: vscode.StatusBarItem;
  private readonly runInteractiveItem: vscode.StatusBarItem;
  private readonly runOutput: vscode.OutputChannel;
  private readonly syncOutput: vscode.OutputChannel;

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
    this.runItem.text = "$(play) Run Non-Interactive";
    this.runItem.tooltip = "Run active Python file on CalSci through raw REPL";

    this.runInteractiveItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 98);
    this.runInteractiveItem.command = "calsci.runInteractiveFile";
    this.runInteractiveItem.text = "$(terminal) Run Interactive";
    this.runInteractiveItem.tooltip = "Run active Python file on CalSci through the normal REPL";

    this.runOutput = vscode.window.createOutputChannel("Run Non-Interactive File");
    this.syncOutput = vscode.window.createOutputChannel("CalSci Folder Sync");
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
    this.context.subscriptions.push(this.statusItem, this.runItem, this.runInteractiveItem, this.runOutput, this.syncOutput);
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
    this.runInteractiveItem.dispose();
    this.runOutput.dispose();
    this.syncOutput.dispose();
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
      vscode.commands.registerCommand("calsci.runInteractiveFile", async () => {
        await this.runInteractiveFile();
      }),
      vscode.commands.registerCommand("calsci.syncFolder", async (uri?: vscode.Uri) => {
        await this.syncFolderCommand(uri);
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
      void vscode.window.showWarningMessage("A CalSci non-interactive run is already in progress.");
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
            title: `CalSci: Running ${path.basename(localFile)} non-interactively on ${port}`,
            cancellable: true,
          },
          async (_progress, cancelToken) => {
            this.runOutput.clear();
            this.runOutput.appendLine(`CalSci non-interactive run on ${port}`);
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
        void vscode.window.showInformationMessage(`Non-interactive run cancelled on ${port}: ${path.basename(localFile)}`);
        return;
      }

      if (!result.ok) {
        const detail = result.error ? ` ${result.error}` : "";
        void vscode.window.showErrorMessage(`Non-interactive run failed on ${port}.${detail}`);
        return;
      }

      void vscode.window.showInformationMessage(`Non-interactive run complete on ${port}: ${path.basename(localFile)}`);
    } finally {
      this.runInFlight = false;
      this.setRunButtonBusy(false);
    }
  }

  private async runInteractiveFile(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }
    if (this.runInFlight) {
      void vscode.window.showWarningMessage("A CalSci non-interactive run is already in progress.");
      return;
    }
    if (this.operationInFlight > 0) {
      void vscode.window.showWarningMessage("CalSci is busy with another operation.");
      return;
    }

    let localFile: string | undefined;
    try {
      localFile = await this.resolveLocalFileForRun();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Interactive run aborted."));
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

    let result: RunInteractiveFileResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      this.showReplTerminal(false);
      result = await this.backend.runFileInteractive(port, localFile);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Interactive run failed."));
      return;
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
    }

    await this.pollDevices();

    if (!result.ok) {
      const detail = result.error ? ` ${result.error}` : "";
      void vscode.window.showErrorMessage(`Interactive run failed on ${port}.${detail}`);
      return;
    }

    this.showReplTerminal(false);
    void vscode.window.showInformationMessage(`Interactive run started on ${port}: ${path.basename(localFile)}`);
  }

  private async syncFolderCommand(folderUri?: vscode.Uri): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }
    if (this.runInFlight) {
      void vscode.window.showWarningMessage("CalSci is busy with a run operation.");
      return;
    }
    if (this.operationInFlight > 0) {
      void vscode.window.showWarningMessage("CalSci is busy with another operation.");
      return;
    }

    let selection: SyncFolderSelection | undefined;
    try {
      selection = await this.resolveFolderForSync(folderUri);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Folder sync aborted."));
      return;
    }
    if (!selection) {
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

    let result: SyncFolderResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `CalSci: Syncing ${path.basename(selection.localFolder)} to ${port}`,
          cancellable: false,
        },
        async (progress) => {
          this.syncOutput.clear();
          this.syncOutput.appendLine(`CalSci folder sync on ${port}`);
          this.syncOutput.appendLine(`Local:  ${selection.localFolder}`);
          this.syncOutput.appendLine(`Remote: ${selection.remoteFolder}`);
          this.syncOutput.appendLine(
            `Mode:   ${selection.deleteExtraneous ? "mirror sync (delete stale remote files)" : "upload only"}`,
          );
          this.syncOutput.appendLine("");
          this.syncOutput.show(false);

          return this.backend.syncFolder(
            port,
            selection.localFolder,
            selection.remoteFolder,
            selection.deleteExtraneous,
            (line: string, isError: boolean) => {
              const formatted = isError ? `[ERROR] ${line}` : line;
              this.syncOutput.appendLine(formatted);
              progress.report({ message: formatted.slice(0, 100) });
            },
          );
        },
      );
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Folder sync failed."));
      return;
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    this.syncOutput.show(false);

    if (!result.ok) {
      const detail = result.error ? ` ${result.error}` : "";
      void vscode.window.showErrorMessage(`Folder sync failed on ${port}.${detail}`);
      return;
    }

    await this.rememberSyncFolder(selection.localFolder);
    const fileCount = result.filesSynced ?? 0;
    const deletedCount = result.filesDeleted ?? 0;
    const skippedCount = result.filesSkipped ?? 0;
    const totalBytes = result.bytesSynced ?? 0;
    void vscode.window.showInformationMessage(
      `CalSci sync complete: ${fileCount} uploaded, ${deletedCount} deleted, ${skippedCount} skipped to ${result.remoteFolder} (${this.formatByteCount(totalBytes)} sent).`,
    );
  }

  private async resolveFolderForSync(folderUri?: vscode.Uri): Promise<SyncFolderSelection | undefined> {
    if (folderUri?.scheme === "file" && await this.isDirectoryPath(folderUri.fsPath)) {
      return this.buildSyncFolderSelection(folderUri.fsPath);
    }

    const history = await this.loadSyncFolderHistory();
    if (history.length === 0) {
      const localFolder = await this.pickFolderFromDialog();
      if (!localFolder) {
        return undefined;
      }
      return this.buildSyncFolderSelection(localFolder);
    }

    const picks: Array<vscode.QuickPickItem & { folderPath?: string; browse?: boolean }> = [
      {
        label: "$(folder-opened) Choose Folder…",
        detail: "Browse for a local folder to sync to CalSci.",
        browse: true,
      },
    ];
    for (const folderPath of history) {
      const selection = await this.buildSyncFolderSelection(folderPath);
      picks.push({
        label: `$(history) ${path.basename(folderPath)}`,
        description: folderPath,
        detail: `${selection.deleteExtraneous ? "Mirror sync" : "Upload only"} -> ${selection.remoteFolder}`,
        folderPath,
      });
    }

    const choice = await vscode.window.showQuickPick(picks, {
      title: "CalSci: Sync Folder",
      placeHolder: "Choose a recent folder or browse for another folder",
      ignoreFocusOut: true,
    });
    if (!choice) {
      return undefined;
    }

    const localFolder = choice.browse ? await this.pickFolderFromDialog() : choice.folderPath;
    if (!localFolder) {
      return undefined;
    }
    return this.buildSyncFolderSelection(localFolder);
  }

  private async pickFolderFromDialog(): Promise<string | undefined> {
    const workspaceFolder = vscode.workspace.workspaceFolders?.find((folder) => folder.uri.scheme === "file");
    const picked = await vscode.window.showOpenDialog({
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      defaultUri: workspaceFolder?.uri,
      openLabel: "Sync to CalSci",
      title: "CalSci: Select Folder to Sync",
    });
    if (!picked || picked.length === 0) {
      return undefined;
    }
    return path.resolve(picked[0].fsPath);
  }

  private async loadSyncFolderHistory(): Promise<string[]> {
    const stored = this.context.globalState.get<string[]>(SYNC_FOLDER_HISTORY_KEY) ?? [];
    const existing: string[] = [];
    for (const candidate of stored) {
      const resolved = path.resolve(candidate);
      if (existing.includes(resolved)) {
        continue;
      }
      if (await this.isDirectoryPath(resolved)) {
        existing.push(resolved);
      }
    }
    if (existing.length !== stored.length) {
      await this.context.globalState.update(SYNC_FOLDER_HISTORY_KEY, existing);
    }
    return existing;
  }

  private async rememberSyncFolder(folderPath: string): Promise<void> {
    const resolved = path.resolve(folderPath);
    const history = await this.loadSyncFolderHistory();
    const next = [resolved, ...history.filter((entry) => entry !== resolved)].slice(0, MAX_SYNC_FOLDER_HISTORY);
    await this.context.globalState.update(SYNC_FOLDER_HISTORY_KEY, next);
  }

  private async buildSyncFolderSelection(folderPath: string): Promise<SyncFolderSelection> {
    const localFolder = path.resolve(folderPath);
    const workspaceTarget = this.computeWorkspaceRemoteFolderTarget(localFolder);
    if (workspaceTarget) {
      return {
        localFolder,
        remoteFolder: workspaceTarget,
        deleteExtraneous: true,
      };
    }

    const directToRoot = await this.hasDeviceRootEntrypoint(localFolder);
    return {
      localFolder,
      remoteFolder: directToRoot ? "/" : `/${path.basename(localFolder)}`,
      deleteExtraneous: false,
    };
  }

  private computeWorkspaceRemoteFolderTarget(localFolder: string): string | undefined {
    const resolvedFolder = path.resolve(localFolder);
    for (const workspaceFolder of vscode.workspace.workspaceFolders ?? []) {
      if (workspaceFolder.uri.scheme !== "file") {
        continue;
      }
      const workspacePath = path.resolve(workspaceFolder.uri.fsPath);
      const relative = path.relative(workspacePath, resolvedFolder);
      if (relative === "") {
        return "/";
      }
      if (!relative.startsWith("..") && !path.isAbsolute(relative)) {
        return `/${relative.split(path.sep).join("/")}`;
      }
    }
    return undefined;
  }

  private async hasDeviceRootEntrypoint(localFolder: string): Promise<boolean> {
    for (const entrypoint of ["boot.py", "main.py"]) {
      if (await this.isFilePath(path.join(localFolder, entrypoint))) {
        return true;
      }
    }
    return false;
  }

  private async isFilePath(targetPath: string): Promise<boolean> {
    try {
      const stat = await fs.promises.stat(targetPath);
      return stat.isFile();
    } catch {
      return false;
    }
  }

  private async isDirectoryPath(targetPath: string): Promise<boolean> {
    try {
      const stat = await fs.promises.stat(targetPath);
      return stat.isDirectory();
    } catch {
      return false;
    }
  }

  private formatByteCount(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
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
      onSyncFolder: async () => {
        await this.syncFolderCommand();
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
      name: "CalSci",
      iconPath: new vscode.ThemeIcon("chip"),
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
    this.statusItem.tooltip = `CalSci session active on ${port}`;
    this.setRunVisible(true);
  }

  private setConnectingStatus(port: string): void {
    this.statusItem.text = `$(sync~spin) Connecting: ${port}`;
    this.statusItem.color = undefined;
    this.statusItem.tooltip = `Opening CalSci session on ${port}`;
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
      this.runInteractiveItem.show();
      return;
    }
    this.runItem.hide();
    this.runInteractiveItem.hide();
  }

  private setRunButtonBusy(busy: boolean): void {
    if (busy) {
      this.runItem.text = "$(sync~spin) Running Non-Interactive";
      this.runItem.tooltip = "CalSci non-interactive run in progress";
      this.runItem.command = undefined;
      this.runInteractiveItem.command = undefined;
      return;
    }
    this.runItem.text = "$(play) Run Non-Interactive";
    this.runItem.tooltip = "Run active Python file on CalSci through raw REPL";
    this.runItem.command = "calsci.runCurrentFile";
    this.runInteractiveItem.command = "calsci.runInteractiveFile";
    this.runInteractiveItem.text = "$(terminal) Run Interactive";
    this.runInteractiveItem.tooltip = "Run active Python file on CalSci through the normal REPL";
  }

  private errorMessage(error: unknown, fallback: string): string {
    if (error instanceof Error && error.message.trim().length > 0) {
      return error.message;
    }
    return fallback;
  }
}
