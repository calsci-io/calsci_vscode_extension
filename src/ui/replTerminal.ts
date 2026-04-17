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
  private suppressNextConnectNotice = false;

  public readonly onDidWrite = this.writeEmitter.event;

  constructor(
    private readonly backend: BackendServiceClient,
    private readonly inputHandler: (data: string) => Promise<void>,
  ) {
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
    void this.inputHandler(data).then(
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
    const reason = state.reason?.trim() || undefined;
    if (state.connected) {
      this.disconnectedKey = undefined;
      this.lastInputError = undefined;
      if (this.connectedPort !== port) {
        this.connectedPort = port;
        if (!this.suppressNextConnectNotice) {
          this.writeLocalLine(`[Connected to ${port ?? "CalSci"}]`);
        }
      }
      this.suppressNextConnectNotice = false;
      return;
    }

    const error = state.error?.trim() || undefined;
    const key = `${port ?? ""}|${error ?? ""}|${reason ?? ""}`;
    if (!error && this.shouldSuppressSessionNotice(reason)) {
      this.connectedPort = undefined;
      this.disconnectedKey = key;
      this.suppressNextConnectNotice = true;
      return;
    }
    if (this.connectedPort !== undefined || error) {
      if (this.disconnectedKey !== key) {
        this.writeLocalLine(`[Session closed${error ? `: ${error}` : ""}]`);
        this.disconnectedKey = key;
      }
    }
    this.connectedPort = undefined;
    this.suppressNextConnectNotice = false;
  }

  private shouldSuppressSessionNotice(reason: string | undefined): boolean {
    return reason === "idle-release"
      || reason === "terminal-closed"
      || reason === "closed-by-command"
      || reason === "shutdown";
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
