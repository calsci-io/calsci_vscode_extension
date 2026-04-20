import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

import {
  type ChipEraseResult,
  type ClearAllFilesResult,
  MAX_SYNC_FOLDER_HISTORY,
  POLL_INTERVAL_MS,
  SELECTED_PORT_KEY,
  SESSION_RETRY_BACKOFF_MS,
  SYNC_FOLDER_HISTORY_KEY,
  type FirmwareFlashResult,
  type FirmwareFlashPaths,
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
  type WorkspaceCreateDirectoryResult,
  type WorkspaceDeleteResult,
  type WorkspaceDirectoryEntry,
  type WorkspaceDirectoryResult,
  type WorkspaceImportResult,
  type WorkspaceRenameResult,
  type SyncFolderResult,
  type SyncFolderSelection,
  type WorkspaceStat,
  type WorkspaceStatResult,
  type WorkspaceTreeEntry,
  type WorkspaceTreeResult,
  type WorkspaceWriteFileResult,
} from "../core/shared";
import { BackendServiceClient } from "../backend/backendServiceClient";
import { CalSciReplPseudoterminal } from "../ui/replTerminal";
import { CalSciActionsViewProvider } from "../ui/actionsView";
import { CalSciHybridPanel } from "../ui/hybridPanel";
import { CalSciTestingFolderViewProvider } from "../ui/testingFolderView";
import {
  CalSciWorkspaceFileSystemProvider,
  createCalSciWorkspaceChildUri,
  createCalSciWorkspaceError,
  createCalSciWorkspaceUri,
  getCalSciWorkspaceErrorCode,
  getCalSciWorkspaceParentUri,
  normalizeCalSciRemotePath,
  parseCalSciWorkspaceUri,
} from "../ui/workspaceFileSystemProvider";
import { CalSciWorkspaceItem, CalSciWorkspaceViewProvider } from "../ui/workspaceView";

const MAX_PROGRESS_MESSAGE_LENGTH = 100;
const SESSION_OPEN_WAIT_MS = 5000;

type WorkspaceCommandTarget = {
  remotePath?: string;
  port?: string;
  kind?: "file" | "folder" | "placeholder";
};

export class CalSciExtensionController implements vscode.Disposable {
  private readonly backend: BackendServiceClient;
  private readonly statusItem: vscode.StatusBarItem;
  private readonly runItem: vscode.StatusBarItem;
  private readonly runInteractiveItem: vscode.StatusBarItem;
  private readonly workspaceSyncItem: vscode.StatusBarItem;
  private readonly runOutput: vscode.OutputChannel;
  private readonly syncOutput: vscode.OutputChannel;
  private readonly cleanupOutput: vscode.OutputChannel;
  private readonly firmwareOutput: vscode.OutputChannel;
  private readonly chipEraseOutput: vscode.OutputChannel;
  private readonly workspaceOutput: vscode.OutputChannel;
  private readonly workspaceFetchOutput: vscode.OutputChannel;
  private readonly workspaceTreeView: vscode.TreeView<CalSciWorkspaceItem>;
  private readonly workspaceViewProvider: CalSciWorkspaceViewProvider;
  private readonly testingFolderViewProvider: CalSciTestingFolderViewProvider;
  private readonly workspaceFileSystemProvider: CalSciWorkspaceFileSystemProvider;

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
  private hybridPanel: CalSciHybridPanel | undefined;
  private hybridStatus: HybridStatus = { connected: false, active: false };
  private hybridState: HybridState = {};
  private disposing = false;

  private lastSessionAttemptAt = 0;
  private lastSessionAttemptPort: string | undefined;
  private lastSessionError: string | undefined;
  private pendingHybridRestorePort: string | undefined;
  private hybridRestoreInFlight = false;
  private recentTerminalOutput = "";
  private terminalInteractionInFlight = false;
  private terminalInputQueue: Promise<void> = Promise.resolve();
  private disconnectHandlingInFlight = false;
  private activeWorkspaceTarget: WorkspaceCommandTarget | undefined;
  private workspaceClipboardSource: vscode.Uri | undefined;
  private lastWorkspaceDestinationFolder: string | undefined;
  private readonly workspaceSyncState = new Map<string, "pending" | "synced">();

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

