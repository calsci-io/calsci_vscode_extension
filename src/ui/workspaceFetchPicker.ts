import * as vscode from "vscode";

import { type WorkspaceTreeEntry } from "../core/shared";

type WorkspaceFetchTreeNode = {
  path: string;
  name: string;
  kind: "directory" | "file";
  size?: number;
  children: WorkspaceFetchTreeNode[];
};

function normalizeRemotePath(remotePath: string): string {
  const trimmed = remotePath.trim();
  if (!trimmed || trimmed === "/") {
    return "/";
  }
  return `/${trimmed.replace(/^\/+/, "").replace(/\/+/g, "/")}`;
}

function compareTreeNodes(left: WorkspaceFetchTreeNode, right: WorkspaceFetchTreeNode): number {
  if (left.kind !== right.kind) {
    return left.kind === "directory" ? -1 : 1;
  }
  return left.name.localeCompare(right.name, undefined, { sensitivity: "base" });
}

function sortTree(nodes: WorkspaceFetchTreeNode[]): void {
  nodes.sort(compareTreeNodes);
  for (const node of nodes) {
    if (node.children.length > 0) {
      sortTree(node.children);
    }
  }
}

function buildWorkspaceFetchTree(entries: WorkspaceTreeEntry[]): WorkspaceFetchTreeNode[] {
  const root: WorkspaceFetchTreeNode = {
    path: "/",
    name: "/",
    kind: "directory",
    children: [],
  };
  const nodesByPath = new Map<string, WorkspaceFetchTreeNode>([[root.path, root]]);

  const sortedEntries = [...entries].sort((left, right) => left.path.localeCompare(right.path));
  for (const entry of sortedEntries) {
    const normalizedPath = normalizeRemotePath(entry.path);
    if (normalizedPath === "/") {
      continue;
    }

    const segments = normalizedPath.split("/").filter(Boolean);
    let currentNode = root;
    let currentPath = "";

    for (let index = 0; index < segments.length; index += 1) {
      const segment = segments[index];
      const isLeaf = index === segments.length - 1;
      currentPath += `/${segment}`;

      let nextNode = nodesByPath.get(currentPath);
      if (!nextNode) {
        nextNode = {
          path: currentPath,
          name: segment,
          kind: isLeaf ? entry.kind : "directory",
          size: isLeaf ? entry.size : undefined,
          children: [],
        };
        currentNode.children.push(nextNode);
        nodesByPath.set(currentPath, nextNode);
      } else if (isLeaf) {
        nextNode.kind = entry.kind;
        nextNode.size = entry.size;
      }

      currentNode = nextNode;
    }
  }

  sortTree(root.children);
  return root.children;
}

function sanitizeSelection(paths: unknown): string[] {
  if (!Array.isArray(paths)) {
    return [];
  }

  const unique = new Set<string>();
  for (const value of paths) {
    if (typeof value !== "string") {
      continue;
    }
    const normalizedPath = normalizeRemotePath(value);
    if (normalizedPath !== "/") {
      unique.add(normalizedPath);
    }
  }

  return [...unique].sort((left, right) => left.localeCompare(right));
}

