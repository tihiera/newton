# Newton — Plan

Backend first; the UI (U1) starts once a real GPU run works end to end.
Every milestone ends with `scripts/test.sh` green and STATUS.md updated.

**Scope (2026-10-01):** Brev is out of scope for now. Compute targets are:
1. an NVIDIA GB10 / DGX Spark, or any Linux NVIDIA box, over SSH (system OpenSSH,
   strict host keys, works with NVIDIA Sync aliases);
2. the local machine when it's capable: Apple Silicon GPU via MLX (Mac / Mac mini).

## Done

| ID | Milestone | Result |
|---|---|---|
| B0 | Backend foundation | agentd, SQLite migrations, token auth, events |
| B1 | Worker + transports | stdlib worker, local runner, SSH tunnel runner, bootstrap |
| B2 | Durable orchestration | state machines, approvals, scheduler, crash recovery |
| B3 | Benchmark end-to-end | 1D advection (4 schemes), evaluation, Markdown report |
| G1 | Bootstrap that can't silently fail | venv/installer chains, numpy gate, staged swaps, offline restarts, auto-upgrade with backoff, per-host scheduler |
| G2 | SSH hardening + one-click connect | Unix-socket worker, strict key capture (KnownHostsCommand), hostile-config neutralisation, `/ssh/hosts`, `/hosts/{id}/connect` |
| G3 | Backends + capability gating | cpu / cuda / metal, strict registry, worker preflight, capabilities, auto placement, GPU exclusivity, backend evidence check |
| M1 | Local Apple GPU (Metal via MLX) | metal backend, float32 validator, Apple GPU probe, auto placement only at ≥65,536 cells, power state recorded and throttled timings discounted; live test passes on the M3 Max |
| R1 | G2/G3 adversarial review | 35 findings fixed: private ControlMaster tunnel + 0700 Unix socket, refused-forward detection, per-install tokens, self-daemonizing worker, deleted hosts terminal, stale-host refresh, best-effort remote cancel, metal wired with a GPU-scale threshold; real-OpenSSH test harness (`NEWTON_TEST_SSHD=1`) |
| S0 | GB10 reachable | plain SSH over the tailnet (`ghost@100.85.54.5`), key already trusted; NVIDIA Sync not needed |
| G4 | CUDA on the GB10 | automatic GPU support (driver-matched CuPy, smoke test, PyPI toolkit fallback, self-maintaining); live: `cupy-cuda13x` 14.2.0 on system CUDA 13.0, verified in 1.3 s |
| T1 | Live acceptance | GB10 end-to-end green in 29 s from scratch; Mac Metal end-to-end green |
| P1 | GPU-scale workloads + kernel engine E1 | 2D advection, throughput mode, hand-written CUDA/Metal kernels (bit-identical to numpy on CUDA), roofline vs measured bandwidth, speed evidence; live: GB10 kernel 48x numpy at 29% of peak, M3 Max 148x at 37% |
| SV0 | Model serving measured on the Spark | gpt-oss:120b via Ollama: 31.5 tok/s alone, 45.7 tok/s at 32 (2 slots: the rest queue), cold start 172 s, idle model doesn't slow CUDA benchmarks; `scripts/sv0_serving.py` |
| SV1 | Model services next to jobs (worker) | typed ServiceSpec (pinned revisions), Newton-owned Ollama / vLLM / fake on 127.0.0.1, detached supervisors with identity-checked process groups, memory + disk admission, CUDA jobs capped beside services; live on GB10 (llama3.2:3b ready in 73 s, answered, stopped) |
| SV2 | Model services in agentd | services table + state machine, approval for downloads and model-supplied code, agentd-generated API keys in the Keychain (sent before start, so retries can't lose them), a private `ssh -L` per service on a stable local port, per-service reconcile with backoff, probes that rebuild only broken forwards; live: llama3.2:3b on the GB10 answered on this Mac in 0.42 s, 15 s after create |
| SV3 | Router (no roles, no accounts) | OpenAI-compatible `/v1` on agentd routing by model (one local profile, an inference-only router key), a slot limit per service with one fair queue, failover, whole-event streaming, provenance on every answer (no content stored), device leases so timed benchmarks never share a host with routed inference; live: GB10 + Mac served in parallel, a CUDA benchmark paused the Spark's inference while the Mac kept answering |
| SV4 | Mac models via MLX | off by default, small 4-bit mlx-community models at a pinned commit (size checked), gated on AC power and memory pressure, join the router; live on the M3 Max |
| E2 | Methods as data | SchemeIR + trusted numpy / CUDA / Metal generators + assumption checks; live: IR Lax–Wendroff bit-identical to the hand-written CUDA kernel in fp64 |
| B4 | Paper ingestion | arXiv → text → model card (through the router) → SchemeIR → proposed experiment; live on real physics papers |
| B5 | Research loop | goals poll arXiv, triage, card, propose (approval-gated), dedup by method, findings as scientific memory; live poll on today's arXiv |
| B6 | Publishing | GitHub gist / issue and Notion page, every one approved, exactly the approved text sent, tokens in the Keychain |
| U1 | Desktop UI | Tauri v2 + React 19 from the user's design: sidebar, paper inbox, research workspace, paper tabs, propose → review → approve → jobs/logs → evidence, publish via approval, compute (host-key trust), models, settings, approvals; no business logic in React; live against agentd on this Mac |

## Next

| # | ID | Step | What gets built | Done when |
|---|---|---|---|---|
| 9 | U2 | **Library, evidence board, research diary** | built with U1 from the design (all papers, evidence from the ValidationReport, research timeline); left: see STATUS "Known gaps (UI)" | the gaps are closed or accepted |

## Then: using the Mac and the GB10 together (before paper ingestion)

Principle (research 2026-10-01): run **different work on each machine at the same
time**; never split one model or one simulation across them. A MacBook M3 Max
(300 GB/s) and a GB10 (273 GB/s) have the same memory bandwidth, there is no RDMA
link between them, and measured cross-machine splits made decoding 40–60 % slower.
EXO's 2.8× Spark+Mac result needed an M3 Ultra and isn't in open-source EXO.

All of these are done (SV0–SV4, E2): see Done.

## Later

| ID | Milestone |
|---|---|
| B7 | Demo hardening, offline/degraded modes, signed build |
| — | Remote Mac mini as an SSH worker (bootstrap Darwin branch, Python ≥3.10 selection) |
| E4 | Optional, needs a SPEC decision: agent-drafted CUDA/Metal kernels shown as a diff for approval, pinned by sha256, compiled and run only inside a sandbox on the Spark |
