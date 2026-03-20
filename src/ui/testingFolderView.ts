import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

type TestingFolderNodeKind = "folder" | "file";

type TestingFolderViewHandlers = {
  getWorkspaceFolder: () => vscode.WorkspaceFolder | undefined;
};

class CalSciTestingFolderItem extends vscode.TreeItem {
  constructor(
    public readonly kind: "placeholder" | TestingFolderNodeKind,
    label: string,
    public readonly fileUri?: vscode.Uri,
    collapsibleState?: vscode.TreeItemCollapsibleState,
  ) {
    super(label, collapsibleState ?? vscode.TreeItemCollapsibleState.None);

    if (kind === "placeholder") {
      this.contextValue = "calsciTestingPlaceholder";
      this.iconPath = new vscode.ThemeIcon("info");
      return;
    }

    if (!fileUri) {
      return;
    }

    this.id = fileUri.toString();
    this.resourceUri = fileUri;
    this.tooltip = fileUri.fsPath;

    if (kind === "folder") {
      this.contextValue = "calsciTestingFolder";
      this.iconPath = vscode.ThemeIcon.Folder;
      return;
    }

    this.contextValue = "calsciTestingFile";
    this.command = {
      command: "vscode.open",
      title: "Open File",
      arguments: [fileUri],
    };
  }
}

export class CalSciTestingFolderViewProvider implements vscode.TreeDataProvider<CalSciTestingFolderItem> {
  private readonly changeEmitter = new vscode.EventEmitter<CalSciTestingFolderItem | undefined | void>();

  public readonly onDidChangeTreeData = this.changeEmitter.event;

  constructor(private readonly handlers: TestingFolderViewHandlers) {}

  public invalidate(): void {
    this.changeEmitter.fire();
  }

  public getTreeItem(element: CalSciTestingFolderItem): vscode.TreeItem {
    return element;
  }

  public async getChildren(element?: CalSciTestingFolderItem): Promise<CalSciTestingFolderItem[]> {
    const workspaceFolder = this.handlers.getWorkspaceFolder();
    if (!workspaceFolder || workspaceFolder.uri.scheme !== "file") {
      return [this.createPlaceholder("Open a local folder in VS Code to use this view")];
    }

    const rootPath = path.resolve(workspaceFolder.uri.fsPath);
    if (!(await this.isDirectoryPath(rootPath))) {
      return [this.createPlaceholder("Opened folder is not available")];
    }

    if (!element) {
      return [this.createRootItem(workspaceFolder.name, rootPath)];
    }

    if (element.kind !== "folder" || !element.fileUri) {
      return [];
    }

    return this.readDirectoryChildren(element.fileUri);
  }

  private createRootItem(label: string, rootPath: string): CalSciTestingFolderItem {
    const rootUri = vscode.Uri.file(rootPath);
    const item = new CalSciTestingFolderItem(
      "folder",
      label,
      rootUri,
      vscode.TreeItemCollapsibleState.Expanded,
    );
    item.description = rootPath;
    return item;
  }

  private createPlaceholder(label: string): CalSciTestingFolderItem {
    const item = new CalSciTestingFolderItem("placeholder", label);
    item.description = "open folder";
    return item;
  }

  private async readDirectoryChildren(directoryUri: vscode.Uri): Promise<CalSciTestingFolderItem[]> {
    try {
      const entries = await fs.promises.readdir(directoryUri.fsPath, { withFileTypes: true });
      entries.sort((left, right) => {
        const leftIsDir = left.isDirectory();
        const rightIsDir = right.isDirectory();
        if (leftIsDir !== rightIsDir) {
          return leftIsDir ? -1 : 1;
        }
        return left.name.localeCompare(right.name);
      });

      return entries.map((entry) => {
        const childUri = vscode.Uri.file(path.join(directoryUri.fsPath, entry.name));
        if (entry.isDirectory()) {
          return new CalSciTestingFolderItem(
            "folder",
            entry.name,
            childUri,
            vscode.TreeItemCollapsibleState.Collapsed,
          );
        }
        return new CalSciTestingFolderItem("file", entry.name, childUri);
      });
    } catch {
      return [];
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
}