export class WorkspaceFetchPicker implements vscode.Disposable {
  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];
  private readonly selectionPromise: Promise<string[] | undefined>;

  private resolveSelection: ((value: string[] | undefined) => void) | undefined;
  private settled = false;
  private disposed = false;

  constructor(entries: WorkspaceTreeEntry[]) {
    const tree = buildWorkspaceFetchTree(entries);

    this.selectionPromise = new Promise<string[] | undefined>((resolve) => {
      this.resolveSelection = resolve;
    });

    this.panel = vscode.window.createWebviewPanel(
      "calsciWorkspaceFetchPicker",
      "CalSci Partial Fetch",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      },
    );
    this.panel.webview.html = this.renderHtml(tree);

    this.disposables.push(
      this.panel.onDidDispose(() => {
        if (!this.settled) {
          this.finish(undefined);
        }
      }),
      this.panel.webview.onDidReceiveMessage((message: unknown) => {
        if (!message || typeof message !== "object") {
          return;
        }
        const payload = message as Record<string, unknown>;
        const type = typeof payload.type === "string" ? payload.type : "";
        if (type === "cancel") {
          this.finish(undefined);
          return;
        }
        if (type === "submit") {
          const selection = sanitizeSelection(payload.paths);
          this.finish(selection.length > 0 ? selection : undefined);
        }
      }),
    );
  }

  public async pick(): Promise<string[] | undefined> {
    return this.selectionPromise;
  }

  public dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;

    for (const disposable of this.disposables.splice(0)) {
      disposable.dispose();
    }

    try {
      this.panel.dispose();
    } catch {
      // Best effort when the panel was already closed.
    }
  }

  private finish(selection: string[] | undefined): void {
    if (this.settled) {
      return;
    }
    this.settled = true;
    this.resolveSelection?.(selection);
    this.dispose();
  }

  private renderHtml(tree: WorkspaceFetchTreeNode[]): string {
    const nonce = String(Date.now());
    const treeJson = JSON.stringify(tree).replace(/</g, "\\u003c");

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CalSci Partial Fetch</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: var(--vscode-editor-background, #1f1f1f);
      --panel: var(--vscode-sideBar-background, #252526);
      --border: var(--vscode-panel-border, rgba(128, 128, 128, 0.35));
      --foreground: var(--vscode-foreground, #cccccc);
      --muted: var(--vscode-descriptionForeground, #9da0a6);
      --accent: var(--vscode-button-background, #0e639c);
      --accent-foreground: var(--vscode-button-foreground, #ffffff);
      --accent-hover: var(--vscode-button-hoverBackground, #1177bb);
      --secondary: color-mix(in srgb, var(--foreground) 8%, transparent);
      --row-hover: color-mix(in srgb, var(--foreground) 8%, transparent);
      --row-active: color-mix(in srgb, var(--accent) 22%, transparent);
      --shadow: 0 18px 34px rgba(0, 0, 0, 0.18);
    }

    * {
      box-sizing: border-box;
    }

    html, body {
      height: 100%;
    }

    body {
      margin: 0;
      padding: 18px;
      background:
        radial-gradient(circle at top left, color-mix(in srgb, var(--accent) 12%, transparent), transparent 28%),
        var(--bg);
      color: var(--foreground);
      font: 13px/1.35 var(--vscode-font-family, "Segoe UI", sans-serif);
    }

    .shell {
      max-width: 860px;
      margin: 0 auto;
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .header {
      padding: 16px 18px 12px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, color-mix(in srgb, var(--panel) 84%, transparent), color-mix(in srgb, var(--panel) 96%, transparent));
    }

    .eyebrow {
      margin: 0 0 4px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.15;
      font-weight: 650;
    }

    .subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      max-width: 64ch;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 12px 18px;
      border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--panel) 94%, transparent);
    }

    .toolbar-spacer {
      flex: 1 1 auto;
    }

    .tree-wrap {
      padding: 12px;
    }

    .tree {
      max-height: 58vh;
      overflow: auto;
      padding: 6px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: color-mix(in srgb, var(--bg) 80%, var(--panel));
    }

    .tree-empty {
      padding: 18px 12px;
      text-align: center;
      color: var(--muted);
    }

    .tree-node {
      display: block;
    }

    .tree-row {
      display: flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      padding: 2px 8px;
      border-radius: 8px;
      user-select: none;
    }

    .tree-row:hover {
      background: var(--row-hover);
    }

    .tree-row.folder-open {
      background: color-mix(in srgb, var(--row-active) 55%, transparent);
    }

    .indent {
      width: calc(var(--depth) * 16px);
      flex: 0 0 auto;
    }

    .toggle,
    .toggle-spacer {
      width: 16px;
      height: 16px;
      flex: 0 0 16px;
    }

    .toggle {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      border-radius: 4px;
      font-size: 11px;
      line-height: 1;
    }

    .toggle:hover {
      background: color-mix(in srgb, var(--foreground) 10%, transparent);
      color: var(--foreground);
    }

    .check {
      width: 16px;
      height: 16px;
      margin: 0;
      flex: 0 0 16px;
      accent-color: var(--accent);
    }

    .node-icon {
      position: relative;
      width: 14px;
      height: 14px;
      flex: 0 0 14px;
    }

    .node-icon.folder::before {
      content: "";
      position: absolute;
      left: 0;
      top: 3px;
      width: 14px;
      height: 9px;
      border: 1px solid color-mix(in srgb, var(--foreground) 45%, transparent);
      border-radius: 2px;
      background: color-mix(in srgb, #d6a03d 58%, var(--panel));
    }

    .node-icon.folder::after {
      content: "";
      position: absolute;
      left: 1px;
      top: 1px;
      width: 7px;
      height: 4px;
      border: 1px solid color-mix(in srgb, var(--foreground) 40%, transparent);
      border-bottom: 0;
      border-radius: 2px 2px 0 0;
      background: color-mix(in srgb, #e5b65a 70%, var(--panel));
    }

    .node-icon.file::before {
      content: "";
      position: absolute;
      inset: 1px 2px 1px 1px;
      border: 1px solid color-mix(in srgb, var(--foreground) 40%, transparent);
      border-radius: 2px;
      background: color-mix(in srgb, var(--foreground) 12%, var(--panel));
    }

    .node-icon.file::after {
      content: "";
      position: absolute;
      right: 2px;
      top: 1px;
      width: 4px;
      height: 4px;
      border-top: 1px solid color-mix(in srgb, var(--foreground) 40%, transparent);
      border-right: 1px solid color-mix(in srgb, var(--foreground) 40%, transparent);
      background: color-mix(in srgb, var(--foreground) 20%, var(--panel));
      transform: skew(-8deg, -8deg);
    }

    .label-button {
      min-width: 0;
      padding: 0;
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }

    .label-button:hover {
      color: var(--accent);
    }

    .name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 500;
    }

    .meta {
      margin-left: auto;
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
      padding-left: 12px;
    }

    .footer {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 18px 16px;
      border-top: 1px solid var(--border);
      background: color-mix(in srgb, var(--panel) 94%, transparent);
    }

    .summary {
      color: var(--muted);
      min-width: 0;
      flex: 1 1 auto;
    }

    .button-row {
      display: flex;
      gap: 8px;
      margin-left: auto;
    }

    button.action {
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--foreground) 10%, transparent);
      color: var(--foreground);
      border-radius: 8px;
      padding: 6px 12px;
      font: inherit;
      cursor: pointer;
    }

    button.action:hover {
      background: color-mix(in srgb, var(--foreground) 14%, transparent);
    }

    button.action.primary {
      border-color: color-mix(in srgb, var(--accent) 75%, var(--border));
      background: var(--accent);
      color: var(--accent-foreground);
    }

    button.action.primary:hover {
      background: var(--accent-hover);
    }

    button.action:disabled {
      opacity: 0.5;
      cursor: default;
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <p class="eyebrow">CalSci Workspace</p>
      <h1>Partial Fetch</h1>
      <p class="subtitle">Browse the remote filesystem like a tree, expand folders inline, and choose exactly which files or folders to download.</p>
    </div>
    <div class="toolbar">
      <button class="action" id="expandAllButton" type="button">Expand all</button>
      <button class="action" id="collapseAllButton" type="button">Collapse all</button>
      <button class="action" id="selectAllButton" type="button">Select all</button>
      <button class="action" id="clearButton" type="button">Clear</button>
      <div class="toolbar-spacer"></div>
      <span class="summary" id="treeStats">Loading tree...</span>
    </div>
    <div class="tree-wrap">
      <div class="tree" id="tree"></div>
    </div>
    <div class="footer">
      <div class="summary" id="selectionSummary">No items selected.</div>
      <div class="button-row">
        <button class="action" id="cancelButton" type="button">Cancel</button>
        <button class="action primary" id="submitButton" type="button" disabled>Fetch selected</button>
      </div>
    </div>
  </div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const initialTree = ${treeJson};

    function decorateNode(node, depth) {
      return {
        path: node.path,
        name: node.name,
        kind: node.kind,
        size: node.size,
        depth: depth,
        expanded: false,
        selected: false,
        indeterminate: false,
        children: Array.isArray(node.children)
          ? node.children.map(function (child) { return decorateNode(child, depth + 1); })
          : [],
      };
    }

    const treeRoots = initialTree.map(function (node) { return decorateNode(node, 0); });
    const treeElement = document.getElementById("tree");
    const treeStatsElement = document.getElementById("treeStats");
    const selectionSummaryElement = document.getElementById("selectionSummary");
    const submitButton = document.getElementById("submitButton");

    function walk(nodes, visit) {
      for (const node of nodes) {
        visit(node);
        if (node.children.length > 0) {
          walk(node.children, visit);
        }
      }
    }

    function refreshDirectoryStates(node) {
      if (node.kind !== "directory" || node.children.length === 0) {
        node.indeterminate = false;
        return {
          selected: node.selected,
          indeterminate: false,
        };
      }

      let anySelected = false;
      let allSelected = true;
      let childIndeterminate = false;

      for (const child of node.children) {
        const state = refreshDirectoryStates(child);
        if (state.selected || state.indeterminate) {
          anySelected = true;
        }
        if (!state.selected || state.indeterminate) {
          allSelected = false;
        }
        if (state.indeterminate) {
          childIndeterminate = true;
        }
      }

      if (allSelected && anySelected) {
        node.selected = true;
        node.indeterminate = false;
      } else if (anySelected || childIndeterminate) {
        node.selected = false;
        node.indeterminate = true;
      } else {
        node.selected = false;
        node.indeterminate = false;
      }

      return {
        selected: node.selected,
        indeterminate: node.indeterminate,
      };
    }

    function refreshTreeStates() {
      for (const node of treeRoots) {
        refreshDirectoryStates(node);
      }
    }

    function setSubtreeSelected(node, selected) {
      node.selected = selected;
      node.indeterminate = false;
      for (const child of node.children) {
        setSubtreeSelected(child, selected);
      }
    }

    function collectSelectedPaths(nodes, selectedPaths) {
      for (const node of nodes) {
        if (node.kind === "directory") {
          if (node.selected && !node.indeterminate) {
            selectedPaths.push(node.path);
            continue;
          }
          if (node.children.length > 0) {
            collectSelectedPaths(node.children, selectedPaths);
          }
          continue;
        }

        if (node.selected) {
          selectedPaths.push(node.path);
        }
      }
    }

    function getSelectedPaths() {
      const selectedPaths = [];
      collectSelectedPaths(treeRoots, selectedPaths);
      return selectedPaths;
    }

    function formatByteCount(value) {
      if (!Number.isFinite(value) || value <= 0) {
        return "0 B";
      }
      const units = ["B", "KB", "MB", "GB"];
      let size = value;
      let unitIndex = 0;
      while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex += 1;
      }
      return (unitIndex === 0 ? String(Math.round(size)) : size.toFixed(size >= 10 ? 1 : 2)) + " " + units[unitIndex];
    }

    function describeNode(node) {
      if (node.kind === "directory") {
        const childCount = node.children.length;
        return childCount === 1 ? "1 item" : String(childCount) + " items";
      }
      return formatByteCount(Number(node.size) || 0);
    }

    function updateSummary() {
      const selectedPaths = getSelectedPaths();
      const selectedCount = selectedPaths.length;
      selectionSummaryElement.textContent = selectedCount === 0
        ? "No items selected."
        : String(selectedCount) + (selectedCount === 1 ? " item selected for fetch." : " items selected for fetch.");
      submitButton.disabled = selectedCount === 0;
    }

    function updateStats() {
      let fileCount = 0;
      let folderCount = 0;
      walk(treeRoots, function (node) {
        if (node.kind === "directory") {
          folderCount += 1;
        } else {
          fileCount += 1;
        }
      });
      treeStatsElement.textContent = String(folderCount) + (folderCount === 1 ? " folder" : " folders")
        + " and "
        + String(fileCount)
        + (fileCount === 1 ? " file" : " files");
    }

    function toggleNodeSelection(node, selected) {
      if (node.kind === "directory") {
        setSubtreeSelected(node, selected);
      } else {
        node.selected = selected;
      }
      refreshTreeStates();
      render();
    }

    function createNodeElement(node) {
      const wrapper = document.createElement("div");
      wrapper.className = "tree-node";

      const row = document.createElement("div");
      row.className = "tree-row" + (node.kind === "directory" && node.expanded ? " folder-open" : "");
      row.style.setProperty("--depth", String(node.depth));
      row.dataset.kind = node.kind;

      const indent = document.createElement("span");
      indent.className = "indent";
      row.appendChild(indent);

      if (node.kind === "directory" && node.children.length > 0) {
        const toggle = document.createElement("button");
        toggle.className = "toggle";
        toggle.type = "button";
        toggle.textContent = node.expanded ? "v" : ">";
        toggle.title = node.expanded ? "Collapse folder" : "Expand folder";
        toggle.addEventListener("click", function () {
          node.expanded = !node.expanded;
          render();
        });
        row.appendChild(toggle);
      } else {
        const toggleSpacer = document.createElement("span");
        toggleSpacer.className = "toggle-spacer";
        row.appendChild(toggleSpacer);
      }

      const checkbox = document.createElement("input");
      checkbox.className = "check";
      checkbox.type = "checkbox";
      checkbox.checked = node.selected;
      checkbox.indeterminate = node.indeterminate;
      checkbox.addEventListener("change", function () {
        toggleNodeSelection(node, checkbox.checked);
      });
      row.appendChild(checkbox);

      const icon = document.createElement("span");
      icon.className = "node-icon " + (node.kind === "directory" ? "folder" : "file");
      row.appendChild(icon);

      const labelButton = document.createElement("button");
      labelButton.className = "label-button";
      labelButton.type = "button";
      labelButton.title = node.path;
      labelButton.addEventListener("click", function () {
        if (node.kind === "directory") {
          node.expanded = !node.expanded;
          render();
          return;
        }
        toggleNodeSelection(node, !node.selected);
      });

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = node.name;
      labelButton.appendChild(name);
      row.appendChild(labelButton);

      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = describeNode(node);
      row.appendChild(meta);

      wrapper.appendChild(row);

      if (node.kind === "directory" && node.expanded && node.children.length > 0) {
        for (const child of node.children) {
          wrapper.appendChild(createNodeElement(child));
        }
      }

      return wrapper;
    }

    function render() {
      treeElement.replaceChildren();
      if (treeRoots.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tree-empty";
        empty.textContent = "The CalSci workspace is empty.";
        treeElement.appendChild(empty);
      } else {
        for (const node of treeRoots) {
          treeElement.appendChild(createNodeElement(node));
        }
      }
      updateStats();
      updateSummary();
    }

    document.getElementById("expandAllButton").addEventListener("click", function () {
      walk(treeRoots, function (node) {
        if (node.kind === "directory") {
          node.expanded = true;
        }
      });
      render();
    });

    document.getElementById("collapseAllButton").addEventListener("click", function () {
      walk(treeRoots, function (node) {
        if (node.kind === "directory") {
          node.expanded = false;
        }
      });
      render();
    });

    document.getElementById("selectAllButton").addEventListener("click", function () {
      for (const node of treeRoots) {
        setSubtreeSelected(node, true);
      }
      refreshTreeStates();
      render();
    });

    document.getElementById("clearButton").addEventListener("click", function () {
      for (const node of treeRoots) {
        setSubtreeSelected(node, false);
      }
      refreshTreeStates();
      render();
    });

    document.getElementById("cancelButton").addEventListener("click", function () {
      vscode.postMessage({ type: "cancel" });
    });

    submitButton.addEventListener("click", function () {
      const selectedPaths = getSelectedPaths();
      vscode.postMessage({
        type: "submit",
        paths: selectedPaths,
      });
    });

    render();
  </script>
</body>
</html>`;
  }
}
