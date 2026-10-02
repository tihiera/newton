// What the desktop shell does for the UI beyond agentd: open a link in the user's
// browser, save a downloaded file where the user says, and restart the engine (the
// agentd the packaged app manages). In the Tauri shell links go
// through the opener plugin (capabilities/default.json lists the sites it may open) and
// files through the shell's `save_file` command (a native save dialog; the webview has
// no filesystem access). In a plain browser (`pnpm dev:web`) the browser does both.

import { invoke as tauriInvoke, isTauri as tauriIsTauri } from "@tauri-apps/api/core";
import { openUrl as tauriOpenUrl } from "@tauri-apps/plugin-opener";

/** The header `save_file` reads the suggested name from (src-tauri/src/files.rs; its
 *  `name_header_matches_the_ui` test fails if either side renames it). */
export const FILE_NAME_HEADER = "x-newton-file-name";

export interface PlatformDeps {
  isTauri: () => boolean;
  openUrl: (url: string) => Promise<void>;
  invoke: <T>(cmd: string, args: Uint8Array, options: { headers: Record<string, string> }) => Promise<T>;
  /** A shell command without arguments (`restart_engine`). */
  command: (cmd: string) => Promise<unknown>;
  /** Browser fallbacks. */
  windowOpen: (url: string) => void;
  download: (name: string, blob: Blob) => void;
  writeText: (text: string) => Promise<void>;
}

/** Only http(s) links leave the app. */
export function isWebUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const { protocol } = new URL(url);
    return protocol === "https:" || protocol === "http:";
  } catch {
    return false;
  }
}

function asError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

function browserDownload(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // The download has started by the time this runs; the URL is only needed until then.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function createPlatform(deps: Partial<PlatformDeps> = {}) {
  const d: PlatformDeps = {
    isTauri: deps.isTauri ?? tauriIsTauri,
    openUrl: deps.openUrl ?? ((url) => tauriOpenUrl(url)),
    invoke: deps.invoke ?? ((cmd, args, options) => tauriInvoke(cmd, args, options)),
    command: deps.command ?? ((cmd) => tauriInvoke(cmd)),
    windowOpen: deps.windowOpen ?? ((url) => void window.open(url, "_blank", "noopener,noreferrer")),
    download: deps.download ?? browserDownload,
    writeText: deps.writeText ?? ((text) => navigator.clipboard.writeText(text)),
  };

  /** Opens an http(s) link in the user's browser. */
  async function openExternal(url: string): Promise<void> {
    if (!isWebUrl(url)) throw new Error(`only web links open outside Newton, not ${url}`);
    if (!d.isTauri()) {
      d.windowOpen(url);
      return;
    }
    try {
      await d.openUrl(url);
    } catch (err) {
      throw asError(err);
    }
  }

  /** Asks where to save `blob` (suggesting `defaultName`) and writes it there. The
   *  saved path, null when the user cancelled; in a browser, the name downloaded. */
  async function saveFile(defaultName: string, blob: Blob): Promise<string | null> {
    if (!d.isTauri()) {
      d.download(defaultName, blob);
      return defaultName;
    }
    const bytes = new Uint8Array(await blob.arrayBuffer());
    try {
      return await d.invoke<string | null>("save_file", bytes, {
        headers: { [FILE_NAME_HEADER]: encodeURIComponent(defaultName) },
      });
    } catch (err) {
      throw asError(err);
    }
  }

  /** Copies `text` to the clipboard; false when the webview refused (no clipboard, or
   *  the click that asked for it is too long ago). */
  async function copyText(text: string): Promise<boolean> {
    try {
      await d.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  /** Whether the Tauri shell is around (a plain browser has no engine to restart). */
  function inShell(): boolean {
    try {
      return d.isTauri();
    } catch {
      return false;
    }
  }

  /** Asks the shell to restart the agentd it manages (packaged app). The shell's
   *  refusal comes back as an Error with its sentence. */
  async function restartEngine(): Promise<void> {
    if (!inShell()) throw new Error("only the Newton app can restart its engine");
    try {
      await d.command("restart_engine");
    } catch (err) {
      if (err && typeof err === "object" && "message" in err) throw new Error(String(err.message), { cause: err });
      throw asError(err);
    }
  }

  return { openExternal, saveFile, copyText, inShell, restartEngine };
}

const platform = createPlatform();

export const openExternal = platform.openExternal;
export const saveFile = platform.saveFile;
export const copyText = platform.copyText;
export const inShell = platform.inShell;
export const restartEngine = platform.restartEngine;