    this.workspaceSyncItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 97);
    this.workspaceSyncItem.hide();

    this.runOutput = vscode.window.createOutputChannel("Run Non-Interactive File");
    this.syncOutput = vscode.window.createOutputChannel("CalSci Folder Sync");
    this.cleanupOutput = vscode.window.createOutputChannel("CalSci Clear All Files");
    this.firmwareOutput = vscode.window.createOutputChannel("CalSci Firmware Upload");
    this.chipEraseOutput = vscode.window.createOutputChannel("CalSci Chip Erase");
    this.workspaceOutput = vscode.window.createOutputChannel("CalSci Workspace");
    this.workspaceFetchOutput = vscode.window.createOutputChannel("CalSci Workspace Fetch");
    this.workspaceFileSystemProvider = new CalSciWorkspaceFileSystemProvider({
      stat: async (uri: vscode.Uri) => this.statWorkspaceUri(uri),
      readDirectory: async (uri: vscode.Uri) => this.readWorkspaceDirectoryUri(uri),
      readFile: async (uri: vscode.Uri) => this.readWorkspaceFileUri(uri),
      writeFile: async (uri: vscode.Uri, content: Uint8Array, options) => this.writeWorkspaceFileUri(uri, content, options),
      createDirectory: async (uri: vscode.Uri) => this.createWorkspaceDirectoryUri(uri),
      delete: async (uri: vscode.Uri, options) => this.deleteWorkspaceEntryUri(uri, options.recursive),
      rename: async (oldUri: vscode.Uri, newUri: vscode.Uri, options) => this.renameWorkspaceEntryUri(oldUri, newUri, options.overwrite),
    });
    this.workspaceViewProvider = new CalSciWorkspaceViewProvider({
      scanTree: async () => this.scanWorkspaceTree(),
      shouldAutoLoad: () => this.shouldAutoScanWorkspace(),
    });
    this.workspaceTreeView = vscode.window.createTreeView("calsci.workspaceView", {
      treeDataProvider: this.workspaceViewProvider,
      manageCheckboxStateManually: true,
    });
    this.testingFolderViewProvider = new CalSciTestingFolderViewProvider({
      getWorkspaceFolder: () => vscode.workspace.workspaceFolders?.find((folder) => folder.uri.scheme === "file"),
    });
    this.selectedPort = this.context.globalState.get<string>(SELECTED_PORT_KEY);
    this.setRunVisible(false);
    void this.setWorkspaceClipboard(undefined);
    void this.updateWorkspaceFetchState();

    this.context.subscriptions.push(
      this.backend.onTerminalOutput((data: string) => {
        this.handleTerminalOutput(data);
      }),
      this.backend.onSessionState((state: SessionState) => {
        this.handleSessionStateChange(state);
      }),
      this.backend.onHybridEvent((event: BackendHybridEventPayload) => {
        this.handleHybridEvent(event);
      }),
      vscode.workspace.registerFileSystemProvider("calsci", this.workspaceFileSystemProvider, {
        isCaseSensitive: true,
      }),
      vscode.window.registerTreeDataProvider("calsci.actionsView", new CalSciActionsViewProvider()),
      this.workspaceTreeView,
      vscode.window.registerTreeDataProvider("calsci.testingView", this.testingFolderViewProvider),
      this.workspaceViewProvider.onDidChangeFetchState(() => {
        void this.updateWorkspaceFetchState();
      }),
      this.workspaceTreeView.onDidChangeCheckboxState((event: vscode.TreeCheckboxChangeEvent<CalSciWorkspaceItem>) => {
        this.workspaceViewProvider.handleCheckboxStateChange(event.items);
      }),
      this.workspaceTreeView.onDidChangeSelection((event: vscode.TreeViewSelectionChangeEvent<CalSciWorkspaceItem>) => {
        this.activeWorkspaceTarget = this.toWorkspaceCommandTarget(event.selection[0]);
      }),
      this.workspaceFileSystemProvider.onDidChangeFile((events: readonly vscode.FileChangeEvent[]) => {
        const hasStructuralChange = events.some((event) => event.type !== vscode.FileChangeType.Changed);
        if (!hasStructuralChange) {
          return;
        }
        this.workspaceViewProvider.invalidate(true);
      }),
      vscode.workspace.onDidChangeWorkspaceFolders(() => {
        this.testingFolderViewProvider.invalidate();
      }),
      vscode.workspace.onDidCreateFiles(() => {
        this.testingFolderViewProvider.invalidate();
      }),
      vscode.workspace.onDidDeleteFiles(() => {
        this.testingFolderViewProvider.invalidate();
      }),
      vscode.workspace.onDidRenameFiles(() => {
        this.testingFolderViewProvider.invalidate();
      }),
      vscode.workspace.onDidChangeTextDocument((event: vscode.TextDocumentChangeEvent) => {
        this.handleWorkspaceTextChanged(event);
      }),
      vscode.workspace.onDidCloseTextDocument((document: vscode.TextDocument) => {
        this.workspaceSyncState.delete(document.uri.toString());
        this.updateWorkspaceSyncStatus();
      }),
      vscode.window.onDidChangeActiveTextEditor(() => {
        this.updateWorkspaceSyncStatus();
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
        if (!this.disposing) {
          void this.handleReplTerminalClosed();
        }
      }),
    );
  }

  public async start(): Promise<void> {
    this.context.subscriptions.push(
      this.statusItem,
      this.runItem,
      this.runInteractiveItem,
      this.workspaceSyncItem,
      this.runOutput,
      this.syncOutput,
      this.cleanupOutput,
      this.firmwareOutput,
      this.chipEraseOutput,
      this.workspaceOutput,
      this.workspaceFetchOutput,
    );
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
    this.workspaceViewProvider.invalidate();
    this.testingFolderViewProvider.invalidate();
    const autoConnect = this.shouldAutoConnectOnDetect();
    await this.pollDevices({
      forceSessionConnect: autoConnect,
      allowSessionConnect: autoConnect,
      showTerminalOnConnect: autoConnect,
    });
    this.pollTimer = setInterval(() => {
      void this.pollDevices({ allowSessionConnect: this.shouldAutoConnectOnDetect() });
    }, POLL_INTERVAL_MS);
  }

  public dispose(): void {
    this.disposing = true;
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
    this.cleanupOutput.dispose();
    this.firmwareOutput.dispose();
    this.chipEraseOutput.dispose();
    this.workspaceOutput.dispose();
    this.workspaceFetchOutput.dispose();
  }

  private registerCommands(): void {
    this.context.subscriptions.push(
      vscode.commands.registerCommand("calsci.selectDevice", async () => {
        await this.selectDevice();
      }),
      vscode.commands.registerCommand("calsci.openTerminal", async () => {
        await this.openTerminal();
      }),
      vscode.commands.registerCommand("calsci.softResetDevice", async () => {
        await this.softResetDevice();
      }),
      vscode.commands.registerCommand("calsci.flashFirmware", async () => {
        await this.uploadFirmwareFromPanel();
      }),
      vscode.commands.registerCommand("calsci.eraseChip", async () => {
        await this.eraseChipFromPanel();
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
      vscode.commands.registerCommand("calsci.fetchWorkspace", async () => {
        await this.fetchWorkspaceCommand();
      }),
      vscode.commands.registerCommand("calsci.fetchWorkspacePartial", async () => {
        await this.fetchWorkspacePartialCommand();
      }),
      vscode.commands.registerCommand("calsci.fetchWorkspacePartialConfirm", async () => {
        await this.confirmWorkspacePartialFetchCommand();
      }),
      vscode.commands.registerCommand("calsci.fetchWorkspacePartialClear", async () => {
        await this.clearWorkspacePartialFetchSelectionCommand();
      }),
      vscode.commands.registerCommand("calsci.fetchWorkspacePartialCancel", async () => {
        await this.cancelWorkspacePartialFetchCommand();
      }),
      vscode.commands.registerCommand("calsci.clearAllFiles", async () => {
        await this.clearAllFilesCommand();
      }),
      vscode.commands.registerCommand("calsci.refreshWorkspace", async () => {
        await this.refreshWorkspaceCommand();
      }),
      vscode.commands.registerCommand("calsci.newWorkspaceFile", async (target?: WorkspaceCommandTarget) => {
        await this.createWorkspaceFileCommand(target);
      }),
      vscode.commands.registerCommand("calsci.newWorkspaceFolder", async (target?: WorkspaceCommandTarget) => {
        await this.createWorkspaceFolderCommand(target);
      }),
      vscode.commands.registerCommand("calsci.copyWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.copyWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.pasteWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.pasteWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.renameWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.renameWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.deleteWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.deleteWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.showWorkspaceEntryProperties", async (target?: WorkspaceCommandTarget) => {
        await this.showWorkspaceEntryPropertiesCommand(target);
      }),
      vscode.commands.registerCommand("calsci.uploadWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.uploadWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.downloadWorkspaceEntry", async (target?: WorkspaceCommandTarget) => {
        await this.downloadWorkspaceEntryCommand(target);
      }),
      vscode.commands.registerCommand("calsci.mountWorkspace", async () => {
        await this.mountWorkspaceCommand();
      }),
      vscode.commands.registerCommand("calsci.refreshTestingFolder", async () => {
        this.testingFolderViewProvider.invalidate();
      }),
      vscode.commands.registerCommand("calsci.openWorkspaceFile", async (remotePath: string, port?: string) => {
        await this.openWorkspaceFileCommand(remotePath, port);
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
      void vscode.window.showWarningMessage("No CalSci device detected. Connect the device, then run Select Device again.");
      return;
    }

    if (this.devices.length === 1) {
      await this.persistSelectedPort(this.devices[0].port);
      this.showReplTerminal(false);
      await this.pollDevices();
      return;
    }

    const picks = this.devices.map((device) => ({
      label: device.port,
      detail: this.formatDevicePickerDetail(device),
      port: device.port,
    }));

    const choice = await vscode.window.showQuickPick(picks, {
      title: "Select Device",
      placeHolder: "Choose a detected device port",
      ignoreFocusOut: true,
    });

    if (!choice) {
      return;
    }

    await this.persistSelectedPort(choice.port);
    this.showReplTerminal(false);
    await this.pollDevices();
    void vscode.window.showInformationMessage(`Device selected: ${choice.port}`);
  }

  private formatDevicePickerDetail(device: DeviceInfo): string {
    const normalizedProduct = this.normalizeCalSciUsbName(device.product);
    if (normalizedProduct) {
      return normalizedProduct;
    }

    const normalizedDescription = this.normalizeCalSciUsbName(device.description);
    if (normalizedDescription) {
      return normalizedDescription;
    }

    return device.product || device.description;
  }

  private normalizeCalSciUsbName(value: string): string {
    const trimmed = value.trim();
    if (!trimmed) {
      return "";
    }

    const match = /^CalSci[_ -]([0-9a-fA-F]{12})$/.exec(trimmed);
    if (!match) {
      return "";
    }

    return `CalSci - ${match[1].toUpperCase()}`;
  }

  private async softResetDevice(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }
    if (this.operationInFlight > 0) {
      const settled = await this.waitForOperationToSettle(2000);
      if (!settled) {
        void vscode.window.showWarningMessage("CalSci is busy with another operation.");
        return;
      }
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
      return;
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
    if (!connected) {
      return;
    }

    const timeout = vscode.workspace.getConfiguration("calsci").get<number>("resetTimeoutSeconds", 5);
    const restoreHybridAfterReset = this.hybridStatus.active;
    if (restoreHybridAfterReset) {
      await this.stopHybridForExclusiveOperation();
    }

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
      if (restoreHybridAfterReset) {
        const restored = await this.restoreHybridWithRetries(port);
        if (!restored) {
          void vscode.window.showWarningMessage(
            `Soft reset finished on ${port}, but hybrid helper did not restart. Toggle Hybrid back on after the prompt settles.`,
          );
        }
      }

      this.workspaceViewProvider.invalidate(true);
      if (this.shouldAutoScanWorkspace()) {
        try {
          await this.refreshWorkspaceCommand();
        } catch {
          // Ignore refresh failures; soft reset itself already completed.
        }
      }

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

  private async restorePromptAfterFirmwareFlash(port: string): Promise<SoftResetResult | undefined> {
    this.firmwareOutput.appendLine("");
    this.firmwareOutput.appendLine("Post-flash serial reset: requesting CalSci prompt...");
    this.firmwareOutput.show(false);

    await this.pollDevices();

    let connected = await this.ensureSessionForPort(port, {
      force: true,
      notifyOnError: false,
      showTerminal: true,
    });
    if (!connected) {
      connected = await this.restartBackendAndReconnect(port);
    }
    if (!connected) {
      this.firmwareOutput.appendLine(`Post-flash serial reset skipped: could not reopen session on ${port}.`);
      return undefined;
    }

    const timeout = vscode.workspace.getConfiguration("calsci").get<number>("resetTimeoutSeconds", 5);

    let result: SoftResetResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await this.backend.softReset(port, timeout);
    } catch (error) {
      const message = this.errorMessage(error, "Post-flash serial reset failed.");
      this.firmwareOutput.appendLine(message);
      return {
        ok: false,
        promptSeen: false,
        rebootSeen: false,
        port,
        output: "",
        error: message,
      };
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    if (!result.ok) {
      const recovered = await this.restartBackendAndReconnect(port);
      if (recovered) {
        try {
          this.operationInFlight += 1;
          this.refreshStatus();
          result = await this.backend.softReset(port, timeout);
        } catch (error) {
          const message = this.errorMessage(error, "Post-flash serial reset recovery retry failed.");
          this.firmwareOutput.appendLine(message);
          return {
            ok: false,
            promptSeen: false,
            rebootSeen: false,
            port,
            output: "",
            error: message,
          };
        } finally {
          this.operationInFlight = Math.max(0, this.operationInFlight - 1);
          this.refreshStatus();
          await this.pollDevices();
        }
      }
    }

    if (result.ok) {
      if (result.promptSeen) {
        this.firmwareOutput.appendLine(`Post-flash serial reset complete on ${port}. CalSci prompt verified.`);
      } else if (result.rebootSeen) {
        this.firmwareOutput.appendLine(
          `Post-flash serial reset reached a reboot on ${port}, but the CalSci prompt was not yet verified.`,
        );
      } else {
        this.firmwareOutput.appendLine(`Post-flash serial reset complete on ${port}.`);
      }
    } else {
      const detail = result.error ? ` ${result.error}` : "";
      this.firmwareOutput.appendLine(`Post-flash serial reset failed on ${port}.${detail}`);
    }

    return result;
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
      const settled = await this.waitForOperationToSettle(2000);
      if (!settled) {
        void vscode.window.showWarningMessage("CalSci is busy with another operation.");
        return;
      }
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
        void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
        return;
      }

      const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
      if (!connected) {
        return;
      }

      const restoreHybridAfterRun = this.hybridStatus.active && await this.stopHybridForExclusiveOperation();
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
      if (restoreHybridAfterRun) {
        await this.restoreHybridAfterRun(port, false);
      }

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
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
      return;
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
    if (!connected) {
      return;
    }

    const hybridStoppedForInteractiveRun = this.hybridStatus.active && await this.stopHybridForExclusiveOperation();

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
      if (hybridStoppedForInteractiveRun) {
        await this.restoreHybridAfterRun(port, false);
      }
      const detail = result.error ? ` ${result.error}` : "";
      void vscode.window.showErrorMessage(`Interactive run failed on ${port}.${detail}`);
      return;
    }

    this.showReplTerminal(false);
    if (hybridStoppedForInteractiveRun) {
      await this.restoreHybridAfterRun(port, true);
    }
    const hybridNote = hybridStoppedForInteractiveRun
      ? " Hybrid helper was disabled for the run and will turn back on when the prompt returns."
      : "";
    void vscode.window.showInformationMessage(
      `Interactive run started on ${port}: ${path.basename(localFile)}.${hybridNote}`,
    );
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
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
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
              progress.report({ message: formatted.slice(0, MAX_PROGRESS_MESSAGE_LENGTH) });
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
    this.workspaceViewProvider.invalidate(true);
    if (this.shouldAutoScanWorkspace()) {
      try {
        await this.refreshWorkspaceCommand();
      } catch {
        // Ignore refresh failures; sync itself already completed.
      }
    }
    const fileCount = result.filesSynced ?? 0;
    const deletedCount = result.filesDeleted ?? 0;
    const skippedCount = result.filesSkipped ?? 0;
    const totalBytes = result.bytesSynced ?? 0;
    void vscode.window.showInformationMessage(
      `CalSci sync complete: ${fileCount} uploaded, ${deletedCount} deleted, ${skippedCount} skipped to ${result.remoteFolder} (${this.formatByteCount(totalBytes)} sent).`,
    );
  }

  private async clearAllFilesCommand(): Promise<void> {
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

    const confirmation = await vscode.window.showWarningMessage(
      "Delete all files from the selected CalSci device and recreate an empty boot.py?",
      {
        modal: true,
        detail: "This mirrors the desktop app workflow and removes the current device workspace.",
      },
      "Delete All Files",
    );
    if (confirmation !== "Delete All Files") {
      return;
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
      return;
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: true });
    if (!connected) {
      return;
    }

    let result: ClearAllFilesResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `CalSci: Clearing all files on ${port}`,
          cancellable: false,
        },
        async (progress) => {
          this.cleanupOutput.clear();
          this.cleanupOutput.appendLine(`CalSci clear-all on ${port}`);
          this.cleanupOutput.appendLine("Workflow: desktop-style recursive cleanup + empty boot.py restore");
          this.cleanupOutput.appendLine("");
          this.cleanupOutput.show(false);

          return this.backend.clearAllFiles(port, (line: string, isError: boolean) => {
            const formatted = isError ? `[ERROR] ${line}` : line;
            this.cleanupOutput.appendLine(formatted);
            progress.report({ message: formatted.slice(0, MAX_PROGRESS_MESSAGE_LENGTH) });
          });
        },
      );
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Clear all files failed."));
      return;
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    this.cleanupOutput.show(false);

    if (!result.ok) {
      const detail = result.error ? ` ${result.error}` : "";
      void vscode.window.showErrorMessage(`Clear all files failed on ${port}.${detail}`);
      return;
    }

    this.workspaceViewProvider.invalidate();
    if (this.shouldAutoScanWorkspace()) {
      try {
        await this.refreshWorkspaceCommand();
      } catch {
        // Ignore post-clean refresh failures; the clear operation already succeeded.
      }
    }

    const filesDeleted = result.filesDeleted ?? 0;
    const directoriesDeleted = result.directoriesDeleted ?? 0;
    const warningsReported = result.warningsReported ?? 0;
    if (warningsReported > 0) {
      void vscode.window.showWarningMessage(
        `CalSci clear complete: ${filesDeleted} files deleted, ${directoriesDeleted} folders deleted, ${warningsReported} warning(s). Empty boot.py restored.`,
      );
      return;
    }

    void vscode.window.showInformationMessage(
      `CalSci clear complete: ${filesDeleted} files deleted, ${directoriesDeleted} folders deleted. Empty boot.py restored.`,
    );
  }

  private async refreshWorkspaceCommand(): Promise<void> {
    await this.workspaceViewProvider.reload();
  }

  private async createWorkspaceFileCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const effectiveTarget = this.getWorkspaceCommandTarget(target);
    const port = await this.prepareWorkspaceCommandPort(effectiveTarget?.port, false);
    if (!port) {
      return;
    }

    const parentUri = this.resolveWorkspaceDirectoryUri(effectiveTarget, port);
    const fileName = await this.promptWorkspaceName(
      "CalSci: New File",
      "Enter the new file name",
      "main.py",
    );
    if (!fileName) {
      return;
    }

    const fileUri = createCalSciWorkspaceChildUri(parentUri, fileName);
    try {
      if (await this.workspaceUriExists(fileUri)) {
        void vscode.window.showWarningMessage(`A CalSci file named ${fileName} already exists.`);
        return;
      }
      await vscode.workspace.fs.writeFile(fileUri, new Uint8Array());
      const document = await vscode.workspace.openTextDocument(fileUri);
      await vscode.window.showTextDocument(document, { preview: false });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to create ${fileName}.`));
    }
  }

  private async createWorkspaceFolderCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const effectiveTarget = this.getWorkspaceCommandTarget(target);
    const port = await this.prepareWorkspaceCommandPort(effectiveTarget?.port, false);
    if (!port) {
      return;
    }

    const parentUri = this.resolveWorkspaceDirectoryUri(effectiveTarget, port);
    const folderName = await this.promptWorkspaceName(
      "CalSci: New Folder",
      "Enter the new folder name",
      "folder",
    );
    if (!folderName) {
      return;
    }

    const folderUri = createCalSciWorkspaceChildUri(parentUri, folderName);
    try {
      if (await this.workspaceUriExists(folderUri)) {
        void vscode.window.showWarningMessage(`A CalSci folder named ${folderName} already exists.`);
        return;
      }
      await vscode.workspace.fs.createDirectory(folderUri);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to create ${folderName}.`));
    }
  }

  private async copyWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const targetUri = await this.resolveWorkspaceEntryUri(target);
    if (!targetUri) {
      return;
    }

    const { remotePath } = parseCalSciWorkspaceUri(targetUri);
    if (remotePath === "/") {
      void vscode.window.showWarningMessage("The CalSci device root cannot be copied.");
      return;
    }

    await this.setWorkspaceClipboard(targetUri);
    vscode.window.setStatusBarMessage(`Copied ${path.posix.basename(remotePath)}.`, 2000);
  }

  private async pasteWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const sourceUri = this.workspaceClipboardSource;
    if (!sourceUri) {
      return;
    }

    let sourceStat: vscode.FileStat;
    try {
      sourceStat = await vscode.workspace.fs.stat(sourceUri);
    } catch (error) {
      await this.setWorkspaceClipboard(undefined);
      void vscode.window.showWarningMessage(this.errorMessage(error, "The copied CalSci item no longer exists."));
      return;
    }

    const sourceTarget = parseCalSciWorkspaceUri(sourceUri);
    const effectiveTarget = this.getWorkspaceCommandTarget(target);
    const port = await this.prepareWorkspaceCommandPort(effectiveTarget?.port ?? sourceTarget.port, false);
    if (!port) {
      return;
    }
    if (port !== sourceTarget.port) {
      void vscode.window.showWarningMessage("Paste currently works only on the same CalSci device.");
      return;
    }

    const destinationDirectoryUri = this.resolveWorkspaceDirectoryUri(effectiveTarget, port);
    const sourceName = path.posix.basename(sourceTarget.remotePath);
    if (!sourceName) {
      void vscode.window.showWarningMessage("The copied CalSci entry is not valid.");
      return;
    }

    const destinationUri = await this.createPasteDestinationUri(destinationDirectoryUri, sourceName, sourceStat);
    const destinationTarget = parseCalSciWorkspaceUri(destinationUri);
    if (destinationTarget.remotePath.startsWith(`${sourceTarget.remotePath}/`)) {
      void vscode.window.showWarningMessage("You cannot paste a folder into itself.");
      return;
    }

    try {
      await this.copyWorkspaceUri(sourceUri, destinationUri);
      await this.setWorkspaceClipboard(undefined);
      void vscode.window.showInformationMessage(`Pasted ${path.posix.basename(destinationTarget.remotePath)}.`);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to paste ${sourceName}.`));
    }
  }

  private async renameWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const targetUri = await this.resolveWorkspaceEntryUri(target);
    if (!targetUri) {
      return;
    }

    const { remotePath } = parseCalSciWorkspaceUri(targetUri);
    if (remotePath === "/") {
      void vscode.window.showWarningMessage("The CalSci device root cannot be renamed.");
      return;
    }

    const currentName = path.posix.basename(remotePath);
    const nextName = await this.promptWorkspaceName(
      "CalSci: Rename",
      `Enter the new name for ${currentName}`,
      currentName,
    );
    if (!nextName || nextName === currentName) {
      return;
    }

    const renamedUri = createCalSciWorkspaceChildUri(getCalSciWorkspaceParentUri(targetUri), nextName);
    try {
      await vscode.workspace.fs.rename(targetUri, renamedUri, { overwrite: false });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to rename ${currentName}.`));
    }
  }

  private async showWorkspaceEntryPropertiesCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const targetUri = await this.resolveWorkspaceEntryUri(target);
    if (!targetUri) {
      return;
    }

    try {
      const entryStat = await vscode.workspace.fs.stat(targetUri);
      const { port, remotePath } = parseCalSciWorkspaceUri(targetUri);
      const name = remotePath === "/" ? "CalSci" : path.posix.basename(remotePath);
      const parentPath = remotePath === "/" ? "/" : path.posix.dirname(remotePath);
      const isDirectory = (entryStat.type & vscode.FileType.Directory) !== 0;
      const typeLabel = isDirectory ? "Folder" : "File";
      let summaryLine = `Size: ${this.formatByteCount(entryStat.size)}`;

      if (isDirectory) {
        const childEntries = await vscode.workspace.fs.readDirectory(targetUri);
        const folderCount = childEntries.filter(([, type]) => (type & vscode.FileType.Directory) !== 0).length;
        const fileCount = childEntries.filter(([, type]) => (type & vscode.FileType.File) !== 0).length;
        summaryLine = `Contents: ${folderCount} folder(s), ${fileCount} file(s)`;
      }

      const detailLines = [
        `Name: ${name}`,
        `Type: ${typeLabel}`,
        summaryLine,
        `Modified: ${this.formatWorkspaceTimestamp(entryStat.mtime)}`,
        `Created: ${this.formatWorkspaceTimestamp(entryStat.ctime)}`,
        `Path: ${remotePath}`,
        `Folder: ${parentPath}`,
        `Device: ${port}`,
      ];

      await vscode.window.showInformationMessage(name, {
        modal: true,
        detail: detailLines.join("\n"),
      });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Failed to load workspace properties."));
    }
  }

  private async deleteWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const targetUri = await this.resolveWorkspaceEntryUri(target);
    if (!targetUri) {
      return;
    }

    const { remotePath } = parseCalSciWorkspaceUri(targetUri);
    if (remotePath === "/") {
      void vscode.window.showWarningMessage("The CalSci device root cannot be deleted.");
      return;
    }

    const label = path.posix.basename(remotePath);
    const isDirectory = target?.kind === "folder";
    const confirmation = await vscode.window.showWarningMessage(
      `Delete ${isDirectory ? "folder" : "file"} ${label} from CalSci?`,
      {
        modal: true,
        detail: isDirectory
          ? "This removes the folder and all of its contents from the device."
          : "This removes the file from the device.",
      },
      "Delete",
    );
    if (confirmation !== "Delete") {
      return;
    }

    try {
      await vscode.workspace.fs.delete(targetUri, { recursive: isDirectory, useTrash: false });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to delete ${label}.`));
    }
  }

  private async uploadWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const port = await this.prepareWorkspaceCommandPort(target?.port, false);
    if (!port) {
      return;
    }

    const destinationUri = this.resolveWorkspaceDirectoryUri(target, port);
    const destinationPath = parseCalSciWorkspaceUri(destinationUri).remotePath;
    const picked = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: true,
      canSelectMany: true,
      openLabel: "Upload to CalSci",
      title: "CalSci: Select Files or Folders to Upload",
      defaultUri: vscode.workspace.workspaceFolders?.find((folder) => folder.uri.scheme === "file")?.uri,
    });
    if (!picked || picked.length === 0) {
      return;
    }

    try {
      const counts = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `CalSci: Uploading to ${destinationPath}`,
          cancellable: false,
        },
        async (progress) => {
          let files = 0;
          let directories = 0;
          for (const sourceUri of picked) {
            const copied = await this.copyLocalUriToWorkspace(sourceUri, destinationUri, progress);
            files += copied.files;
            directories += copied.directories;
          }
          return { files, directories };
        },
      );

      void vscode.window.showInformationMessage(
        `CalSci upload complete: ${counts.files} file(s) and ${counts.directories} folder(s) copied to ${destinationPath}.`,
      );
      this.workspaceViewProvider.invalidate(true);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "CalSci upload failed."));
    }
  }

  private async downloadWorkspaceEntryCommand(target?: WorkspaceCommandTarget): Promise<void> {
    const effectiveTarget = this.getWorkspaceCommandTarget(target);
    const remotePath = normalizeCalSciRemotePath(effectiveTarget?.remotePath ?? "/");
    if (remotePath === "/") {
      await this.fetchWorkspaceCommand();
      return;
    }

    const port = await this.prepareWorkspaceCommandPort(effectiveTarget?.port, true);
    if (!port) {
      return;
    }

    const localFolder = await this.pickExistingWorkspaceDestinationFolder("Save CalSci Files", "CalSci: Select Local Folder");
    if (!localFolder) {
      return;
    }

    await this.importWorkspaceSelection(port, localFolder, [remotePath]);
  }

  private async mountWorkspaceCommand(): Promise<void> {
    const port = await this.prepareWorkspaceCommandPort(undefined, false);
    if (!port) {
      return;
    }

    const rootUri = createCalSciWorkspaceUri("/", port);
    const existing = vscode.workspace.workspaceFolders?.find((folder) => folder.uri.toString() === rootUri.toString());
    if (existing) {
      void vscode.window.showInformationMessage(`CalSci workspace already mounted as ${existing.name}.`);
      return;
    }

    const index = vscode.workspace.workspaceFolders?.length ?? 0;
    const added = vscode.workspace.updateWorkspaceFolders(index, 0, {
      uri: rootUri,
      name: `CalSci (${path.basename(port) || port})`,
    });
    if (!added) {
      void vscode.window.showErrorMessage("Failed to mount the CalSci workspace in Explorer.");
      return;
    }

    void vscode.window.showInformationMessage(`CalSci workspace mounted for ${port}.`);
  }

  private async fetchWorkspaceCommand(): Promise<void> {
    const port = await this.prepareWorkspaceFetchPort();
    if (!port) {
      return;
    }

    const localFolder = await this.pickExistingWorkspaceDestinationFolder("Save CalSci Files", "CalSci: Select Local Folder");
    if (!localFolder) {
      return;
    }

    await this.importWorkspaceSelection(port, localFolder);
  }

  private async fetchWorkspacePartialCommand(): Promise<void> {
    if (this.workspaceViewProvider.isFetchSelectionActive) {
      if (this.workspaceViewProvider.getSelectedFetchPaths().length === 0) {
        await this.clearWorkspaceFetchSession();
        void vscode.window.showInformationMessage("Download selection closed.");
        return;
      }

      await this.confirmWorkspacePartialFetchCommand();
      return;
    }

    const port = await this.prepareWorkspaceFetchPort();
    if (!port) {
      return;
    }

    if (this.workspaceViewProvider.activateFetchSelection(port)) {
      await this.updateWorkspaceFetchState();
      await vscode.commands.executeCommand("workbench.view.extension.calsciSidebar");
      void vscode.window.showInformationMessage(
        "Download selection is active in CalSci Workspace. Check files or folders, then press Download again.",
      );
      return;
    }

    let entries: WorkspaceTreeEntry[];
    try {
      entries = await this.loadWorkspaceEntriesForFetch(port);
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Unable to load CalSci workspace."));
      return;
    }

    if (entries.length === 0) {
      await this.clearWorkspaceFetchSession();
      void vscode.window.showInformationMessage("CalSci workspace is empty.");
      return;
    }

    this.workspaceViewProvider.setFetchSelectionSnapshot(port, entries);
    await this.updateWorkspaceFetchState();

    await vscode.commands.executeCommand("workbench.view.extension.calsciSidebar");
    void vscode.window.showInformationMessage(
      "Download selection is active in CalSci Workspace. Check files or folders, then press Download again.",
    );
  }

  private async prepareWorkspaceCommandPort(preferredPort?: string, showTerminal = false): Promise<string | undefined> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return undefined;
    }
    if (this.runInFlight) {
      void vscode.window.showWarningMessage("CalSci is busy with a run operation.");
      return undefined;
    }
    if (this.operationInFlight > 0) {
      const settled = await this.waitForOperationToSettle(2000);
      if (!settled) {
        void vscode.window.showWarningMessage("CalSci is busy with another operation.");
        return undefined;
      }
    }

    let port = preferredPort?.trim();
    if (!port) {
      try {
        port = await this.resolvePortForOperation();
      } catch (error) {
        void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
        return undefined;
      }
    }

    const connected = await this.ensureSessionForPort(port, {
      force: true,
      notifyOnError: true,
      showTerminal,
    });
    if (!connected) {
      return undefined;
    }
    return port;
  }

  private async prepareWorkspaceFetchPort(): Promise<string | undefined> {
    return this.prepareWorkspaceCommandPort(undefined, true);
  }

  private async loadWorkspaceEntriesForFetch(port: string): Promise<WorkspaceTreeEntry[]> {
    let result: WorkspaceTreeResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `CalSci: Loading workspace from ${port}`,
          cancellable: false,
        },
        async () => {
          this.workspaceOutput.clear();
          this.workspaceOutput.appendLine(`CalSci workspace scan on ${port}`);
          this.workspaceOutput.appendLine("Remote root: /");
          this.workspaceOutput.appendLine("");
          this.workspaceOutput.show(false);
          return this.backend.scanWorkspaceTree(port);
        },
      );
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    this.workspaceOutput.show(false);
    if (!result.ok) {
      throw new Error(result.error ?? "Failed to scan CalSci workspace.");
    }
    return result.entries ?? [];
  }

  private async pickWorkspaceDestinationFolder(): Promise<string | undefined> {
    type DestinationChoice = vscode.QuickPickItem & {
      action: "existing" | "new";
    };

    const choice = await vscode.window.showQuickPick<DestinationChoice>([
      {
        label: "$(folder-opened) Existing Folder",
        detail: "Save the fetched CalSci files into an existing local folder.",
        action: "existing",
      },
      {
        label: "$(new-folder) New Folder",
        detail: "Choose a parent folder, then create a new folder for the fetched CalSci files.",
        action: "new",
      },
    ], {
      title: "CalSci: Choose Save Location",
      placeHolder: "Select where the fetched CalSci files should be saved",
      ignoreFocusOut: true,
    });

    if (!choice) {
      return undefined;
    }

    if (choice.action === "existing") {
      return this.pickExistingWorkspaceDestinationFolder("Save CalSci Files", "CalSci: Select Folder to Save Files");
    }

    const parentFolder = await this.pickExistingWorkspaceDestinationFolder("Select Parent Folder", "CalSci: Select Parent Folder");
    if (!parentFolder) {
      return undefined;
    }

    const folderName = await vscode.window.showInputBox({
      title: "CalSci: New Folder Name",
      prompt: "Enter the name of the new folder for fetched CalSci files",
      ignoreFocusOut: true,
      validateInput: (value) => {
        const trimmed = value.trim();
        if (!trimmed) {
          return "Folder name is required.";
        }
        if (trimmed.includes("/") || trimmed.includes("\\")) {
          return "Enter a folder name only, not a path.";
        }
        return undefined;
      },
    });
    if (!folderName) {
      return undefined;
    }

    const destination = path.resolve(parentFolder, folderName.trim());
    try {
      await fs.promises.mkdir(destination, { recursive: false });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Unable to create folder ${destination}.`));
      return undefined;
    }
    this.lastWorkspaceDestinationFolder = destination;
    return destination;
  }

  private async pickExistingWorkspaceDestinationFolder(openLabel: string, title: string): Promise<string | undefined> {
    const picked = await vscode.window.showOpenDialog({
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      defaultUri: this.getDefaultLocalFolderUri(),
      openLabel,
      title,
    });
    if (!picked || picked.length === 0) {
      return undefined;
    }
    const resolved = path.resolve(picked[0].fsPath);
    this.lastWorkspaceDestinationFolder = resolved;
    return resolved;
  }

  private async importWorkspaceSelection(
    port: string,
    localFolder: string,
    remotePaths?: string[],
  ): Promise<boolean> {
    let result: WorkspaceImportResult;
    const selectedCount = remotePaths?.length ?? 0;
    const fetchTitle = remotePaths && remotePaths.length > 0
      ? `CalSci: Fetching ${selectedCount} selected item(s) from ${port}`
      : `CalSci: Fetching all files from ${port}`;

    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: fetchTitle,
          cancellable: false,
        },
        async (progress) => {
          this.workspaceFetchOutput.clear();
          this.workspaceFetchOutput.appendLine(`CalSci workspace fetch on ${port}`);
          this.workspaceFetchOutput.appendLine(`Local: ${localFolder}`);
          this.workspaceFetchOutput.appendLine(
            remotePaths && remotePaths.length > 0
              ? `Selection: ${remotePaths.join(", ")}`
              : "Selection: /",
          );
          this.workspaceFetchOutput.appendLine("");
          this.workspaceFetchOutput.show(false);

          return this.backend.importWorkspace(
            port,
            localFolder,
            (line: string, isError: boolean) => {
              const formatted = isError ? `[ERROR] ${line}` : line;
              this.workspaceFetchOutput.appendLine(formatted);
              progress.report({ message: formatted.slice(0, MAX_PROGRESS_MESSAGE_LENGTH) });
            },
            remotePaths,
          );
        },
      );
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Workspace fetch failed."));
      return false;
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      this.testingFolderViewProvider.invalidate();
      await this.pollDevices();
    }

    this.workspaceFetchOutput.show(false);
    if (!result.ok) {
      const detail = result.error ? ` ${result.error}` : "";
      void vscode.window.showErrorMessage(`Workspace fetch failed on ${port}.${detail}`);
      return false;
    }

    const filesImported = result.filesImported ?? 0;
    const directoriesImported = result.directoriesImported ?? 0;
    const bytesImported = result.bytesImported ?? 0;
    void vscode.window.showInformationMessage(
      `CalSci fetch complete: ${filesImported} files and ${directoriesImported} folders saved to ${localFolder} (${this.formatByteCount(bytesImported)}).`,
    );
    return true;
  }

  private async confirmWorkspacePartialFetchCommand(): Promise<void> {
    if (!this.workspaceViewProvider.isFetchSelectionActive) {
      void vscode.window.showWarningMessage("Press the download button in CalSci Workspace first to show checkboxes.");
      return;
    }

    const port = this.workspaceViewProvider.fetchSelectionPort;
    if (!port) {
      void vscode.window.showWarningMessage("No CalSci device is loaded for download selection.");
      return;
    }

    const remotePaths = this.workspaceViewProvider.getSelectedFetchPaths();
    if (remotePaths.length === 0) {
      void vscode.window.showWarningMessage("Select at least one file or folder in CalSci Workspace.");
      return;
    }

    const localFolder = await this.pickExistingWorkspaceDestinationFolder("Save CalSci Files", "CalSci: Select Local Folder");
    if (!localFolder) {
      return;
    }

    const completed = await this.importWorkspaceSelection(port, localFolder, remotePaths);
    if (completed) {
      await this.clearWorkspaceFetchSession();
    }
  }

  private async clearWorkspacePartialFetchSelectionCommand(): Promise<void> {
    if (!this.workspaceViewProvider.isFetchSelectionActive) {
      return;
    }

    this.workspaceViewProvider.clearFetchSelection();
    await this.updateWorkspaceFetchState();
  }

  private async cancelWorkspacePartialFetchCommand(): Promise<void> {
    if (!this.workspaceViewProvider.isFetchSelectionActive) {
      return;
    }

    await this.clearWorkspaceFetchSession();
  }

  private async clearWorkspaceFetchSession(): Promise<void> {
    this.workspaceViewProvider.resetFetchSelection();
    await this.updateWorkspaceFetchState();
  }

  private async updateWorkspaceFetchState(): Promise<void> {
    const active = this.workspaceViewProvider.isFetchSelectionActive;
    const port = this.workspaceViewProvider.fetchSelectionPort;
    const selectionCount = this.workspaceViewProvider.getSelectedFetchPaths().length;

    this.workspaceTreeView.message = active
      ? "Select files or folders, then press Download again. Press Download again with no selection to close selection mode."
      : undefined;
    this.workspaceTreeView.description = active && port
      ? `${path.basename(port) || port}${selectionCount > 0 ? ` | ${selectionCount} selected` : " | choose files"}`
      : undefined;
    this.workspaceTreeView.badge = active && selectionCount > 0
      ? {
          value: Math.min(selectionCount, 999),
          tooltip: `${selectionCount} item(s) selected for download.`,
        }
      : undefined;

    await vscode.commands.executeCommand("setContext", "calsci.workspaceFetchActive", active);
    await vscode.commands.executeCommand("setContext", "calsci.workspaceFetchHasSelection", active && selectionCount > 0);
  }

  private async scanWorkspaceTree(): Promise<{ port: string; entries: WorkspaceTreeEntry[] }> {
    if (!this.backendReady) {
      throw new Error("CalSci backend is still initializing.");
    }
    if (this.runInFlight) {
      throw new Error("CalSci is busy with a run operation.");
    }
    if (this.operationInFlight > 0) {
      throw new Error("CalSci is busy with another operation.");
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      throw new Error(this.errorMessage(error, "No device selected."));
    }

    const connected = await this.ensureSessionForPort(port, { force: true, notifyOnError: true, showTerminal: false });
    if (!connected) {
      throw new Error(`Unable to connect to ${port}.`);
    }

    let result: WorkspaceTreeResult;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `CalSci: Scanning workspace on ${port}`,
          cancellable: false,
        },
        async () => {
          this.workspaceOutput.clear();
          this.workspaceOutput.appendLine(`CalSci workspace scan on ${port}`);
          this.workspaceOutput.appendLine("Remote root: /");
          this.workspaceOutput.appendLine("");
          this.workspaceOutput.show(false);
          return this.backend.scanWorkspaceTree(port);
        },
      );
    } catch (error) {
      throw new Error(this.errorMessage(error, "Workspace scan failed."));
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
      await this.pollDevices();
    }

    this.workspaceOutput.show(false);

    if (!result.ok) {
      throw new Error(result.error ?? `Workspace scan failed on ${port}.`);
    }

    const entries = result.entries ?? [];
    this.workspaceOutput.appendLine(`Scan complete: ${entries.length} entry(s)`);
    return {
      port: result.port || port,
      entries,
    };
  }

  private async openWorkspaceFileCommand(remotePath: string, preferredPort?: string): Promise<void> {
    const remoteFile = remotePath?.trim();
    if (!remoteFile) {
      void vscode.window.showErrorMessage("CalSci workspace file path is missing.");
      return;
    }

    const port = await this.prepareWorkspaceCommandPort(preferredPort, false);
    if (!port) {
      return;
    }

    try {
      const uri = createCalSciWorkspaceUri(remoteFile, port);
      const document = await vscode.workspace.openTextDocument(uri);
      await vscode.window.showTextDocument(document, { preview: false });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, `Failed to open ${remoteFile}.`));
    }
  }

  private async ensureWorkspaceBackendAccess(port: string): Promise<void> {
    if (!this.backendReady) {
      throw new Error("CalSci backend is still initializing.");
    }
    if (this.runInFlight) {
      throw new Error("CalSci is busy with a run operation.");
    }
    if (this.operationInFlight > 0) {
      const settled = await this.waitForOperationToSettle(2000);
      if (!settled) {
        throw new Error("CalSci is busy with another operation.");
      }
    }

    const connected = await this.ensureSessionForPort(port, {
      force: true,
      notifyOnError: false,
      showTerminal: false,
    });
    if (!connected) {
      throw new Error(`Unable to connect to ${port}.`);
    }
  }

  private async withWorkspaceBackendOperation<T>(port: string, task: () => Promise<T>): Promise<T> {
    await this.ensureWorkspaceBackendAccess(port);
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      return await task();
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
    }
  }

  private async statWorkspaceUri(uri: vscode.Uri): Promise<WorkspaceStat> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.statWorkspaceEntry(port, remotePath));
    if (!result.ok || !result.stat) {
      this.throwWorkspaceResultError(result, `Failed to stat ${remotePath}.`);
    }
    return result.stat;
  }

  private async readWorkspaceDirectoryUri(uri: vscode.Uri): Promise<WorkspaceDirectoryEntry[]> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.listWorkspaceDirectory(port, remotePath));
    if (!result.ok || !result.entries) {
      this.throwWorkspaceResultError(result, `Failed to list ${remotePath}.`);
    }
    return result.entries;
  }

  private async readWorkspaceFileUri(uri: vscode.Uri): Promise<Uint8Array> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.readWorkspaceFile(port, remotePath));
    if (!result.ok || typeof result.contentBase64 !== "string") {
      this.throwWorkspaceResultError(result, `Failed to read ${remotePath}.`);
    }
    return Buffer.from(result.contentBase64, "base64");
  }

  private async writeWorkspaceFileUri(
    uri: vscode.Uri,
    content: Uint8Array,
    options: {
      create: boolean;
      overwrite: boolean;
    },
  ): Promise<void> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.writeWorkspaceFile(
      port,
      remotePath,
      Buffer.from(content).toString("base64"),
      options,
    ));
    if (!result.ok) {
      this.throwWorkspaceResultError(result, `Failed to write ${remotePath}.`);
    }
    this.workspaceSyncState.set(uri.toString(), "synced");
    this.updateWorkspaceSyncStatus();
  }

  private async createWorkspaceDirectoryUri(uri: vscode.Uri): Promise<void> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.createWorkspaceDirectory(port, remotePath));
    if (!result.ok) {
      this.throwWorkspaceResultError(result, `Failed to create ${remotePath}.`);
    }
  }

  private async deleteWorkspaceEntryUri(uri: vscode.Uri, recursive: boolean): Promise<void> {
    const { port, remotePath } = parseCalSciWorkspaceUri(uri);
    const result = await this.withWorkspaceBackendOperation(port, () => this.backend.deleteWorkspaceEntry(port, remotePath, recursive));
    if (!result.ok) {
      this.throwWorkspaceResultError(result, `Failed to delete ${remotePath}.`);
    }
  }

  private async renameWorkspaceEntryUri(oldUri: vscode.Uri, newUri: vscode.Uri, overwrite: boolean): Promise<void> {
    const oldTarget = parseCalSciWorkspaceUri(oldUri);
    const newTarget = parseCalSciWorkspaceUri(newUri);
    if (oldTarget.port !== newTarget.port) {
      throw createCalSciWorkspaceError("EINVAL", "CalSci workspace rename must stay on the same device.");
    }

    const result = await this.withWorkspaceBackendOperation(oldTarget.port, () => this.backend.renameWorkspaceEntry(
      oldTarget.port,
      oldTarget.remotePath,
      newTarget.remotePath,
      overwrite,
    ));
    if (!result.ok) {
      this.throwWorkspaceResultError(result, `Failed to rename ${oldTarget.remotePath}.`);
    }
  }

  private resolveWorkspaceDirectoryUri(target: WorkspaceCommandTarget | undefined, port: string): vscode.Uri {
    const remotePath = normalizeCalSciRemotePath(target?.remotePath ?? "/");
    if (target?.kind === "file") {
      return createCalSciWorkspaceUri(path.posix.dirname(remotePath), port);
    }
    return createCalSciWorkspaceUri(remotePath, port);
  }

  private async resolveWorkspaceEntryUri(target?: WorkspaceCommandTarget): Promise<vscode.Uri | undefined> {
    const effectiveTarget = this.getWorkspaceCommandTarget(target);
    const remotePath = effectiveTarget?.remotePath?.trim();
    if (!remotePath) {
      void vscode.window.showWarningMessage("Select a CalSci workspace file or folder first.");
      return undefined;
    }

    const port = await this.prepareWorkspaceCommandPort(effectiveTarget?.port, false);
    if (!port) {
      return undefined;
    }

    return createCalSciWorkspaceUri(remotePath, port);
  }

  private async promptWorkspaceName(title: string, prompt: string, initialValue: string): Promise<string | undefined> {
    const value = await vscode.window.showInputBox({
      title,
      prompt,
      value: initialValue,
      ignoreFocusOut: true,
      validateInput: (input) => {
        const trimmed = input.trim();
        if (!trimmed) {
          return "Name is required.";
        }
        if (trimmed === "." || trimmed === "..") {
          return "Choose a normal file or folder name.";
        }
        if (trimmed.includes("/") || trimmed.includes("\\")) {
          return "Enter a name only, not a path.";
        }
        return undefined;
      },
    });
    return value?.trim() || undefined;
  }

  private async copyLocalUriToWorkspace(
    sourceUri: vscode.Uri,
    destinationDirectoryUri: vscode.Uri,
    progress: vscode.Progress<{ message?: string }>,
  ): Promise<{ files: number; directories: number }> {
    const sourceStat = await vscode.workspace.fs.stat(sourceUri);
    const sourceName = path.posix.basename(sourceUri.path);
    if (!sourceName) {
      throw new Error(`Cannot derive a destination name for ${sourceUri.path}.`);
    }

    const targetUri = createCalSciWorkspaceChildUri(destinationDirectoryUri, sourceName);
    if ((sourceStat.type & vscode.FileType.Directory) !== 0) {
      progress.report({ message: `Folder: ${sourceName}` });
      await vscode.workspace.fs.createDirectory(targetUri);
      let files = 0;
      let directories = 1;
      const children = await vscode.workspace.fs.readDirectory(sourceUri);
      for (const [childName] of children) {
        const childUri = vscode.Uri.joinPath(sourceUri, childName);
        const childCounts = await this.copyLocalUriToWorkspace(childUri, targetUri, progress);
        files += childCounts.files;
        directories += childCounts.directories;
      }
      return { files, directories };
    }

    if ((sourceStat.type & vscode.FileType.File) !== 0) {
      progress.report({ message: `File: ${sourceName}` });
      const content = await vscode.workspace.fs.readFile(sourceUri);
      await vscode.workspace.fs.writeFile(targetUri, content);
      return { files: 1, directories: 0 };
    }

    throw new Error(`Unsupported local entry type: ${sourceUri.fsPath || sourceUri.path}`);
  }

  private async copyWorkspaceUri(sourceUri: vscode.Uri, destinationUri: vscode.Uri): Promise<void> {
    const sourceStat = await vscode.workspace.fs.stat(sourceUri);
    if ((sourceStat.type & vscode.FileType.Directory) !== 0) {
      await vscode.workspace.fs.createDirectory(destinationUri);
      const children = await vscode.workspace.fs.readDirectory(sourceUri);
      for (const [childName] of children) {
        await this.copyWorkspaceUri(
          vscode.Uri.joinPath(sourceUri, childName),
          vscode.Uri.joinPath(destinationUri, childName),
        );
      }
      return;
    }

    const content = await vscode.workspace.fs.readFile(sourceUri);
    await vscode.workspace.fs.writeFile(destinationUri, content);
  }

  private async createPasteDestinationUri(
    destinationDirectoryUri: vscode.Uri,
    sourceName: string,
    sourceStat: vscode.FileStat,
  ): Promise<vscode.Uri> {
    const directUri = createCalSciWorkspaceChildUri(destinationDirectoryUri, sourceName);
    if (!(await this.workspaceUriExists(directUri))) {
      return directUri;
    }

    const isDirectory = (sourceStat.type & vscode.FileType.Directory) !== 0;
    const parsedName = path.posix.parse(sourceName);
    const baseName = isDirectory ? sourceName : parsedName.name;
    const extension = isDirectory ? "" : parsedName.ext;

    for (let copyIndex = 1; copyIndex <= 999; copyIndex += 1) {
      const suffix = copyIndex === 1 ? " copy" : ` copy ${copyIndex}`;
      const candidateName = `${baseName}${suffix}${extension}`;
      const candidateUri = createCalSciWorkspaceChildUri(destinationDirectoryUri, candidateName);
      if (!(await this.workspaceUriExists(candidateUri))) {
        return candidateUri;
      }
    }

    throw new Error(`Unable to find a free paste name for ${sourceName}.`);
  }

  private async workspaceUriExists(uri: vscode.Uri): Promise<boolean> {
    try {
      await vscode.workspace.fs.stat(uri);
      return true;
    } catch (error) {
      if (getCalSciWorkspaceErrorCode(error) === "ENOENT") {
        return false;
      }
      if (error instanceof vscode.FileSystemError && /file not found/i.test(error.message)) {
        return false;
      }
      throw error;
    }
  }

  private getWorkspaceCommandTarget(target?: WorkspaceCommandTarget): WorkspaceCommandTarget | undefined {
    if (target?.remotePath || target?.port) {
      return target;
    }
    return this.activeWorkspaceTarget;
  }

  private toWorkspaceCommandTarget(item?: CalSciWorkspaceItem): WorkspaceCommandTarget | undefined {
    if (!item || item.kind === "placeholder") {
      return undefined;
    }
    return {
      remotePath: item.remotePath,
      port: item.port,
      kind: item.kind,
    };
  }

  private async setWorkspaceClipboard(source: vscode.Uri | undefined): Promise<void> {
    this.workspaceClipboardSource = source;
    await vscode.commands.executeCommand("setContext", "calsci.workspaceHasClipboard", Boolean(source));
  }

  private getDefaultLocalFolderUri(): vscode.Uri {
    if (this.lastWorkspaceDestinationFolder) {
      return vscode.Uri.file(this.lastWorkspaceDestinationFolder);
    }

    const workspaceFolder = vscode.workspace.workspaceFolders?.find((folder) => folder.uri.scheme === "file");
    if (workspaceFolder) {
      return workspaceFolder.uri;
    }

    return vscode.Uri.file(os.homedir());
  }

  private formatWorkspaceTimestamp(value: number): string {
    if (!value) {
      return "Unknown";
    }

    const milliseconds = value < 10_000_000_000 ? value * 1000 : value;
    const date = new Date(milliseconds);
    if (Number.isNaN(date.valueOf())) {
      return String(value);
    }
    return date.toLocaleString();
  }

  private throwWorkspaceResultError(
    result: {
      code?: string;
      error?: string;
    },
    fallback: string,
  ): never {
    const message = result.error?.trim() || fallback;
    if (typeof result.code === "string" && result.code.trim().length > 0) {
      throw createCalSciWorkspaceError(
        result.code as Parameters<typeof createCalSciWorkspaceError>[0],
        message,
      );
    }
    throw new Error(message);
  }

  private async openTerminal(): Promise<void> {
    if (!this.backendReady) {
      void vscode.window.showErrorMessage("CalSci backend is still initializing.");
      return;
    }

    let port: string;
    try {
      port = await this.resolvePortForOperation();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
      return;
    }

    this.showReplTerminal(false);
  }

  private queueTerminalInput(data: string): Promise<void> {
    const pending = this.terminalInputQueue.then(() => this.sendTerminalInputToDevice(data));
    this.terminalInputQueue = pending.then(
      () => undefined,
      () => undefined,
    );
    return pending;
  }

  private async sendTerminalInputToDevice(data: string): Promise<void> {
    if (!data) {
      return;
    }
    if (!this.backendReady) {
      throw new Error("CalSci backend is still initializing.");
    }
    if (this.operationInFlight > 0 || this.sessionOpenInFlight) {
      throw new Error("CalSci is busy with another operation.");
    }
    if (this.pendingHybridRestorePort || this.hybridRestoreInFlight) {
      throw new Error("CalSci is waiting for the previous interactive run to settle.");
    }

    let port = this.selectedPort;
    const selectedSessionOpen = Boolean(port && this.sessionState.connected && this.sessionState.port === port);
    if (!selectedSessionOpen) {
      port = await this.resolvePortForOperation();
      const connected = await this.ensureSessionForPort(port, {
        force: false,
        notifyOnError: false,
        showTerminal: false,
      });
      if (!connected) {
        throw new Error(`Failed to open CalSci session on ${port}.`);
      }
    }

    this.terminalInteractionInFlight = true;
    this.refreshStatus();

    try {
      await this.backend.sendTerminalInput(data);
      this.lastSessionError = undefined;
    } catch (error) {
      this.terminalInteractionInFlight = false;
      this.refreshStatus();
      throw error;
    }
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

    type FolderChoice = vscode.QuickPickItem & {
      folderPath?: string;
      browse?: boolean;
    };

    const picks: FolderChoice[] = [
      {
        label: "$(folder-opened) Select Folder...",
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
      title: "CalSci: Select Folder",
      placeHolder: "Choose a remembered folder or browse for another folder",
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
      title: "CalSci: Select Local Folder",
    });
    if (!picked || picked.length === 0) {
      return undefined;
    }
    return path.resolve(picked[0].fsPath);
  }

  private handleWorkspaceTextChanged(event: vscode.TextDocumentChangeEvent): void {
    if (event.contentChanges.length === 0 || event.document.uri.scheme !== "calsci") {
      return;
    }

    this.workspaceSyncState.set(event.document.uri.toString(), "pending");
    this.updateWorkspaceSyncStatus();
  }

  private updateWorkspaceSyncStatus(): void {
    const activeUri = vscode.window.activeTextEditor?.document.uri;
    if (!activeUri || activeUri.scheme !== "calsci") {
      this.workspaceSyncItem.hide();
      return;
    }

    const state = this.workspaceSyncState.get(activeUri.toString());
    if (state === "pending") {
      this.workspaceSyncItem.text = "$(sync~spin) Sync pending";
      this.workspaceSyncItem.tooltip = "CalSci detected edits in this file and is waiting for the save/write to finish.";
      this.workspaceSyncItem.show();
      return;
    }

    if (state === "synced") {
      this.workspaceSyncItem.text = "$(cloud-upload) Synced to device";
      this.workspaceSyncItem.tooltip = "The latest saved content was acknowledged by the CalSci device.";
      this.workspaceSyncItem.show();
      return;
    }

    this.workspaceSyncItem.hide();
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
    const trimmed = existing.slice(0, MAX_SYNC_FOLDER_HISTORY);
    if (trimmed.length !== stored.length || trimmed.length !== existing.length) {
      await this.context.globalState.update(SYNC_FOLDER_HISTORY_KEY, trimmed);
    }
    return trimmed;
  }

  private async rememberSyncFolder(folderPath: string): Promise<void> {
    const resolved = path.resolve(folderPath);
    const history = await this.loadSyncFolderHistory();
    const next = [resolved, ...history.filter((entry) => entry !== resolved)].slice(0, MAX_SYNC_FOLDER_HISTORY);
    await this.context.globalState.update(SYNC_FOLDER_HISTORY_KEY, next);
  }

  private async buildSyncFolderSelection(folderPath: string): Promise<SyncFolderSelection> {
    const localFolder = path.resolve(folderPath);
    return {
      localFolder,
      remoteFolder: "/",
      deleteExtraneous: true,
    };
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

  private shouldAutoConnectOnDetect(): boolean {
    return vscode.workspace.getConfiguration("calsci").get<boolean>("autoConnectOnDetect", false);
  }

  private shouldAutoScanWorkspace(): boolean {
    return vscode.workspace.getConfiguration("calsci").get<boolean>("autoScanWorkspace", false);
  }

  private async waitForOperationToSettle(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + Math.max(0, timeoutMs);
    while (this.operationInFlight > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return this.operationInFlight === 0;
  }

  private async stopHybridForExclusiveOperation(): Promise<boolean> {
    if (!this.hybridStatus.active) {
      return false;
    }

    try {
      const snapshot = await this.backend.stopHybrid();
      this.applyHybridSnapshot(snapshot);
      return !this.hybridStatus.active;
    } catch {
      // Best effort only. The following exclusive operation still pauses normal polling.
      return false;
    }
  }

  private async restoreHybridWithRetries(port: string): Promise<boolean> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const connected = await this.ensureSessionForPort(port, {
          force: true,
          notifyOnError: false,
          showTerminal: false,
        });
        if (!connected) {
          continue;
        }

        const snapshot = await this.backend.startHybrid(port);
        this.applyHybridSnapshot(snapshot);
        if (snapshot.ok) {
          return true;
        }
      } catch {
        // Retry once after a short settle delay.
      }

      if (attempt === 0) {
        await new Promise((resolve) => setTimeout(resolve, 350));
      }
    }

    return false;
  }

  private async restoreHybridAfterRun(port: string, waitForPrompt: boolean): Promise<void> {
    if (waitForPrompt) {
      this.pendingHybridRestorePort = port;
      void this.tryRestoreHybridAfterPrompt();
      return;
    }

    this.pendingHybridRestorePort = undefined;
    const restored = await this.restoreHybridWithRetries(port);
    if (!restored) {
      void vscode.window.showWarningMessage(
        `Run finished on ${port}, but hybrid helper did not restart. Toggle Hybrid back on after the prompt settles.`,
      );
    }
  }

  private handleTerminalOutput(data: string): void {
    this.recentTerminalOutput = (this.recentTerminalOutput + data).slice(-256);
    if (this.terminalInteractionInFlight && this.terminalBufferHasFriendlyPrompt()) {
      this.terminalInteractionInFlight = false;
      this.refreshStatus();
    }
    void this.tryRestoreHybridAfterPrompt();
  }

  private async tryRestoreHybridAfterPrompt(): Promise<void> {
    const port = this.pendingHybridRestorePort;
    if (!port || this.hybridRestoreInFlight) {
      return;
    }
    if (!this.sessionState.connected || this.sessionState.port !== port) {
      return;
    }
    if (!this.terminalBufferHasFriendlyPrompt()) {
      return;
    }

    this.hybridRestoreInFlight = true;
    try {
      const restored = await this.restoreHybridWithRetries(port);
      this.pendingHybridRestorePort = undefined;
      if (restored) {
        this.recentTerminalOutput = "";
      } else {
        void vscode.window.showWarningMessage(
          `Interactive run finished on ${port}, but hybrid helper did not restart. Toggle Hybrid back on after the prompt settles.`,
        );
      }
    } finally {
      this.hybridRestoreInFlight = false;
    }
  }

  private terminalBufferHasFriendlyPrompt(): boolean {
    const normalized = this.recentTerminalOutput.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    return /(?:^|\n)(?:CalSci >>>|>>>)[ \t]*$/.test(normalized);
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
      onSoftReset: async () => {
        await this.softResetDevice();
      },
      onSyncFull: async () => {
        await this.syncHybridFullFromPanel();
      },
      onSyncFolder: async () => {
        await this.syncFolderCommand();
      },
      onFirmwareUpload: async () => {
        await this.uploadFirmwareFromPanel();
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
      void vscode.window.showErrorMessage(this.errorMessage(error, "No device selected."));
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

  private async uploadFirmwareFromPanel(): Promise<void> {
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

    const preferredPort = this.selectedPort?.trim() ?? "";

    const confirm = await vscode.window.showWarningMessage(
      "Flash the bundled CalSci firmware to the detected ESP device? This will stop the current CalSci session and replace all onboard firmware images.",
      {
        modal: true,
        detail: preferredPort
          ? `The extension will auto-detect the current ESP port and prefer the selected port ${preferredPort} when it is available.`
          : "The extension will auto-detect the current ESP port, then flash bootloader, partition table, OTA data, and CalOS.",
      },
      "Yes",
    );
    if (confirm !== "Yes") {
      return;
    }

    let firmwarePaths: FirmwareFlashPaths;
    try {
      firmwarePaths = await this.resolveFirmwareFlashPaths();
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Firmware flash aborted."));
      return;
    }

    let result: FirmwareFlashResult;
    try {
      result = await this.runFirmwareFlashAttempt({
        port: preferredPort,
        firmwarePaths,
        manualBootloader: false,
        progressTitle: "CalSci: Flashing bundled firmware to detected ESP device",
        clearOutput: true,
      });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Firmware flash failed."));
      return;
    }

    if (!result.ok && this.isLikelyEsptoolConnectError(result.error)) {
      const retry = await vscode.window.showWarningMessage(
        `Automatic firmware flashing could not connect to ${result.port || preferredPort || "the detected device"}. Put CalSci into bootloader mode manually, then retry.`,
        {
          modal: true,
          detail: "Hold BOOT, tap RESET, then release BOOT. The extension will auto-detect the current ESP port and retry after bootloader confirmation.",
        },
        "Retry Flash",
      );

      if (retry === "Retry Flash") {
        try {
          result = await this.runFirmwareFlashAttempt({
            port: result.port || preferredPort,
            firmwarePaths,
            manualBootloader: true,
            progressTitle: "CalSci: Waiting for bootloader mode on detected ESP device",
            clearOutput: false,
          });
        } catch (error) {
          void vscode.window.showErrorMessage(this.errorMessage(error, "Manual bootloader firmware flash failed."));
          return;
        }
      }
    }

    this.firmwareOutput.show(false);
    await this.persistSelectedPort(result.port || preferredPort || undefined);
    await this.pollDevices();
    if (result.ok) {
      const flashedPort = result.port || preferredPort;
      const postFlashReset = flashedPort ? await this.restorePromptAfterFirmwareFlash(flashedPort) : undefined;

      if (postFlashReset?.ok && postFlashReset.promptSeen) {
        if (preferredPort && flashedPort !== preferredPort) {
          void vscode.window.showInformationMessage(
            `Firmware flash complete. Device port changed to ${flashedPort}. CalSci prompt verified.`,
          );
        } else {
          void vscode.window.showInformationMessage(`Firmware flash complete on ${flashedPort}. CalSci prompt verified.`);
        }
      } else if (flashedPort) {
        const detail = postFlashReset?.error
          ? ` ${postFlashReset.error}`
          : postFlashReset?.rebootSeen
            ? " Device reboot detected, but the CalSci prompt was not yet verified."
            : "";
        if (preferredPort && flashedPort !== preferredPort) {
          void vscode.window.showWarningMessage(
            `Firmware flash complete. Device port changed to ${flashedPort}, but post-flash serial reset did not restore the CalSci prompt.${detail}`,
          );
        } else {
          void vscode.window.showWarningMessage(
            `Firmware flash complete on ${flashedPort}, but post-flash serial reset did not restore the CalSci prompt.${detail}`,
          );
        }
      } else {
        void vscode.window.showInformationMessage("Firmware flash complete.");
      }
      return;
    }

    const detail = result.error ? ` ${result.error}` : "";
    void vscode.window.showErrorMessage(`Firmware flash failed on ${result.port || preferredPort || "the detected device"}.${detail}`);
  }

  private async eraseChipFromPanel(): Promise<void> {
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

    const preferredPort = this.selectedPort?.trim() ?? "";

    const confirm = await vscode.window.showWarningMessage(
      "Erase the entire flash on the detected ESP device? This removes CalSci firmware and all stored data.",
      {
        modal: true,
        detail: preferredPort
          ? `The extension will auto-detect the current ESP port and prefer the selected port ${preferredPort} when it is available. You will need to flash firmware again before CalSci can reconnect.`
          : "The extension will auto-detect the current ESP port, erase the entire chip flash, and leave the device without firmware until you flash it again.",
      },
      "Erase Chip",
    );
    if (confirm !== "Erase Chip") {
      return;
    }

    let result: ChipEraseResult;
    try {
      result = await this.runChipEraseAttempt({
        port: preferredPort,
        manualBootloader: false,
        progressTitle: "CalSci: Erasing detected ESP chip",
        clearOutput: true,
      });
    } catch (error) {
      void vscode.window.showErrorMessage(this.errorMessage(error, "Chip erase failed."));
      return;
    }

    if (!result.ok && this.isLikelyEsptoolConnectError(result.error)) {
      const retry = await vscode.window.showWarningMessage(
        `Automatic chip erase could not connect to ${result.port || preferredPort || "the detected device"}. Put CalSci into bootloader mode manually, then retry.`,
        {
          modal: true,
          detail: "Hold BOOT, tap RESET, then release BOOT. The extension will auto-detect the current ESP port and retry after bootloader confirmation.",
        },
        "Retry Erase",
      );

      if (retry === "Retry Erase") {
        try {
          result = await this.runChipEraseAttempt({
            port: result.port || preferredPort,
            manualBootloader: true,
            progressTitle: "CalSci: Waiting for bootloader mode on detected ESP device",
            clearOutput: false,
          });
        } catch (error) {
          void vscode.window.showErrorMessage(this.errorMessage(error, "Manual bootloader chip erase failed."));
          return;
        }
      }
    }

    this.chipEraseOutput.show(false);
    await this.persistSelectedPort(result.port || preferredPort || undefined);
    await this.pollDevices({ allowSessionConnect: false });
    if (result.ok) {
      const erasedPort = result.port || preferredPort || "the detected device";
      if (preferredPort && erasedPort !== preferredPort) {
        void vscode.window.showInformationMessage(
          `Chip erase complete. Device port changed to ${erasedPort}. Flash firmware before reconnecting.`,
        );
      } else {
        void vscode.window.showInformationMessage(`Chip erase complete on ${erasedPort}. Flash firmware before reconnecting.`);
      }
      return;
    }

    const detail = result.error ? ` ${result.error}` : "";
    void vscode.window.showErrorMessage(`Chip erase failed on ${result.port || preferredPort || "the detected device"}.${detail}`);
  }

  private async runFirmwareFlashAttempt(options: {
    port: string;
    firmwarePaths: FirmwareFlashPaths;
    manualBootloader: boolean;
    progressTitle: string;
    clearOutput: boolean;
  }): Promise<FirmwareFlashResult> {
    const { port, firmwarePaths, manualBootloader, progressTitle, clearOutput } = options;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      return await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: progressTitle,
          cancellable: false,
        },
        async (progress) => {
          if (clearOutput) {
            this.firmwareOutput.clear();
          } else {
            this.firmwareOutput.appendLine("");
            this.firmwareOutput.appendLine("----");
          }
          this.firmwareOutput.appendLine(`CalSci firmware flash target: ${port || "auto-detect ESP port"}`);
          this.firmwareOutput.appendLine(`Mode:            ${manualBootloader ? "manual bootloader retry" : "automatic bootloader entry"}`);
          this.firmwareOutput.appendLine(`Bootloader:      ${firmwarePaths.bootloaderPath}`);
          this.firmwareOutput.appendLine(`Partition table: ${firmwarePaths.partitionTablePath}`);
          this.firmwareOutput.appendLine(`OTA data:        ${firmwarePaths.otaDataPath}`);
          this.firmwareOutput.appendLine(`CalOS:           ${firmwarePaths.calOsPath}`);
          this.firmwareOutput.appendLine("");
          this.firmwareOutput.show(false);

          return this.backend.flashFirmware(
            port,
            firmwarePaths,
            (line: string, isError: boolean) => {
              const formatted = isError ? `[ERROR] ${line}` : line;
              this.firmwareOutput.appendLine(formatted);
              progress.report({ message: formatted.slice(0, MAX_PROGRESS_MESSAGE_LENGTH) });
            },
            { manualBootloader },
          );
        },
      );
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
    }
  }

  private async runChipEraseAttempt(options: {
    port: string;
    manualBootloader: boolean;
    progressTitle: string;
    clearOutput: boolean;
  }): Promise<ChipEraseResult> {
    const { port, manualBootloader, progressTitle, clearOutput } = options;
    try {
      this.operationInFlight += 1;
      this.refreshStatus();
      return await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: progressTitle,
          cancellable: false,
        },
        async (progress) => {
          if (clearOutput) {
            this.chipEraseOutput.clear();
          } else {
            this.chipEraseOutput.appendLine("");
            this.chipEraseOutput.appendLine("----");
          }
          this.chipEraseOutput.appendLine(`CalSci chip erase target: ${port || "auto-detect ESP port"}`);
          this.chipEraseOutput.appendLine(`Mode: ${manualBootloader ? "manual bootloader retry" : "automatic bootloader entry"}`);
          this.chipEraseOutput.appendLine("");
          this.chipEraseOutput.show(false);

          return this.backend.eraseChip(
            port,
            (line: string, isError: boolean) => {
              const formatted = isError ? `[ERROR] ${line}` : line;
              this.chipEraseOutput.appendLine(formatted);
              progress.report({ message: formatted.slice(0, MAX_PROGRESS_MESSAGE_LENGTH) });
            },
            { manualBootloader },
          );
        },
      );
    } finally {
      this.operationInFlight = Math.max(0, this.operationInFlight - 1);
      this.refreshStatus();
    }
  }

  private async resolveFirmwareFlashPaths(): Promise<FirmwareFlashPaths> {
    const firmwareRoot = path.join(this.context.extensionPath, "firmware", "esp32s3", "latest");
    const firmwarePaths: FirmwareFlashPaths = {
      bootloaderPath: path.join(firmwareRoot, "bootloader.bin"),
      partitionTablePath: path.join(firmwareRoot, "partition-table.bin"),
      otaDataPath: path.join(firmwareRoot, "ota_data_initial.bin"),
      calOsPath: path.join(firmwareRoot, "micropython.bin"),
    };

    const requiredPaths: Array<[string, string]> = [
      ["bootloader", firmwarePaths.bootloaderPath],
      ["partition table", firmwarePaths.partitionTablePath],
      ["OTA data", firmwarePaths.otaDataPath],
      ["CalOS", firmwarePaths.calOsPath],
    ];
    for (const [label, candidatePath] of requiredPaths) {
      const resolvedPath = path.resolve(candidatePath);
      if (!await this.isFilePath(resolvedPath)) {
        throw new Error(`Bundled ${label} image not found: ${resolvedPath}. Reinstall or update the CalSci extension package.`);
      }
    }

    return firmwarePaths;
  }

  private isLikelyEsptoolConnectError(errorText: string | undefined): boolean {
    if (!errorText) {
      return false;
    }

    const lowered = errorText.toLowerCase();
    return [
      "write timeout",
      "failed to connect",
      "timed out waiting for packet header",
      "serial exception",
      "could not open port",
      "device not found",
      "no serial data received",
      "bootloader signal not detected",
      "bootloader confirmation timeout",
    ].some((needle) => lowered.includes(needle));
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
      throw new Error("No device found. Connect CalSci, then use Select Device.");
    }

    if (!this.selectedPort) {
      throw new Error("No device selected. Use CalSci: Select Device first.");
    }

    const selected = this.devices.find((device) => device.port === this.selectedPort);
    if (selected) {
      return selected.port;
    }

    const missingSelection = this.selectedPort;
    await this.persistSelectedPort(undefined);
    throw new Error(`Selected device ${missingSelection} is not available. Use CalSci: Select Device again.`);
  }

  private async pollDevices(options?: PollOptions): Promise<void> {
    if (!this.backendReady || this.pollInFlight || this.sessionOpenInFlight) {
      return;
    }

    const allowSessionConnect = options?.allowSessionConnect ?? this.shouldAutoConnectOnDetect();
    const operationActive = this.operationInFlight > 0 || this.runInFlight || this.terminalInteractionInFlight;
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
      const disconnectedPort = this.reconcileSelectedPort();
      if (disconnectedPort) {
        await this.handleSelectedPortDisconnected(disconnectedPort);
        return;
      }

      if (!this.selectedPort) {
        if (this.sessionState.connected) {
          await this.closeSessionSilently();
        }
        this.refreshStatus();
        return;
      }

      if (operationActive) {
        this.refreshStatus();
        return;
      }

      await this.closeDetachedSessionIfIdle();

      const shouldMaintainSession = this.shouldMaintainPersistentSession(Boolean(options?.showTerminalOnConnect));
      if (options?.forceSessionConnect || (allowSessionConnect && shouldMaintainSession && this.shouldAttemptSession(this.selectedPort))) {
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

  private reconcileSelectedPort(): string | undefined {
    if (!this.selectedPort) {
      return undefined;
    }
    const stillAvailable = this.devices.some((device) => device.port === this.selectedPort);
    return stillAvailable ? undefined : this.selectedPort;
  }

  private async handleSelectedPortDisconnected(port: string): Promise<void> {
    if (this.disconnectHandlingInFlight) {
      return;
    }
    if (this.selectedPort !== port && this.sessionState.port !== port) {
      return;
    }

    this.disconnectHandlingInFlight = true;
    const message = `CalSci device on ${port} disconnected.`;
    try {
      this.pendingHybridRestorePort = undefined;
      this.hybridRestoreInFlight = false;
      this.recentTerminalOutput = "";
      this.terminalInteractionInFlight = false;
      this.terminalInputQueue = Promise.resolve();

      try {
        await this.backend.abortSessionActivity("device-disconnected");
      } catch {
        // Best effort only.
      }

      this.sessionState = {
        connected: false,
        error: message,
        reason: "device-disconnected",
      };
      this.lastSessionError = message;
      this.hybridStatus = {
        ...this.hybridStatus,
        connected: false,
        active: false,
        port: undefined,
        error: message,
        reason: "device-disconnected",
      };
      this.hybridPanel?.updateSessionState(this.sessionState);
      this.hybridPanel?.updateHybridStatus(this.hybridStatus);
      await this.persistSelectedPort(undefined);
      this.refreshStatus();
      void vscode.window.showWarningMessage(message);
    } finally {
      this.disconnectHandlingInFlight = false;
    }
  }

  private async persistSelectedPort(port: string | undefined): Promise<void> {
    if (this.selectedPort === port) {
      return;
    }
    this.selectedPort = port;
    this.activeWorkspaceTarget = undefined;
    void this.clearWorkspaceFetchSession();
    await this.setWorkspaceClipboard(undefined);
    this.workspaceViewProvider.invalidate();
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

    if (this.sessionState.connected && this.sessionState.port === this.selectedPort) {
      if (options.showTerminal) {
        this.showReplTerminal(true);
      }
      return true;
    }

    if (this.sessionOpenInFlight) {
      const settled = await this.waitForSessionOpenToSettle(SESSION_OPEN_WAIT_MS);
      if (!settled) {
        this.refreshStatus();
        return false;
      }

      if (this.sessionState.connected && this.sessionState.port === this.selectedPort) {
        if (options.showTerminal) {
          this.showReplTerminal(true);
        }
        return true;
      }

      if (this.sessionOpenInFlight) {
        return false;
      }
    }

    this.sessionOpenInFlight = true;
    this.lastSessionAttemptAt = Date.now();
    this.lastSessionAttemptPort = this.selectedPort;
    if (options.showTerminal) {
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

  private async waitForSessionOpenToSettle(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + Math.max(0, timeoutMs);
    while (this.sessionOpenInFlight && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return !this.sessionOpenInFlight;
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

  private async closeSessionSilently(reason = "idle-release"): Promise<void> {
    try {
      await this.backend.closeSession(reason);
    } catch {
      // Best effort only.
    }
    this.sessionState = { connected: false };
    this.terminalInteractionInFlight = false;
    this.refreshStatus();
  }

  private async closeDetachedSessionIfIdle(): Promise<void> {
    if (!this.sessionState.connected || this.shouldMaintainPersistentSession()) {
      return;
    }
    await this.closeSessionSilently();
  }

  private async handleReplTerminalClosed(): Promise<void> {
    if (this.operationInFlight > 0 || this.runInFlight || this.sessionOpenInFlight || this.hybridStatus.active) {
      return;
    }
    await this.closeSessionSilently("terminal-closed");
  }

  private handleSessionStateChange(state: SessionState): void {
    const previousPort = this.sessionState.port ?? undefined;
    const previousConnected = this.sessionState.connected;
    this.sessionState = {
      connected: state.connected,
      port: state.port ?? undefined,
      error: state.error?.trim() || undefined,
      reason: state.reason?.trim() || undefined,
    };
    if (!this.sessionState.connected) {
      this.terminalInteractionInFlight = false;
    }
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
    const portChanged = Boolean(previousPort && this.sessionState.port && previousPort !== this.sessionState.port);
    if (!this.sessionState.connected || (this.pendingHybridRestorePort && this.sessionState.port !== this.pendingHybridRestorePort)) {
      this.pendingHybridRestorePort = undefined;
      this.hybridRestoreInFlight = false;
      this.recentTerminalOutput = "";
    }
    const selectionCleared = !this.selectedPort;
    if (portChanged || selectionCleared) {
      this.activeWorkspaceTarget = undefined;
      void this.clearWorkspaceFetchSession();
      this.workspaceViewProvider.invalidate();
    }
    this.refreshStatus();

    if (!this.sessionState.connected && previousConnected && previousPort && this.selectedPort === previousPort && state.reason === "reader-failed") {
      void this.handleSelectedPortDisconnected(previousPort);
    }
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

    const pty = new CalSciReplPseudoterminal(this.backend, async (data: string) => {
      await this.queueTerminalInput(data);
    });
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
  }

  private shouldMaintainPersistentSession(showTerminalOnConnect = false): boolean {
    return showTerminalOnConnect
      || this.hybridStatus.active
      || this.hybridRestoreInFlight
      || Boolean(this.pendingHybridRestorePort)
      || this.terminalInteractionInFlight;
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
      if (this.devices.length > 0) {
        this.setNeedsSelectionStatus("Device detected. Run Select Device to enable communication.");
        return;
      }

      this.setNoDeviceStatus("No device selected.");
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

    const reason = this.lastSessionError ?? "Selected device is available. CalSci opens the serial session on demand.";
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

  private setNeedsSelectionStatus(reason: string): void {
    this.statusItem.text = "$(plug) Select Device";
    this.statusItem.color = new vscode.ThemeColor("terminal.ansiYellow");
    this.statusItem.tooltip = reason;
    this.setRunVisible(false);
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
