import * as path from "path";
import * as vscode from "vscode";

import { type WorkspaceTreeEntry } from "../core/shared";
import { createCalSciWorkspaceUri } from "./workspaceFileSystemProvider";

type WorkspaceNodeKind = "folder" | "file";

type WorkspaceTreeSnapshot = {
  port: string;
  entries: WorkspaceTreeEntry[];
};

type WorkspaceViewHandlers = {
  scanTree: () => Promise<WorkspaceTreeSnapshot>;
  shouldAutoLoad?: () => boolean;
};

type WorkspaceNode = {
  kind: WorkspaceNodeKind;
  name: string;
  remotePath: string;
  size?: number;
  checked: boolean;
  parent?: WorkspaceNode;
  children: WorkspaceNode[];
};

function setSubtreeChecked(node: WorkspaceNode, checked: boolean): void {
  node.checked = checked;
  for (const child of node.children) {
    setSubtreeChecked(child, checked);
  }
}

function syncAncestorSelection(node: WorkspaceNode | undefined): void {
  let current = node;
  while (current) {
    current.checked = current.children.length > 0 && current.children.every((child) => child.checked);
    current = current.parent;
  }
}

function collectSelectedPaths(nodes: readonly WorkspaceNode[]): string[] {
  const selectedPaths: string[] = [];

  for (const node of nodes) {
    if (node.checked) {
      selectedPaths.push(node.remotePath);
      continue;
    }

    if (node.children.length > 0) {
      selectedPaths.push(...collectSelectedPaths(node.children));
    }
  }

  return selectedPaths;
}

export class CalSciWorkspaceItem extends vscode.TreeItem {
  constructor(
    public readonly kind: "placeholder" | WorkspaceNodeKind,
    label: string,
    public readonly remotePath?: string,
    public readonly port?: string,
    collapsibleState?: vscode.TreeItemCollapsibleState,
    checkboxState?: vscode.TreeItemCheckboxState,
  ) {
    super(label, collapsibleState ?? vscode.TreeItemCollapsibleState.None);

    if (kind === "placeholder") {
      this.id = `placeholder:${label}`;
      this.command = {
        command: "calsci.refreshWorkspace",
        title: "Refresh CalSci Workspace",
      };
      this.contextValue = "calsciWorkspacePlaceholder";
      this.iconPath = new vscode.ThemeIcon("refresh");
      return;
    }

    if (checkboxState !== undefined) {
      this.checkboxState = checkboxState;
    }

    if (!remotePath || !port) {
      return;
    }

    this.id = `${port}:${remotePath}`;

    if (kind === "folder") {
      this.contextValue = remotePath === "/" ? "calsciWorkspaceRoot" : "calsciWorkspaceFolder";
      this.iconPath = vscode.ThemeIcon.Folder;
      return;
    }

    const resourceUri = createCalSciWorkspaceUri(remotePath, port);
    this.resourceUri = resourceUri;
    this.contextValue = "calsciWorkspaceFile";
    this.command = {
      command: "vscode.open",
      title: "Open CalSci File",
      arguments: [resourceUri],
    };
  }
}

export class CalSciWorkspaceViewProvider implements vscode.TreeDataProvider<CalSciWorkspaceItem> {
  private readonly changeEmitter = new vscode.EventEmitter<CalSciWorkspaceItem | undefined | void>();
  private readonly fetchStateEmitter = new vscode.EventEmitter<void>();
  private readonly root: WorkspaceNode = {
    kind: "folder",
    name: "CalSci",
    remotePath: "/",
    checked: false,
    children: [],
  };

  private scanState: "idle" | "loading" | "ready" | "error" = "idle";
  private loadedPort: string | undefined;
  private errorMessage: string | undefined;
  private loadPromise: Promise<void> | undefined;
  private manualLoadRequested = false;
  private refreshQueued = false;
  private fetchSelectionActive = false;

  public readonly onDidChangeTreeData = this.changeEmitter.event;
  public readonly onDidChangeFetchState = this.fetchStateEmitter.event;

  public get isFetchSelectionActive(): boolean {
    return this.fetchSelectionActive;
  }

  public get fetchSelectionPort(): string | undefined {
    return this.fetchSelectionActive ? this.loadedPort : undefined;
  }

  constructor(private readonly handlers: WorkspaceViewHandlers) {}

  public activateFetchSelection(port: string): boolean {
    if (this.scanState !== "ready" || this.loadedPort !== port || this.root.children.length === 0) {
      return false;
    }

    setSubtreeChecked(this.root, false);
    this.fetchSelectionActive = true;
    this.changeEmitter.fire();
    this.fetchStateEmitter.fire();
    return true;
  }

  public setFetchSelectionSnapshot(port: string, entries: WorkspaceTreeEntry[]): void {
    this.loadedPort = port;
    this.errorMessage = undefined;
    this.scanState = "ready";
    this.loadPromise = undefined;
    this.refreshQueued = false;
    this.manualLoadRequested = true;
    this.root.checked = false;
    this.root.children = this.buildTree(entries, this.root);
    this.fetchSelectionActive = true;
    this.changeEmitter.fire();
    this.fetchStateEmitter.fire();
  }

