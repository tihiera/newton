// The one-click connect, with the host-key step agentd asks for:
//   connect -> 409 {fingerprints} -> the user compares them -> trust -> connect again.
// Nothing is ever trusted without the user pressing "Trust & connect".

import { useCallback, useState } from "react";
import { AgentdError, api, type ConnectResult, type HostKeyFingerprint } from "../../api";

export type ConnectPhase =
  | { phase: "idle" }
  | { phase: "connecting" }
  | { phase: "hostkey"; fingerprints: HostKeyFingerprint[]; message: string }
  | { phase: "trusting"; fingerprints: HostKeyFingerprint[]; message: string }
  | { phase: "connected"; result: ConnectResult }
  | { phase: "error"; error: Error };

export function useHostConnect(hostId: string, onChange?: () => void) {
  const [state, setState] = useState<ConnectPhase>({ phase: "idle" });

  const connect = useCallback(async () => {
    setState({ phase: "connecting" });
    try {
      const result = await api.hosts.connect(hostId);
      setState({ phase: "connected", result });
    } catch (err) {
      if (err instanceof AgentdError && err.status === 409 && err.fingerprints?.length) {
        setState({ phase: "hostkey", fingerprints: err.fingerprints, message: err.message });
      } else {
        setState({ phase: "error", error: err instanceof Error ? err : new Error(String(err)) });
      }
    } finally {
      onChange?.();
    }
  }, [hostId, onChange]);

  /** Only from the user's "Trust & connect": trusts exactly the keys shown, then connects. */
  const trust = useCallback(async () => {
    if (state.phase !== "hostkey") return;
    const { fingerprints, message } = state;
    setState({ phase: "trusting", fingerprints, message });
    try {
      await api.hosts.trust(hostId, fingerprints.map((f) => f.fingerprint));
    } catch (err) {
      setState({ phase: "error", error: err instanceof Error ? err : new Error(String(err)) });
      onChange?.();
      return;
    }
    await connect();
  }, [state, hostId, connect, onChange]);

  const reset = useCallback(() => setState({ phase: "idle" }), []);

  return { state, connect, trust, reset, busy: state.phase === "connecting" || state.phase === "trusting" };
}
