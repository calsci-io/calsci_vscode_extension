import * as vscode from "vscode";

import { CalSciExtensionController } from "./controller/extensionController";

let controller: CalSciExtensionController | undefined;

export function activate(context: vscode.ExtensionContext): void {
  controller = new CalSciExtensionController(context);
  void controller.start();
}

export function deactivate(): void {
  if (controller) {
    controller.dispose();
    controller = undefined;
  }
}
