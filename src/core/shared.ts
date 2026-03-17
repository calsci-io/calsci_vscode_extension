export const SELECTED_PORT_KEY = "selectedPort";
export const SYNC_FOLDER_HISTORY_KEY = "syncFolderHistory";
export const FIRMWARE_FLASH_PATHS_KEY = "firmwareFlashPaths";
export const POLL_INTERVAL_MS = 1000;
export const BACKEND_TIMEOUT_BUFFER_SEC = 30;
export const SESSION_RETRY_BACKOFF_MS = 3000;
export const MAX_SYNC_FOLDER_HISTORY = 10;

export type DeviceInfo = {
  port: string;
  product: string;
  description: string;
};

export type ScanResult = {
  ok: boolean;
  devices?: DeviceInfo[];
  error?: string;
};

export type SoftResetResult = {
  ok: boolean;
  promptSeen: boolean;
  rebootSeen?: boolean;
  port: string;
  output: string;
  error?: string;
};

export type RunFileResult = {
  ok: boolean;
  port: string;
  localFile: string;
  output: string;
  cancelled?: boolean;
  error?: string;
};

export type RunInteractiveFileResult = {
  ok: boolean;
  port: string;
  localFile: string;
  error?: string;
};

export type SyncFolderResult = {
  ok: boolean;
  port: string;
  localFolder: string;
  remoteFolder: string;
  filesSynced?: number;
  filesDeleted?: number;
  filesSkipped?: number;
  filesTotal?: number;
  directoriesEnsured?: number;
  bytesSynced?: number;
  error?: string;
};

export type WorkspaceImportResult = {
  ok: boolean;
  port: string;
  localFolder: string;
  filesImported?: number;
  directoriesImported?: number;
  bytesImported?: number;
  error?: string;
};

export type WorkspaceTreeEntry = {
  path: string;
  kind: "directory" | "file";
  size?: number;
};

export type WorkspaceTreeResult = {
  ok: boolean;
  port: string;
  entries?: WorkspaceTreeEntry[];
  error?: string;
};

export type WorkspaceFileResult = {
  ok: boolean;
  port: string;
  remotePath: string;
  content?: string;
  error?: string;
};

export type SyncFolderSelection = {
  localFolder: string;
  remoteFolder: string;
  deleteExtraneous: boolean;
};

export type FirmwareFlashPaths = {
  bootloaderPath: string;
  calOsPath: string;
  partitionTablePath: string;
};

export type FirmwareFlashResult = {
  ok: boolean;
  port: string;
  bootloaderPath: string;
  calOsPath: string;
  partitionTablePath: string;
  error?: string;
};

export type RunCancelResult = {
  ok: boolean;
  active?: boolean;
  cancelled?: boolean;
  requestId?: string;
  error?: string;
};

export type SessionState = {
  connected: boolean;
  port?: string | null;
  error?: string;
  reason?: string;
};

export type SessionResult = SessionState & {
  ok: boolean;
};

export type TerminalWriteResult = {
  ok: boolean;
  error?: string;
};

export type HybridStatus = {
  connected: boolean;
  active: boolean;
  port?: string | null;
  transport?: string;
  mode?: boolean;
  keyDebounceMs?: number | null;
  graphFastMs?: number | null;
  error?: string;
  reason?: string;
};

export type HybridState = {
  frame_id?: number;
  fb_seq?: number;
  fb?: string;
  fb_full?: boolean;
  fb_seen?: boolean;
  nav?: string;
  lines?: string[];
  mode?: boolean;
  capture_enabled?: boolean;
};

export type HybridSnapshotResult = {
  ok: boolean;
  status: HybridStatus;
  state: HybridState;
  error?: string;
};

export type HybridKeyResult = {
  ok: boolean;
  ack?: string;
  error?: string;
};

export type BackendReadyMessage = {
  type: "ready";
};

export type BackendStreamMessage = {
  id: string;
  type: "stream";
  stream: "stdout" | "stderr";
  line: string;
};

export type BackendResultMessage = {
  id: string;
  type: "result";
  payload: unknown;
};

export type BackendTerminalOutputEventMessage = {
  type: "event";
  event: "terminal-output";
  data: string;
};

export type BackendSessionEventMessage = {
  type: "event";
  event: "session";
  payload: SessionState;
};

export type BackendHybridStatusEventMessage = {
  type: "event";
  event: "hybrid";
  payload: {
    type: "status";
  } & HybridStatus;
};

export type BackendHybridStateEventMessage = {
  type: "event";
  event: "hybrid";
  payload: {
    type: "state";
    state: HybridState;
  };
};

export type BackendHybridEventPayload = BackendHybridStatusEventMessage["payload"] | BackendHybridStateEventMessage["payload"];

export type BackendMessage =
  | BackendReadyMessage
  | BackendStreamMessage
  | BackendResultMessage
  | BackendTerminalOutputEventMessage
  | BackendSessionEventMessage
  | BackendHybridStatusEventMessage
  | BackendHybridStateEventMessage;

export type PendingBackendRequest<T> = {
  resolve: (payload: T) => void;
  reject: (error: Error) => void;
  onStream?: (line: string, isError: boolean) => void;
};

export type ProcessResult = {
  code: number;
  stdout: string;
  stderr: string;
};

export type PollOptions = {
  forceSessionConnect?: boolean;
  showTerminalOnConnect?: boolean;
};

export type EnsureSessionOptions = {
  force?: boolean;
  notifyOnError?: boolean;
  showTerminal?: boolean;
};

export const HYBRID_KEYPAD_LABELS: string[][] = [
  ["ON", "ALPHA", "BETA", "HOME", "WIFI"],
  ["BL", "BACK", "TB", "d/dx", "ln"],
  ["<", "v", ">", "OK", "^"],
  ["|x|", "BT", "sin", "cos", "tan"],
  ["int", "pi", "e", "sum", "a/b"],
  ["log", "x^y", "sqrt", "x^2", "S<->D"],
  ["7", "8", "9", "DEL", "AC"],
  ["4", "5", "6", "*", "/"],
  ["1", "2", "3", "+", "-"],
  [".", "0", ",", "ANS", "EXE"],
];

export function mergeHybridState(base: HybridState, update: HybridState): HybridState {
  return {
    ...base,
    ...update,
    lines: update.lines ?? base.lines,
    fb: update.fb ?? base.fb,
  };
}
