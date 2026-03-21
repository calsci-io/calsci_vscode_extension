# CalSci for VS Code

CalSci turns Visual Studio Code into a desktop control surface for the CalSci device. Run MicroPython code directly from the active editor, keep a persistent REPL open for terminal-level computation, sync files to the device, inspect the device workspace, and flash bundled firmware from one extension.

## Highlights

- Persistent backend-owned REPL session inside VS Code.
- Integrated CalSci terminal backed by a VS Code `Pseudoterminal`.
- Run the active Python file non-interactively over raw REPL without permanently uploading it to the device.
- Run the active Python file interactively through the normal REPL so `input()` and live terminal interaction work as expected.
- Scan for the connected CalSci device and keep a single extension-owned serial session active.
- Sync a local folder to the device workspace.
- Browse and open files from the device workspace view.
- Clear all user files from the device workspace.
- Use the hybrid panel for helper-path display mirroring and keypad injection.
- Flash bundled CalSci firmware images directly from VS Code.

## Current support

- Linux only for the `0.1.0` release.
- Packaged releases currently target the Linux architecture they were built on, such as `linux-x64`.
- Supports the current CalSci hardware model.

## Requirements

- Visual Studio Code `1.85.0` or newer.
- Working USB/serial permissions on the Linux machine.
- A CalSci device connected over USB.

## Packaged extension runtime

The packaged `.vsix` bundles its own Python runtime, Python dependencies, backend scripts, and firmware images. End users do not need to install `python3`, run `pip install`, or provide internet access on first run.

## Building the packaged runtime

When packaging a release from source, run:

```bash
npm run build
```

This stages a bundled runtime under `runtime/<platform>` and then compiles the extension. The runtime staging step looks for the CalSci builder environment created by the extension under VS Code global storage, or you can point it at a prepared source environment with `CALSCI_SOURCE_SITE_PACKAGES` or `CALSCI_SOURCE_PYENV`.

## Getting started

1. Install the extension.
2. Connect your CalSci device over USB.
3. Open the CalSci view container from the activity bar.
4. Run `CalSci: Select Device`.
5. Run `CalSci: Open Terminal` to open the persistent REPL.
6. Use `CalSci: Run Non-Interactive File` or `CalSci: Run Interactive File` on the active Python file.

## Main commands

- `CalSci: Select Device`
- `CalSci: Open Terminal`
- `CalSci: Soft Reset Device`
- `CalSci: Run Non-Interactive File`
- `CalSci: Run Interactive File`
- `CalSci: Open Hybrid Panel`
- `CalSci: Flash Firmware`
- `CalSci: Refresh Workspace`
- `CalSci: Refresh Testing Folder`
- `CalSci: Clear All Files`

## Settings

- `calsci.resetTimeoutSeconds`: timeout for waiting on the CalSci prompt after soft reset.
- `calsci.runTimeoutSeconds`: timeout for non-interactive file execution. Set `0` to disable the timeout.
- `calsci.autoConnectOnDetect`: automatically connect when the selected CalSci device is detected.
- `calsci.autoScanWorkspace`: automatically scan the device workspace when the CalSci Workspace view opens.

## Firmware

`CalSci: Flash Firmware` uses firmware files bundled inside the extension package, so users do not need to download the image set separately before flashing.

## Support

- Website: https://calsci.io
- Email: mailto:contact@calsci.io
