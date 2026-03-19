import * as path from "path";
import * as vscode from "vscode";

import { type WorkspaceTreeEntry } from "../core/shared";
import { createCalSciWorkspaceUri } from "./workspaceContentProvider";

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
  children: WorkspaceNode[];
};

class CalSciWorkspaceItem extends vscode.TreeItem {
  constructor(
    public readonly kind: "placeholder" | WorkspaceNodeKind,
    label: string,
    public readonly remotePath?: string,
    public readonly port?: string,
    collapsibleState?: vscode.TreeItemCollapsibleState,
  ) {
    super(label, collapsibleState ?? vscode.TreeItemCollapsibleState.None);

    if (kind === "placeholder") {
      this.command = {
        command: "calsci.refreshWorkspace",
        title: "Refresh CalSci Workspace",
      };
      this.contextValue = "calsciWorkspacePlaceholder";
      this.iconPath = new vscode.ThemeIcon("refresh");
      return;
    }

    if (!remotePath || !port) {
      return;
    }

    if (kind === "folder") {
      this.contextValue = "calsciWorkspaceFolder";
      this.iconPath = vscode.ThemeIcon.Folder;
      return;
    }

    const resourceUri = createCalSciWorkspaceUri(remotePath, port);
    this.resourceUri = resourceUri;
    this.contextValue = "calsciWorkspaceFile";
    this.command = {
      command: "calsci.openWorkspaceFile",
      title: "Open CalSci File",
      arguments: [remotePath, port],
    };
  }
}

export class CalSciWorkspaceViewProvider implements vscode.TreeDataProvider<CalSciWorkspaceItem> {
  private readonly changeEmitter = new vscode.EventEmitter<CalSciWorkspaceItem | undefined | void>();
  private readonly root: WorkspaceNode = {
    kind: "folder",
    name: "CalSci",
    remotePath: "/",
    children: [],
  };

  private scanState: "idle" | "loading" | "ready" | "error" = "idle";
  private loadedPort: string | undefined;
  private errorMessage: string | undefined;
  private loadPromise: Promise<void> | undefined;
  private manualLoadRequested = false;

  public readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly handlers: WorkspaceViewHandlers) {}

  public invalidate(): void {
    this.scanState = "idle";
    this.loadedPort = undefined;
    this.errorMessage = undefined;
    this.loadPromise = undefined;
    this.manualLoadRequested = false;
    this.root.children = [];
    this.changeEmitter.fire();
  }

  public async reload(): Promise<void> {
    this.scanState = "idle";
    this.loadedPort = undefined;
    this.errorMessage = undefined;
    this.loadPromise = undefined;
    this.manualLoadRequested = true;
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
      return [
        new CalSciWorkspaceItem(
          "folder",
          "CalSci",
          "/",
          this.loadedPort,
          vscode.TreeItemCollapsibleState.Expanded,
        ),
      ];
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

    this.loadPromise = (async () => {
      try {
        const snapshot = await this.handlers.scanTree();
        this.loadedPort = snapshot.port;
        this.errorMessage = undefined;
        this.root.children = this.buildTree(snapshot.entries);
        this.scanState = "ready";
      } catch (error) {
        this.loadedPort = undefined;
        this.root.children = [];
        this.errorMessage = error instanceof Error ? error.message : String(error);
        this.scanState = "error";
      } finally {
        this.loadPromise = undefined;
        this.changeEmitter.fire();
      }
    })();

    await this.loadPromise;
  }

  private buildTree(entries: WorkspaceTreeEntry[]): WorkspaceNode[] {
    const nodes = new Map<string, WorkspaceNode>();
    nodes.set("/", this.root);

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

    sortChildren(this.root);
    return this.root.children;
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
    const item = new CalSciWorkspaceItem(
      node.kind,
      node.name,
      node.remotePath,
      this.loadedPort,
      node.kind === "folder"
        ? vscode.TreeItemCollapsibleState.Collapsed
        : vscode.TreeItemCollapsibleState.None,
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
