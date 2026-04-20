import * as path from "path";
import * as vscode from "vscode";

import {
  type WorkspaceDirectoryEntry,
  type WorkspaceEntryKind,
  type WorkspaceErrorCode,
  type WorkspaceStat,
} from "../core/shared";

const CALSCI_WORKSPACE_SCHEME = "calsci";

type WorkspaceWriteFileOptions = {
  create: boolean;
  overwrite: boolean;
};

type WorkspaceRenameOptions = {
  overwrite: boolean;
};

type WorkspaceDeleteOptions = {
  recursive: boolean;
};

type WorkspaceProviderError = Error & {
  code?: WorkspaceErrorCode;
};

type WorkspaceFileSystemHandlers = {
  stat: (uri: vscode.Uri) => Promise<WorkspaceStat>;
  readDirectory: (uri: vscode.Uri) => Promise<WorkspaceDirectoryEntry[]>;
  readFile: (uri: vscode.Uri) => Promise<Uint8Array>;
  writeFile: (uri: vscode.Uri, content: Uint8Array, options: WorkspaceWriteFileOptions) => Promise<void>;
  createDirectory: (uri: vscode.Uri) => Promise<void>;
  delete: (uri: vscode.Uri, options: WorkspaceDeleteOptions) => Promise<void>;
  rename: (oldUri: vscode.Uri, newUri: vscode.Uri, options: WorkspaceRenameOptions) => Promise<void>;
};

export function normalizeCalSciRemotePath(remotePath: string): string {
  const normalized = path.posix.normalize(remotePath.replace(/\\/g, "/"));
  if (normalized === "." || normalized === "") {
    return "/";
  }
  return normalized.startsWith("/") ? normalized : `/${normalized}`;
}

export function createCalSciWorkspaceUri(remotePath: string, port: string): vscode.Uri {
  const normalizedPath = normalizeCalSciRemotePath(remotePath);
  const query = new URLSearchParams({ port }).toString();
  return vscode.Uri.from({
    scheme: CALSCI_WORKSPACE_SCHEME,
    path: normalizedPath,
    query,
  });
}

export function parseCalSciWorkspaceUri(uri: vscode.Uri): { remotePath: string; port: string } {
  if (uri.scheme !== CALSCI_WORKSPACE_SCHEME) {
    throw createCalSciWorkspaceError("EINVAL", `Unsupported CalSci workspace URI scheme: ${uri.scheme}`);
  }

  const port = new URLSearchParams(uri.query).get("port")?.trim();
  if (!port) {
    throw createCalSciWorkspaceError("EINVAL", `CalSci workspace URI is missing a device port: ${uri.toString()}`);
  }

  return {
    remotePath: normalizeCalSciRemotePath(uri.path),
    port,
  };
}

export function createCalSciWorkspaceError(code: WorkspaceErrorCode, message: string): Error {
  const error = new Error(message) as WorkspaceProviderError;
  error.code = code;
  return error;
}

export function getCalSciWorkspaceErrorCode(error: unknown): WorkspaceErrorCode | undefined {
  if (!error || typeof error !== "object") {
    return undefined;
  }
  const code = "code" in error ? (error as { code?: unknown }).code : undefined;
  return typeof code === "string" ? (code as WorkspaceErrorCode) : undefined;
}

export function getCalSciWorkspaceParentUri(uri: vscode.Uri): vscode.Uri {
  const { port, remotePath } = parseCalSciWorkspaceUri(uri);
  const parentPath = remotePath === "/" ? "/" : normalizeCalSciRemotePath(path.posix.dirname(remotePath));
  return createCalSciWorkspaceUri(parentPath, port);
}

export function createCalSciWorkspaceChildUri(parentUri: vscode.Uri, name: string): vscode.Uri {
  const { port, remotePath } = parseCalSciWorkspaceUri(parentUri);
  const childPath = remotePath === "/"
    ? `/${name}`
    : normalizeCalSciRemotePath(path.posix.join(remotePath, name));
  return createCalSciWorkspaceUri(childPath, port);
}

function toFileType(kind: WorkspaceEntryKind): vscode.FileType {
  return kind === "directory" ? vscode.FileType.Directory : vscode.FileType.File;
}

function toFileStat(stat: WorkspaceStat): vscode.FileStat {
  return {
    type: toFileType(stat.kind),
    ctime: stat.ctime ?? 0,
    mtime: stat.mtime ?? 0,
    size: stat.size,
  };
}

export class CalSciWorkspaceFileSystemProvider implements vscode.FileSystemProvider {
  private readonly changeEmitter = new vscode.EventEmitter<vscode.FileChangeEvent[]>();

  public readonly onDidChangeFile = this.changeEmitter.event;

  constructor(private readonly handlers: WorkspaceFileSystemHandlers) {}

  public watch(_uri: vscode.Uri, _options: { recursive: boolean; excludes: string[] }): vscode.Disposable {
    return new vscode.Disposable(() => undefined);
  }