  public clearFetchSelection(): void {
    if (!this.fetchSelectionActive) {
      return;
    }

    setSubtreeChecked(this.root, false);
    this.changeEmitter.fire();
    this.fetchStateEmitter.fire();
  }

  public resetFetchSelection(): void {
    if (!this.fetchSelectionActive && this.getSelectedFetchPaths().length === 0) {
      return;
    }

    setSubtreeChecked(this.root, false);
    this.fetchSelectionActive = false;
    this.changeEmitter.fire();
    this.fetchStateEmitter.fire();
  }

  public getSelectedFetchPaths(): string[] {
    if (!this.fetchSelectionActive || this.root.children.length === 0) {
      return [];
    }

    return collectSelectedPaths([this.root]);
  }

  public handleCheckboxStateChange(
    items: ReadonlyArray<readonly [CalSciWorkspaceItem, vscode.TreeItemCheckboxState]>,
  ): boolean {
    if (!this.fetchSelectionActive || items.length === 0) {
      return false;
    }

    let changed = false;
    for (const [item, checkboxState] of items) {
      if (item.kind === "placeholder") {
        continue;
      }

      const node = this.findNode(item.remotePath ?? "/");
      if (!node) {
        continue;
      }

      const checked = checkboxState === vscode.TreeItemCheckboxState.Checked;
      setSubtreeChecked(node, checked);
      syncAncestorSelection(node.parent);
      changed = true;
    }

    if (!changed) {
      return false;
    }

    this.changeEmitter.fire();
    this.fetchStateEmitter.fire();
    return true;
  }

  public invalidate(preserveLoadedState = false): void {
    if (preserveLoadedState && this.scanState === "ready" && this.loadedPort) {
      this.manualLoadRequested = true;
      void this.refreshPreservingView();
      return;
    }

    const shouldPreserveLoad = preserveLoadedState && this.manualLoadRequested;
    const fetchStateChanged = this.fetchSelectionActive;
    this.scanState = "idle";
    this.loadedPort = undefined;
    this.errorMessage = undefined;
    this.loadPromise = undefined;
    this.refreshQueued = false;
    this.manualLoadRequested = shouldPreserveLoad;
    this.fetchSelectionActive = false;
    this.root.checked = false;
    this.root.children = [];
    this.changeEmitter.fire();
    if (fetchStateChanged) {
      this.fetchStateEmitter.fire();
    }
  }

  public async reload(): Promise<void> {
    this.manualLoadRequested = true;
    if (this.scanState === "ready" && this.loadedPort) {
      await this.refreshPreservingView();
      return;
    }

    this.scanState = "idle";
    this.loadedPort = undefined;
    this.errorMessage = undefined;
    this.loadPromise = undefined;
    this.refreshQueued = false;
    this.root.checked = false;
    this.root.children = [];
    await this.ensureLoaded();
    this.changeEmitter.fire();
  }

  public getTreeItem(element: CalSciWorkspaceItem): vscode.TreeItem {
    return element;
  }

  public async getChildren(element?: CalSciWorkspaceItem): Promise<CalSciWorkspaceItem[]> {
    const shouldAutoLoad = this.handlers.shouldAutoLoad?.() ?? true;
    if (!shouldAutoLoad && !this.manualLoadRequested && this.scanState === "idle") {
      if (element) {
        return [];
      }
      return [this.createPlaceholder("Refresh CalSci workspace to scan device")];
    }

    await this.ensureLoaded();

    if (this.scanState === "error") {
      if (element) {
        return [];
      }
      return [this.createPlaceholder(this.errorMessage ?? "Failed to scan CalSci workspace")];
    }

    if (this.scanState !== "ready") {
      if (element) {
        return [];
      }
      return [this.createPlaceholder("Loading CalSci workspace...")];
    }

    if (!element) {
      return [this.toTreeItem(this.root)];
    }

    if (element.kind === "placeholder") {
      return [];
    }

    const node = this.findNode(element.remotePath ?? "/");
    if (!node) {
      return [];
    }

    if (node.children.length === 0) {
      return [];
    }

    return node.children.map((child) => this.toTreeItem(child));
  }

  private async ensureLoaded(): Promise<void> {
    if (this.scanState === "ready" || this.scanState === "error") {
      return;
    }
    if (this.loadPromise) {
      await this.loadPromise;
      return;
    }

    this.scanState = "loading";
    this.changeEmitter.fire();

    this.loadPromise = this.loadSnapshot(false);

    await this.loadPromise;
  }

  private async refreshPreservingView(): Promise<void> {
    if (this.loadPromise) {
      this.refreshQueued = true;
      await this.loadPromise;
      return;
    }

    this.loadPromise = this.loadSnapshot(true);
    await this.loadPromise;
  }

