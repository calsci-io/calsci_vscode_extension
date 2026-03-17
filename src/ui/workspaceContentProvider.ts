import * as vscode from "vscode";

const CALSCI_WORKSPACE_SCHEME = "calsci";

export function createCalSciWorkspaceUri(remotePath: string, port: string): vscode.Uri {
  const normalizedPath = remotePath.startsWith("/") ? remotePath : `/${remotePath}`;
  const query = new URLSearchParams({ port }).toString();
  return vscode.Uri.from({
    scheme: CALSCI_WORKSPACE_SCHEME,
    path: normalizedPath,
    query,
  });
}

export class CalSciWorkspaceContentProvider implements vscode.TextDocumentContentProvider {
  private readonly changeEmitter = new vscode.EventEmitter<vscode.Uri>();
  private readonly contents = new Map<string, string>();

  public readonly onDidChange = this.changeEmitter.event;

  public setContent(uri: vscode.Uri, content: string): void {
    this.contents.set(uri.toString(), content);
    this.changeEmitter.fire(uri);
  }

  public clear(): void {
    this.contents.clear();
  }

  public provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? "";
  }
}
