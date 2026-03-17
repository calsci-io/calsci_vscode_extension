# CalSci VS Code Extension

CalSci extension for the desktop-to-extension migration. Step 1 is achieved, and the Step 2 helper-path hybrid panel is now implemented in the extension pending live verification. Phased status is tracked in [MIGRATION.md](./MIGRATION.md).

## Current scope

- Persistent backend-owned serial session.
- In-extension REPL terminal backed by a VS Code `Pseudoterminal`.
- Existing V1 commands preserved:
  - `CalSci: Select Device`
  - `CalSci: Soft Reset Device`
  - `CalSci: Run Non-Interactive File`
  - `CalSci: Run Interactive File`
- `CalSci: Open Hybrid Panel` for helper-path display mirroring and keypad injection.

## Behavior

- Extension scans for strict `CalSci` USB devices.
- Backend is the single serial reader and writer while a session is open.
- Selecting a device opens and holds the port inside the extension session.
- `Run Non-Interactive File` uses raw REPL on the existing session and returns to the friendly REPL without resetting on success.
- `Run Interactive File` uses the normal REPL and MicroPython paste mode so `input()` and live terminal interaction work through the CalSci terminal.
- If a non-interactive run leaves the session in a bad state, backend recovery first tries to restore the friendly REPL and only falls back to a soft reset as a last resort.
- `Soft Reset Device` reuses the same held session.
- `Open Hybrid Panel` reuses the same held session and talks to the current helper contract (`_hyb_mode`, `_hyb_poll_state`, `_hyb_sync_full`, `_hyb_key`) through the backend.
- External terminals should not be able to claim the same port while the extension session is active.

## Settings

- `calsci.resetTimeoutSeconds` default `5`
- `calsci.runTimeoutSeconds` default `0` (`0` disables timeout)

## Development

1. `npm install`
2. `npm run compile`
3. Press `F5` in VS Code
