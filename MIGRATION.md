# Desktop Hybrid -> VS Code Extension Migration

## Summary

Move in four steps, not one rewrite:

1. make the extension a persistent serial-session owner with an in-extension REPL,
2. add the hybrid screen/keypad in a webview using the current helper path as a temporary bridge,
3. add the real binary hybrid transport in firmware + backend parser,
4. switch the extension hybrid path from helper-poll to binary while keeping REPL visible in the same app-owned session.

This keeps risk low and preserves the current extension V1 commands during the migration.

## Status

- [x] Step 1 achieved
- [ ] Step 2 implemented in code, pending live verification
- [ ] Step 3 pending
- [ ] Step 4 pending

## Step 1: Persistent Session Foundation

### Goal

- Extend the VS Code extension from per-command open/close into a long-lived backend session owner.
- Keep existing commands working through that session:
  - `Select Device`
  - `Run Current File`
  - `Soft Reset Device`
- Add an in-extension REPL terminal using a VS Code `Pseudoterminal` backed by the Python backend service.
- Make the backend the single reader/writer for the serial port.
- Do not add hybrid UI in this step.

### Achieved

- Extension can connect and hold the port.
- REPL is usable inside VS Code.
- Current V1 commands still work through the held session.
- Outside terminals are blocked while the extension session owns the port.

### Remaining Step 1 Stability Note

- One concept is still tracked after Step 1 verification: repeated raw REPL crossings between `CalSci Run` and the normal REPL can still push the device/runtime into a "dirty" state over time.
- Treat this as a device/runtime stability concern, not as a blocker for Step 1 acceptance.
- Practical meaning:
  - Step 1 is achieved.
  - The remaining work is to make repeated raw-REPL run/return cycles less likely to leave the device filesystem/runtime in a mismatched or degraded state.
  - If this symptom returns after host-side rollback but improves after reflashing, suspect device-state drift first.

## Step 2: Hybrid Webview Using Current Helper Path

- Add a VS Code `Webview Panel` for:
  - display mirror
  - keypad
  - connect/hybrid status
- Reuse the Step 1 persistent backend session.
- Use the current helper contract temporarily:
  - `_hyb_mode(True/False)`
  - `_hyb_poll_state(last_frame_id)`
  - `_hyb_sync_full()`
  - `_hyb_key(col,row)`
- Keep REPL terminal and hybrid panel inside the same extension-owned app session.

### Current code state

- The extension now has a `CalSci: Open Hybrid Panel` webview command.
- The Python backend owns the helper-path hybrid polling loop so the extension process still never reads the serial port directly.
- Hybrid helper commands now go through the held session:
  - `_hyb_mode(True/False)`
  - `_hyb_sync_full()`
  - `_hyb_poll_state(last_frame_id)`
  - `_hyb_key(col,row)`
- The webview receives display/state updates and sends keypad presses through backend commands and events.
- This is still marked pending until it is live-tested on hardware from inside VS Code.

### Step 2 Success Criteria

- Hybrid can be toggled on/off from the extension.
- Display mirrors in the webview.
- Keypad injection works.
- REPL still appears in the extension terminal.
- No external terminal can attach while the extension owns the port.

## Step 3: Binary Hybrid Transport

- Add the real hybrid binary mode in firmware/backend for long-term transport.
- Firmware behavior:
  - `HYBRID_OFF` = normal REPL behavior
  - `HYBRID_ON` = binary display stream enabled, app-owned port session expected
- Backend parser behavior:
  - one serial reader loop
  - valid framed binary packets -> hybrid pipeline
  - all other non-packet bytes -> REPL terminal text
- Binary protocol defaults:
  - `magic`
  - `type`
  - `sequence`
  - `payload length`
  - `payload`
  - `CRC`
- Keypad and hybrid control should move to controlled packets, not helper REPL calls.

### Step 3 Success Criteria

- Display stream is no longer dependent on REPL polling.
- Keypad path no longer depends on `_hyb_key`.
- REPL text remains visible in-app.
- Port remains single-owner.

## Step 4: Cutover Extension to Binary

- Switch the extension hybrid panel from helper-poll transport to binary packets.
- Keep the REPL terminal running from the same backend session.
- Keep helper functions only as fallback/debug tooling while `HYBRID_OFF`.
- Remove extension dependence on helper-poll transport for normal hybrid operation.

### Step 4 Success Criteria

- Extension hybrid uses binary stream by default.
- REPL still works inside the extension.
- Outside terminals stay blocked while the extension owns the session.
- Hybrid off returns to normal REPL-only behavior.

## Interface Changes

### Extension / Backend

- Add persistent session commands such as `session.open`, `session.close`, terminal input/output streaming, and hybrid start/stop.
- Add webview messaging for display frames, keypad events, and session state.

### Firmware / Device

- Keep current helper API for staged migration.
- Add explicit binary hybrid mode and framed packet transport for the final cutover.

### REPL Terminal

- Implement it as a VS Code `Pseudoterminal` backed by the backend session.

## Test Plan

### Step 1

- connect from extension terminal
- verify V1 commands still work
- verify outside terminal gets busy/no-device while extension holds port

### Step 2

- toggle hybrid on/off
- verify helper-based display updates and keypad injection
- verify REPL remains visible in extension only

### Step 3

- validate packet framing, CRC failure recovery, and reconnect resync
- confirm parser correctly separates binary packets from plain REPL text

### Step 4

- verify end-to-end hybrid binary streaming
- verify keypad/control packets
- verify hybrid off returns to normal REPL behavior

## Assumptions

- Use the existing Python backend service in `calsci_vscode_extension/backend` as the serial/session owner.
- Use a VS Code `Webview Panel` for the hybrid UI.
- Use a VS Code `Pseudoterminal` for the in-extension REPL.
- Preserve current V1 commands during Step 1 rather than breaking and restoring them later.
- Use the current helper-based device path only as a temporary migration stage before binary cutover.
