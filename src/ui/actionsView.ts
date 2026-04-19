import * as vscode from "vscode";

type CalSciActionDefinition = {
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly command: string;
  readonly icon: string;
};

class CalSciActionItem extends vscode.TreeItem {
  constructor(action: CalSciActionDefinition) {
    super(action.label, vscode.TreeItemCollapsibleState.None);
    this.id = action.id;
    this.description = action.description;
    this.tooltip = `${action.label}\n${action.description}`;
    this.command = {
      command: action.command,
      title: action.label,
    };
    this.iconPath = new vscode.ThemeIcon(action.icon);
    this.contextValue = "calsciAction";
  }
}

const ACTIONS: readonly CalSciActionDefinition[] = [
  {
    id: "selectDevice",
    label: "Select Device",
    description: "Choose the active CalSci serial device before using CalSci commands.",
    command: "calsci.selectDevice",
    icon: "plug",
  },
  {
    id: "openHybridPanel",
    label: "Open Hybrid Panel",
    description: "Open the CalSci keypad and sync controls.",
    command: "calsci.openHybridPanel",
    icon: "preview",
  },
  {
    id: "softResetDevice",
    label: "Soft Reset",
    description: "Soft reset the selected CalSci device.",
    command: "calsci.softResetDevice",
    icon: "debug-restart",
  },
  {
    id: "flashFirmware",
    label: "Flash Firmware",
    description: "Flash the bundled latest firmware to the selected CalSci device.",
    command: "calsci.flashFirmware",
    icon: "arrow-up",
  },
  {
    id: "eraseChip",
    label: "Erase Chip",
    description: "Erase the entire ESP32-S3 flash. Reflash firmware before reconnecting.",
    command: "calsci.eraseChip",
    icon: "trash",
  },
  {
    id: "runCurrentFile",
    label: "Run Non-Interactive",
    description: "Run the active Python file through raw REPL.",
    command: "calsci.runCurrentFile",
    icon: "play",
  },
  {
    id: "runInteractiveFile",
    label: "Run Interactive",
    description: "Run the active Python file in the normal CalSci REPL.",
    command: "calsci.runInteractiveFile",
    icon: "terminal",
  },
  {
    id: "openTerminal",
    label: "Open Terminal",
    description: "Show the persistent CalSci REPL terminal.",
    command: "calsci.openTerminal",
    icon: "chip",
  },
  {
    id: "syncFolder",
    label: "Select Folder",
    description: "Choose a folder and sync its contents to CalSci root.",
    command: "calsci.syncFolder",
    icon: "sync",
  },
  {
    id: "fetchWorkspace",
    label: "Fetch All Files",
    description: "Download the full CalSci workspace into a local folder with matching structure.",
    command: "calsci.fetchWorkspace",
    icon: "cloud-download",
  },
  {
    id: "fetchWorkspacePartial",
    label: "Partial Fetch",
    description: "Choose CalSci files or folders to download into a local folder.",
    command: "calsci.fetchWorkspacePartial",
    icon: "filter",
  },
  {
    id: "clearAllFiles",
    label: "Clear All Files",
    description: "Delete all files from CalSci root and recreate empty boot.py.",
    command: "calsci.clearAllFiles",
    icon: "trash",
  },
];

export class CalSciActionsViewProvider implements vscode.TreeDataProvider<CalSciActionItem> {
  public getTreeItem(element: CalSciActionItem): vscode.TreeItem {
    return element;
  }

  public getChildren(element?: CalSciActionItem): CalSciActionItem[] {
    if (element) {
      return [];
    }
    return ACTIONS.map((action) => new CalSciActionItem(action));
  }
}