  private async loadSnapshot(preserveExisting: boolean): Promise<void> {
    let fetchStateChanged = false;
    try {
      const snapshot = await this.handlers.scanTree();
      this.loadedPort = snapshot.port;
      this.errorMessage = undefined;
      this.root.checked = false;
      this.root.children = this.buildTree(snapshot.entries, this.root);
      this.scanState = "ready";
      if (this.fetchSelectionActive) {
        fetchStateChanged = true;
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.errorMessage = message;

      if (!preserveExisting || !this.loadedPort) {
        this.loadedPort = undefined;
        this.root.checked = false;
        this.root.children = [];
        this.scanState = "error";
        if (this.fetchSelectionActive) {
          this.fetchSelectionActive = false;
          fetchStateChanged = true;
        }
      }
    } finally {
      this.loadPromise = undefined;
      this.changeEmitter.fire();
      if (fetchStateChanged) {
        this.fetchStateEmitter.fire();
      }

      if (this.refreshQueued) {
        this.refreshQueued = false;
        void this.refreshPreservingView();
      }
    }
  }

  private buildTree(entries: WorkspaceTreeEntry[], root: WorkspaceNode): WorkspaceNode[] {
    root.checked = false;
    root.children = [];
    const nodes = new Map<string, WorkspaceNode>();
    nodes.set("/", root);

    const ensureFolder = (remotePath: string): WorkspaceNode => {
      const normalizedPath = normalizeRemotePath(remotePath);
      const existing = nodes.get(normalizedPath);
      if (existing) {
        return existing;
      }

      const parentPath = normalizedPath === "/" ? "/" : normalizeRemotePath(path.posix.dirname(normalizedPath));
      const parent = ensureFolder(parentPath);
      const node: WorkspaceNode = {
        kind: "folder",
        name: normalizedPath === "/" ? "CalSci" : path.posix.basename(normalizedPath),
        remotePath: normalizedPath,
        checked: false,
        parent,
        children: [],
      };
      nodes.set(normalizedPath, node);
      parent.children.push(node);
      return node;
    };

    for (const entry of entries) {
      const normalizedPath = normalizeRemotePath(entry.path);
      if (entry.kind === "directory") {
        ensureFolder(normalizedPath);
        continue;
      }

      const parent = ensureFolder(path.posix.dirname(normalizedPath));
      const node: WorkspaceNode = {
        kind: "file",
        name: path.posix.basename(normalizedPath),
        remotePath: normalizedPath,
        size: entry.size,
        checked: false,
        parent,
        children: [],
      };
      nodes.set(normalizedPath, node);
      parent.children.push(node);
    }

    const sortChildren = (node: WorkspaceNode): void => {
      node.children.sort((left, right) => {
        if (left.kind !== right.kind) {
          return left.kind === "folder" ? -1 : 1;
        }
        return left.name.localeCompare(right.name);
      });
      for (const child of node.children) {
        if (child.kind === "folder") {
          sortChildren(child);
        }
      }
    };

    sortChildren(root);
    return root.children;
  }

  private findNode(remotePath: string): WorkspaceNode | undefined {
    const normalizedPath = normalizeRemotePath(remotePath);
    if (normalizedPath === "/") {
      return this.root;
    }

    const walk = (node: WorkspaceNode): WorkspaceNode | undefined => {
      for (const child of node.children) {
        if (child.remotePath === normalizedPath) {
          return child;
        }
        if (child.kind === "folder") {
          const nested = walk(child);
          if (nested) {
            return nested;
          }
        }
      }
      return undefined;
    };

    return walk(this.root);
  }

  private toTreeItem(node: WorkspaceNode): CalSciWorkspaceItem {
    const checkboxState = this.fetchSelectionActive
      ? (node.checked ? vscode.TreeItemCheckboxState.Checked : vscode.TreeItemCheckboxState.Unchecked)
      : undefined;
    const item = new CalSciWorkspaceItem(
      node.kind,
      node.name,
      node.remotePath,
      this.loadedPort,
      node.kind === "folder"
        ? (node.remotePath === "/" ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed)
        : vscode.TreeItemCollapsibleState.None,
      checkboxState,
    );

    if (node.kind === "folder" && node.remotePath === "/" && this.loadedPort) {
      item.description = this.loadedPort;
    }

    if (node.kind === "file" && typeof node.size === "number") {
      item.description = formatSize(node.size);
    }

    return item;
  }

  private createPlaceholder(label: string): CalSciWorkspaceItem {
    const item = new CalSciWorkspaceItem("placeholder", label);
    item.description = "refresh";
    return item;
  }
}

function normalizeRemotePath(remotePath: string): string {
  const normalized = path.posix.normalize(remotePath.replace(/\\/g, "/"));
  if (normalized === "." || normalized === "") {
    return "/";
  }
  return normalized.startsWith("/") ? normalized : `/${normalized}`;
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 1024) {
    return `${Math.max(0, Math.round(bytes))} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
