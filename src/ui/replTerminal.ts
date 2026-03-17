import * as vscode from "vscode";

import type { SessionState } from "../core/shared";
import { BackendServiceClient } from "../backend/backendServiceClient";

export class CalSciReplPseudoterminal implements vscode.Pseudoterminal, vscode.Disposable {
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
    this.writeLocalLine("CalSci ready.");
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
        const message = error instanceof Error && error.message.trim().length > 0 ? error.message : "Failed to write to CalSci.";
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
