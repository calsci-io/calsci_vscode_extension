# CalSci VS Code Extension

CalSci Step 1 extension for the desktop-to-extension migration.

## Current scope

- Persistent backend-owned serial session.
- In-extension REPL terminal backed by a VS Code `Pseudoterminal`.
- Existing V1 commands preserved:
  - `CalSci: Select Device`
  - `CalSci: Soft Reset Device`
  - `CalSci: Run Current File`
- No hybrid mirror or keypad yet.

## Behavior

- Extension scans for strict `CalSci` USB devices.
- Backend is the single serial reader and writer while a session is open.
- Selecting a device opens and holds the port inside the extension session.
- `Run Current File` uses raw REPL on the existing session and returns to the friendly REPL without resetting on success.
- If a run leaves the session in a bad state, backend recovery first tries to restore the friendly REPL and only falls back to a soft reset as a last resort.
- `Soft Reset Device` reuses the same held session.
- External terminals should not be able to claim the same port while the extension session is active.

## Settings

- `calsci.resetTimeoutSeconds` default `5`
- `calsci.runTimeoutSeconds` default `300`

## Development

1. `npm install`
2. `npm run compile`
3. Press `F5` in VS Code