  public async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
    try {
      return toFileStat(await this.handlers.stat(uri));
    } catch (error) {
      throw this.asFileSystemError(error);
    }
  }

  public async readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
    try {
      const entries = await this.handlers.readDirectory(uri);
      return entries.map((entry) => [entry.name, toFileType(entry.kind)]);
    } catch (error) {
      throw this.asFileSystemError(error);
    }
  }

  public async readFile(uri: vscode.Uri): Promise<Uint8Array> {
    try {
      return await this.handlers.readFile(uri);
    } catch (error) {
      throw this.asFileSystemError(error);
    }
  }

  public async writeFile(
    uri: vscode.Uri,
    content: Uint8Array,
    options: { create: boolean; overwrite: boolean },
  ): Promise<void> {
    const existed = await this.exists(uri);

    try {
      await this.handlers.writeFile(uri, content, {
        create: options.create,
        overwrite: options.overwrite,
      });
    } catch (error) {
      throw this.asFileSystemError(error);
    }

    if (existed) {
      this.notifyChanged(uri);
      return;
    }
    this.notifyCreated(uri);
  }

  public async createDirectory(uri: vscode.Uri): Promise<void> {
    const existed = await this.exists(uri);
    if (existed) {
      return;
    }

    try {
      await this.handlers.createDirectory(uri);
    } catch (error) {
      throw this.asFileSystemError(error);
    }

    this.notifyCreated(uri);
  }

  public async delete(uri: vscode.Uri, options: { recursive: boolean; useTrash: boolean }): Promise<void> {
    try {
      await this.handlers.delete(uri, {
        recursive: options.recursive,
      });
    } catch (error) {
      throw this.asFileSystemError(error);
    }

    this.notifyDeleted(uri);
  }

  public async rename(oldUri: vscode.Uri, newUri: vscode.Uri, options: { overwrite: boolean }): Promise<void> {
    try {
      await this.handlers.rename(oldUri, newUri, {
        overwrite: options.overwrite,
      });
    } catch (error) {
      throw this.asFileSystemError(error);
    }

    this.notifyRenamed(oldUri, newUri);
  }

  public notifyChanged(uri: vscode.Uri): void {
    this.emitMutationEvents([
      { type: vscode.FileChangeType.Changed, uri },
    ]);
  }

  public notifyCreated(uri: vscode.Uri): void {
    this.emitMutationEvents([
      { type: vscode.FileChangeType.Created, uri },
    ]);
  }

  public notifyDeleted(uri: vscode.Uri): void {
    this.emitMutationEvents([
      { type: vscode.FileChangeType.Deleted, uri },
    ]);
  }

  public notifyRenamed(oldUri: vscode.Uri, newUri: vscode.Uri): void {
    this.emitMutationEvents([
      { type: vscode.FileChangeType.Deleted, uri: oldUri },
      { type: vscode.FileChangeType.Created, uri: newUri },
    ]);
  }

  private emitMutationEvents(events: vscode.FileChangeEvent[]): void {
    const allEvents = [...events];
    const seenParents = new Set<string>();

    for (const event of events) {
      const parent = getCalSciWorkspaceParentUri(event.uri);
      if (parent.toString() === event.uri.toString()) {
        continue;
      }
      const key = parent.toString();
      if (seenParents.has(key)) {
        continue;
      }
      seenParents.add(key);
      allEvents.push({ type: vscode.FileChangeType.Changed, uri: parent });
    }

    this.changeEmitter.fire(allEvents);
  }

  private async exists(uri: vscode.Uri): Promise<boolean> {
    try {
      await this.handlers.stat(uri);
      return true;
    } catch (error) {
      if (getCalSciWorkspaceErrorCode(error) === "ENOENT") {
        return false;
      }
      if (error instanceof vscode.FileSystemError && /File not found/i.test(error.message)) {
        return false;
      }
      throw this.asFileSystemError(error);
    }
  }

  private asFileSystemError(error: unknown): Error {
    if (error instanceof vscode.FileSystemError) {
      return error;
    }

    const message = error instanceof Error && error.message.trim().length > 0
      ? error.message
      : "CalSci workspace operation failed.";

    switch (getCalSciWorkspaceErrorCode(error)) {
      case "ENOENT":
        return vscode.FileSystemError.FileNotFound(message);
      case "EEXIST":
        return vscode.FileSystemError.FileExists(message);
      case "ENOTDIR":
        return vscode.FileSystemError.FileNotADirectory(message);
      case "EISDIR":
        return vscode.FileSystemError.FileIsADirectory(message);
      case "EPERM":
      case "ENOTEMPTY":
      case "ENOSPC":
        return vscode.FileSystemError.NoPermissions(message);
      case "EINVAL":
        return vscode.FileSystemError.Unavailable(message);
      default:
        return new vscode.FileSystemError(message);
    }
  }
}