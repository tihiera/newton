# Status

_Last updated: 2026-10-02_

## Current milestone

**SV4, E2, B4, B5 and B6 done** (2026-10-02). Mac models via MLX, methods as data
(SchemeIR + generators + assumption checks), paper ingestion from arXiv, the research
loop with scientific memory, and approval-gated publishing to GitHub / Notion. All
live-verified except publishing (built and tested against mocked APIs: it needs the
user's tokens and consent to post). SV4 and E2 each had an adversarial review round.
Next: **U2** (UI for library, evidence, diary) once the design screenshots arrive, and
**B7** (demo hardening).

## Decisions

- **2026-10-02: research candidates are SchemeIR documents, not code.** B5's "candidate
  worktrees" are not needed: a paper's method becomes data that trusted generators
  turn into code. Git worktrees come back only with agent-drafted kernels (E4).
- **2026-10-02: the model reads, Newton maps.** A model fills a card by choosing from
  fixed vocabularies (limiter, time integration, claimed order / CFL / TVD); Newton's
  own mapping makes the SchemeIR. A method outside the vocabulary stays a note.
- **2026-10-02: no user accounts, no roles.** One local profile (display name, the model
  `default` stands for, the router key); the router routes by model name, never by
  role. PLAN's `extract-vlm` / `reason` / `embed` / `rerank` / `code` roles are dropped.
- **2026-10-01: project name is Newton** (it was FlowProof). Import packages are
  `newton_agentd` / `newton_worker`, never a bare `newton`, which is the NVIDIA /
  Disney / DeepMind physics engine on PyPI. Env vars `NEWTON_*`, remote dir
  `~/.newton`, data dir `~/Library/Application Support/Newton`.
- **2026-10-01: Brev out of scope.** Targets are SSH to a GB10/DGX Spark or any Linux
  NVIDIA box (NVIDIA Sync aliases work as-is), plus the local Apple Silicon GPU via MLX.
- **Host keys stay strict.** New keys are captured and shown to the user, and are
  trusted only on approval. No accept-new, no auto-rotation: the auto-mode safety
  check rejected those on 2026-09-30, and that still stands.
- **2026-10-01: Mac + GB10 together = different work on each, in parallel.** Never
  split one model or simulation across them (research: same memory bandwidth, no
  RDMA link, and splits measured 40–60 % slower). This is captured in PLAN.md
  "Then: using the Mac and the GB10 together".
- **2026-10-01: no Docker, jobs see the user's machine.** Jobs run directly as the
  SSH user (or the Mac user), from `~/.newton`, and may read anything that user
  can, e.g. datasets anywhere in the home directory. Container isolation was
  considered (a design was checked live on the GB10, where Docker 28.5 + NVIDIA
  Container Toolkit 1.17.8 work) and dropped by the user. The safety rule stays:
  only approved, Newton-defined programs run, built as argument lists from a typed
  spec, never shell text from a model or a request.
- **Agent-written GPU code:** v1 runs only Newton-authored kernels (E1) or methods
  expressed as data and compiled by trusted generators (E2). Agent-drafted CUDA
  (E4) needs a separate SPEC decision plus a sandbox.

## Completed

- **B6, publishing reports** (`connectors/publish.py`, migration `0008`):
  - GitHub (a secret gist, or an issue in a repo) and Notion (a page under a parent page).
  - Every publication is an approval showing where, what (sha256, size) and a preview;
    the approved text is frozen on disk and checked before sending (a changed file is
    not sent); a rejection sends nothing.
  - Tokens in the Keychain (`PUT /connectors/{github,notion}`), GitHub's importable from
    the GitHub CLI on request (`gh auth token`, fixed argv); never in SQLite, never in
    error messages. Notion: headings, lists, tables (monospaced), 100 blocks per call.
  - `POST /experiments/{id}/publish`, `GET /publications`, `GET /connectors`.
- **B5, the research loop** (`research/loop.py`, migration `0007`):
  - Goals carry arXiv keywords (validated words: no query syntax) and categories, how
    often to look (`poll_hours`) and `auto_propose`. Polls keep arXiv's 1 request / 3 s.
  - New papers only (dedup by arXiv id), at most 8 per poll: triage on title + abstract
    (relevant to the goal? one sentence why), then B4's card for relevant ones;
    irrelevant ones are dismissed with the reason.
  - Proposals: a carded scheme becomes an experiment awaiting approval, unless the same
    method (flux + time stepping: `method_digest`, whatever its name or claims) was
    already tested or planned.
  - Scientific memory: `findings`: each finished experiment from a paper, its evidence
    and claim-by-claim result; the paper's item moves to reported.
  - `POST /goals/{id}/poll`, `GET /findings`. Live: see Verification.
- **B4, paper ingestion** (`research/papers.py`):
  - `POST /research/ingest {ref: arXiv id or URL}`: metadata from arXiv's API, full text
    from arXiv's HTML (formulas kept as LaTeX) or the PDF (`pypdf`), stored on disk.
  - A model (the profile's default, through the router: slots, leases, provenance) fills
    a JSON card; Newton validates it field by field and maps the method onto a SchemeIR
    (or keeps a note why not). Nothing from a paper or a model is executed.
  - `POST /research/items/{id}/propose`: the card's scheme against a baseline, as an
    experiment awaiting approval. `GET /research/items`.
- **E2, methods as data** (`benchmarks/advection/ir.py`, `research/schemes.py`):
  - SchemeIR: flux form F = u_i + A(c) φ(r) (u_{i+1} - u_i); A(c) a small typed expression
    tree (evaluated as written: bitwise reproducible), φ from a fixed limiter list
    (none, minmod, van Leer, superbee, MC, Koren), one-step or explicit RK (tableau);
    claims: order, max CFL, TVD. Validation refuses anything else (and singular A(c)).
  - Trusted generators: numpy array code, and a C++ header for the existing CUDA and
    Metal sweep kernels (fixed snippets chosen by enum: no document text becomes code).
    The generated header's sha256 is pinned at approval and checked in the result.
  - Assumption checks in every run: order (finest grid pair; not on discontinuous data),
    conservation, TVD, max CFL (exact von Neumann for linear schemes, a transit-scaled
    probe for limited ones), determinism (bitwise), agreement with numpy. A broken
    claim caps the verdict at yellow; nondeterminism or another document is red.
  - Library: the 4 hand-written schemes + superbee, MC, Koren and MUSCL / SSP-RK2.
    `GET /schemes`, `POST /schemes/check`; reports get an "Assumption checks" table.
- **SV4, Mac models via MLX** (worker engine `mlx`, migration `0006`):
  - Off by default (`PATCH /profile {mac_models: true}`); off again stops them.
  - Small 4-bit `mlx-community/*-4bit` models at a pinned commit; the size is checked
    from the hub's listing before downloading and on disk after (`memory_gb`, at most
    16 GB); only the model's files are downloaded; a completion marker, so a partial
    download is never "present".
  - `mlx_lm server` on 127.0.0.1 on that exact snapshot, no browser origins, bounded
    prompt cache, KV bits from `kv_cache_type`, MPI disabled (MLX otherwise aborts on a
    conda MPICH). The router sends it standard OpenAI fields only, `model` is always
    its own snapshot (mlx-lm would load any path a request names).
  - Gate: AC power and no critical memory pressure, at admission (503 "not now",
    retried) and every few seconds while downloading, loading and serving (stops with
    the reason). Services get an allowlisted environment (never the worker's token).

- **SV3, the router** (`services/agentd/newton_agentd/serving/router.py`, `profile.py`,
  migration `0005_router.sql`; `/v1/*`, `/profile`, `/router/*`):
  - **One local profile, no accounts:** display name, the model `default` stands for,
    and the router key (secret store; inference only: it opens `/v1/*` and nothing
    else, so scripts and editors never hold the admin token). `GET/PATCH /profile`,
    `GET /router/credentials`, `POST /router/credentials/rotate`.
  - **OpenAI-compatible `/v1` on agentd's port:** `GET /v1/models` and `POST
    /v1/chat/completions`, `/v1/completions`, `/v1/embeddings`. Errors have OpenAI's
    shape (`{error: {message, type, param, code}}`) so the official SDK raises the
    right exception; tested with `openai` 3.23.
  - **Routing by model, no roles:** a model name (Ollama's also without `:latest`),
    pinned to a revision with `model@<revision>`, or a service id, or `default`. One
    model at two revisions is ambiguous unless pinned. Never a substitute model.
  - **Slots:** each service takes at most `parallel` requests; the rest wait in one
    queue in arrival order (least loaded free service first), 300 s at most (then
    503, `x-should-retry: false`), at most 256 per model (then 429).
  - **Clients that leave:** while waiting, mid-answer or mid-stream, the slot is freed
    at once and the engine's connection closed (the guard is plain ASGI so the
    router sees `http.disconnect`; uvicorn doesn't cancel handlers).
  - **Failover:** a request that got no response at all (a dead engine behind
    `ssh -L` accepts, then closes) is retried once on another service for the same
    model, and that service is re-probed now.
  - **Streams** are relayed whole event by whole event; a stream that breaks or is
    aborted ends with one well-formed error event and no `[DONE]`.
  - **Provenance:** `X-Newton-Request-Id`, `-Service`, `-Host`, `-Engine`, `-Model`,
    `-Revision` (ids only: headers stay ASCII) on every answer; `router_requests`
    logs timings (queued, first byte, total), status, attempts and token counts. No
    prompt or completion text, and no engine error text (it can echo the input), is
    ever stored; 30 days / 200k rows kept. `GET /router/requests`, `/router/status`.
  - **Device leases:** a timed job (GPU, or exclusive) takes its host. No new routed
    request goes there (models served elsewhere still answer; the rest get a 503
    "paused" at once rather than waiting out an hour-long benchmark); requests in
    flight may finish, stragglers are aborted after 120 s; no model starts loading
    there meanwhile (approved services wait). Nothing else starts on the host while
    it clears. The lease is given back during retry backoffs, upgrade waits and
    collecting, and taken again at startup before `/v1` serves (a job still running
    keeps its host). Each job records a `device_lease` event; aborts or a model that
    was still loading become a timing caveat (the evaluation discounts the timing);
    the report says what was paused and that direct endpoint use isn't verified.
  - **Design reviewed before coding** (3 critics + synthesis): leases released from
    every scheduler tick (not host passes), whole-event SSE relay, disconnect
    handling, failover on "no response", fail-fast when paused, starts held during
    leases, ASCII-only headers, no engine error text stored.
  - **Review round fixes** (42 confirmed):
    - no way to leak a slot (NaN / Infinity / lone surrogates in a body were one);
    - a client that stops reading can't hold a timed job: a stopped stream is cut
      after a 2 s grace;
    - the only service failing to answer is a 502 at once, not a 300 s wait;
    - shutdown ends queued and running requests with their own "shutdown" error
      (graceful timeout 10 s);
    - the guard never reads the Keychain (cached key, 503 when locked) and compares
      bytes;
    - `stream` must be a boolean; only `text/event-stream` answers are relayed;
    - CRLF split across chunks; every /v1 path and method answers OpenAI-style;
    - a timed job doesn't take the host while its worker upgrade or a GPU install
      blocks it;
    - a transient submit error retries the same remote id and keeps the host (it
      may be running there);
    - a model start already on its way counts as loading, and a late start becomes a
      caveat;
    - caveats found after the grant and leases re-taken after a restart are
      recorded;
    - the waiting note says what it waits for.

- **SV2, model services in agentd** (`services/agentd/newton_agentd/serving/manager.py`,
  migrations `0003_services.sql` + `0004_service_forwards.sql`, `/services` routes;
  worker protocol 4):
  - **API:**
    - `POST /services` (host + typed settings), `GET /services[?host_id]` and
      `/services/{id}`;
    - `POST /services/{id}/stop` and `/drain?seconds=`, `GET /services/{id}/logs?offset=`;
    - `GET /services/{id}/credentials` (base_url + key + model, agentd token only).
  - **Admission before anything is asked:** the worker checks memory (reserve, services
    still loading), disk for a download, whether the engine is installed and vLLM's
    sizing; any "no" is a 409 and nothing is created.
  - **Approval** when starting means a download (with its size) or `trust_remote_code`;
    a rejected or cancelled service records `finished_at`; cancelling one also
    rejects its pending approval.
  - **API keys:** agentd generates each vLLM/fake key, stores it in the Keychain
    (`service-key-<id>`, the database holds only that reference), and only then asks
    the worker to start, sending the key in a header (never in spec.json, status or
    logs). A retried start can't lose the key. The key is deleted when the service
    ends. Ollama has no keys, and the API says so (any local user of this Mac can
    reach that endpoint while it is up; never the network).
  - **Reachable on this Mac:**
    - an SSH host gets a private ControlMaster with one `-L 127.0.0.1:<local>:127.0.0.1:<remote>`;
    - the local port is kept across rebuilds and restarts, so clients keep their base_url;
    - the local worker's own loopback port is used directly;
    - the master's pid and control path are stored, so a new agentd ends masters a
      crashed one left behind.
  - **Reconcile loop:** each service is advanced in its own task (a slow or offline
    host delays only its own services), with exponential backoff up to 60 s. It
    mirrors the worker's state, marks a service the worker has no record of `lost`,
    and a host deleted under a service `lost` / `failed`.
  - **Probes:**
    - every 10 s, never through a proxy;
    - an HTTP error or a slow answer is reported, nothing is torn down;
    - two connection failures in a row (or a dead master) rebuild the forward;
    - "administratively prohibited" from sshd is reported as forwarding denied, and
      the forward is not rebuilt in a loop.
  - **Stop during start:** a service cancelled while its start was on the way is
    stopped on the worker as soon as the start returns, so no model server runs untracked.
  - **Host deletion:**
    - refused while services are active;
    - `force` stops started services on the worker and marks them lost;
    - `force` cancels waiting services and rejects their approvals.
  - **Found by the tests:**
    - the worker's health checks of its own engine went through `HTTP_PROXY` from
      the environment (urllib proxies even loopback), so a host with a proxy set
      never got a service ready;
    - the worker and the SV0 script now open loopback URLs without proxies.

- **SV1, model services next to jobs** (`services/worker/newton_worker/services.py`,
  `run_service.py`, `procs.py`, `fake_openai.py`; worker protocol 3):
  - **Typed `ServiceSpec`** (worker and agentd apply the same rules): engine `ollama`
    | `vllm` | `fake`; model names fully matched per engine (Ollama names get their
    `:latest`); a **pinned revision** (Ollama manifest digest, or a 40-hex Hugging Face
    commit); context length, parallel slots, KV cache type, declared memory; no
    free-form flags anywhere. `trust_remote_code` exists for vLLM only and is off by
    default (its approval is SV2's).
  - **Engines get fixed argv on 127.0.0.1:** a Newton-owned `ollama serve` (its own port
    and model store under `~/.newton/models`, model resident until stopped, the host's
    system Ollama untouched), or `vllm serve --revision`, its memory share derived from
    the declared memory. `fake` is an OpenAI-compatible stand-in for tests.
  - **A detached supervisor per service** (survives the worker and SSH): starting →
    loading (Ollama: pull, check the pinned digest, delete it if the tag moved, load
    it resident) → ready (health every 5 s) → draining → stopping → stopped / failed /
    lost. A stop interrupts a long download.
  - **Process identity:** each engine's boot id and start time are recorded; a process
    group is signalled only while provably ours (no pid reuse after a reboot can make
    Newton kill a stranger), and always as a whole group, so an engine's children
    (Ollama's runner, vLLM's workers) never outlive it holding memory.
  - **Memory admission** (a GB10 that runs out of unified memory freezes): available RAM,
    minus what services still loading or stopping before ready have declared, minus a
    reserve of max(8 GB, 10%); unknown memory refuses. Downloads need free disk (model
    + 10 GB). CUDA jobs cap their CuPy pool beside running services, or refuse.
  - **API:** `POST /services`, `POST /services/admission`, `GET /services[/{id}]`,
    `POST /services/{id}/stop`, `POST /services/{id}/drain?seconds=`,
    `GET /services/{id}/logs`.
  - **Live on the GB10:** through agentd's SSH runner, a Newton-owned Ollama pulled
    `llama3.2:3b` (1.9 GB), verified the pinned digest, was ready in 73 s (16 s once
    the model is in Newton's store), answered on its OpenAI endpoint, stopped cleanly.
  - **Review:** 8 agents (4 lenses, verified by reproduction): 38 confirmed (about 15
    distinct issues), all fixed and covered by tests; 10 rejected.
  - **Tests now run in parallel** (`pytest-xdist`, `scripts/test.sh`): 9 min → 1m40s.

- **SV0, model serving measured on the Spark** (`scripts/sv0_serving.py`, stdlib only, run
  on the host against its Ollama 0.30.7 system service: 127.0.0.1:11434, flash attention,
  q8_0 KV cache, `NUM_PARALLEL=2`, `KEEP_ALIVE=24h`; 8192-token context per request,
  256-token answers, temperature 0). gpt-oss:120b (MXFP4, 64.4 GB resident):

  | Measure | Result |
  |---|---|
  | Cold start (load from NVMe to first token) | **171.8 s** (load 170.6 s) |
  | Warm start (resident, short prompt) | 2.6 s |
  | 1 request | **31.5 tok/s** (decode 34.3 tok/s), first token 0.7 s |
  | 8 at once | 39.2 tok/s total, 24.3 tok/s each; first token 22 s median, 38 s max |
  | 32 at once | 45.7 tok/s total, 24.6 tok/s each; first token 84 s median, 169 s max |
  | Newton CUDA kernel (2D 2048², float64) with the model resident but idle | 2.20 ms/step vs 2.20 unloaded: **no slowdown** |

  llama3.2:3b for comparison: cold start 4.8 s, 70 tok/s alone, 153 tok/s with 8.
  - **What it means for SV1-SV3:**
    - Concurrency beyond 2 only queues (the server's `NUM_PARALLEL=2`): total
      throughput grows 31 → 46 tok/s while time to first token grows to minutes.
      Interactive roles need their own slots, or a server Newton configures.
    - A 64 GB model takes ~3 minutes to load: keep it resident while in use, and
      admit memory before loading (SV1), never load per request.
    - An idle resident model does not disturb GPU benchmarks: the device lease (SV3)
      only needs to drain *active* serving, not evict models.
  - **Caveats:** the owner's CFD job (`brae_interFoam`) shared the GPU during part of the
    run; the server config needs sudo to change and the model files belong to the
    `ollama` user, so a second, Newton-configured server would need its own copy
    (65 GB on a 93%-full disk). The script unloads the model at the end (otherwise the
    service keeps it for 24 h).

- **U1 shell (design pending)** (`apps/desktop`: Tauri v2, Vite, React, TypeScript, pnpm):
  - The Rust command `agentd_connection` resolves agentd's data dir, port and token
    with `config.py`'s rules (`NEWTON_DATA_DIR`, `NEWTON_PORT`, `NEWTON_API_TOKEN`,
    `<data_dir>/api-token`) and hands `{base_url, token}` to the UI; the token is never
    logged, and a missing token says "agentd not started yet".
  - CSP: connections only to Tauri IPC and `http://127.0.0.1:*`. agentd's CORS already
    allowed `tauri://localhost` and the dev origin, so the backend is unchanged.
  - Typed API client (`src/api/`) with explicit error kinds; one placeholder screen
    (connection state, hosts with their CUDA/Metal capability), polling every 3 s.
  - Verified: typecheck, 8 vitest + 7 Rust tests, debug `.app` bundle (28.8 MiB), and the
    real window against a live agentd (authenticated `/health` and `/hosts`, 200).
  - Placeholder look and icons, to be replaced from the user's design screenshots.
  - `scripts/test.sh` runs the desktop checks where `apps/desktop` is installed.
  - Note: with `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID` in the environment,
    `tauri build` signs and submits to Apple's notary; one debug build was submitted
    by accident and refused (403, a developer agreement is missing or expired). Local
    builds unset them (see `apps/desktop/README.md`).

- **P1, GPU-scale workloads + kernel engine E1** (`benchmarks/advection/`):
  - **2D advection** (`linear_advection_2d`): dimension-split sweeps in alternating
    order (xy, yx), exact solution from separable cell averages. Every grid of a
    refinement uses the same Courant number (a drifting one fakes order changes).
  - **Throughput mode:** a fixed number of steps on one large grid, timed without
    diagnostics, after a time-based warm-up (an idle GPU starts slow).
  - **Engine E1:** hand-written sweep kernels for all four schemes, one shared header
    (`kernels/advect.h`) mirroring numpy's operation order, compiled as CUDA (CuPy
    `RawModule`, `--fmad=false`) and Metal (`mx.fast.metal_kernel`). sha256 of the
    kernel sources is pinned in each job's manifest at approval and must match what the
    job reports, or the result is red.
  - **Same numbers as numpy, checked in every GPU job:** the same steps in numpy at the
    same precision on the same machine. Measured: CUDA float64 and float32 are
    **bit-identical** to numpy; Metal float32 within 1.8e-7. A disagreement is red.
  - **Roofline:** each job measures its device's copy/triad bandwidth (chained, so cache
    sharing can't inflate it), then reports achieved GB/s at minimum sweep traffic and
    the % of that measured peak. Grids that fit in cache get no % (it would be the
    cache's bandwidth). Speedup vs numpy is best-of-N against best-of-N.
  - **Speed evidence** (`objective: performance`): only the implementation may differ
    between variants; green at ≥1.2x with the same numbers, red if slower or different.
    Speed-verdict jobs run alone on their host. A shared GPU (sampled before and after,
    any backend on an NVIDIA host) or Low Power Mode makes the speed check inconclusive,
    never hiding a real failure.
  - **Accuracy evidence:** variants may differ only in scheme/implementation. float32
    grids past the round-off cut-off (calibrated on 10 measured float32/float64
    refinements: within 0.04 of the float64 order, never dropping a float64 grid) are
    left out of the fit and of the L2 comparison; no usable grid means inconclusive.
  - **Approval:** runtime estimate (from comparable past runs on this host, else rough
    defaults) and peak memory; a job estimated >3x its timeout, or needing >60% of the
    host's RAM, is refused up front. Kernels are never placed on a CPU.

- **G4, CUDA on the GB10, with no prompts** (`newton_worker/gpu.py`, `bootstrap.sh --gpu-only`):
  - **Separate from the worker bootstrap:** agentd runs GPU support over its own SSH
    call once the worker serves (timeout 1 h), so a slow download (CuPy ~70 MB, the
    PyPI CUDA toolkit fallback ~1.3 GB) never holds up or fails a connection. No new
    job starts on the host while it runs.
  - **What it installs:** driver ≥ 580 → `cupy-cuda13x>=14.0.1,<15`; 525–579 →
    `cupy-cuda12x`; older → explained. CuPy goes into `~/.newton/venv` through the
    same installer chain as numpy, with the numpy pin in the same resolve; numpy is
    re-checked afterwards. A CuPy built for the wrong CUDA major is replaced.
  - **The smoke test decides:** a child process (so a SIGBUS or a hang can't take
    anything down; bounded even if stuck in the driver) compiles an NVRTC kernel for
    the GPU, checks exact results, runs every op the schemes use in float64 and
    float32. If CUDA libraries are missing, it installs NVIDIA's PyPI toolkit once
    (`cupy[ctk]` + `cuda-toolkit==13.0.*`), never for driver-library errors.
  - **`gpu.json` is the truth:** cuda counts as usable only when the smoke test
    passed for exactly the installed CuPy and the current driver. Errors name the
    cause (CuPy's import banner and cuda-pathfinder's listings are parsed).
  - **agentd keeps it current by itself (`gpu_support: auto`, per host, or `off`):**
    after connect, after a worker restart or upgrade, when the driver or CuPy
    changed, when a run waited for jobs, and a daily retry after a failure.
    Preconditions (Python ≥ 3.10 for CUDA 13, installs enabled) are re-checked on
    every run. A transient nvidia-smi failure never overwrites a verified result.
  - **API:** `POST /hosts/{id}/gpu-support` ("Install GPU support": 202, or
    `?wait=true`; 409 while jobs run), `PATCH /hosts/{id}` (`gpu_support`,
    `max_parallel_jobs`), host `gpu_task`, events `gpu_support*`.
  - **Jobs on unified memory (GB10):** `CUPY_GPU_MEMORY_LIMIT=50%` unless the owner
    set one (memory pressure there can hang the whole machine); a private kernel
    cache `~/.newton/cache/cupy`.

- **B0–B3**: foundation, worker + transports, durable orchestration, advection
  benchmark with evaluation and reports (details in git history and PLAN.md "Done").
- **G1, a bootstrap that can't silently fail** (`services/worker/bootstrap.sh`):
  - **Packages and Python environment:**
    - packages go only into `~/.newton/venv`, never into a system Python;
    - venv chain `venv` → `venv --without-pip` → `uv venv`; installer chain venv pip →
      system `pip --python` → `pip --target --upgrade` → `uv pip`;
    - pins `numpy>=2,<2.6` (gated by a range check) and `matplotlib>=3.9` (best effort);
    - the install is skipped when the pins are already met, so restarts work offline;
    - the login environment (`PYTHONPATH`, cwd) can't fake numpy;
    - a venv built from another Python is rebuilt.
  - **Failure safety:**
    - everything that can fail runs before the live worker is touched;
    - the new source arrives in a staging dir, and a new venv is built in `venv.new`;
      both are swapped in only on success;
    - package changes wait while jobs are running (exit 75);
    - exit codes 64–69/75 map to `config`, `token_missing`, `python_missing`,
      `worker_start_failed`, `deps_missing`, `venv_unavailable` and `bootstrap_busy`.
  - **Locking and process safety:**
    - the lock records its owner's pid;
    - a stale pid in `worker.json` is never signalled (the command line is checked);
    - `~/.newton` is 0700.
  - **agentd side:**
    - the worker source is a snapshot with a content digest, and the worker reports
      the digest it actually loaded;
    - a stale worker is upgraded automatically;
    - a failed upgrade keeps serving through the old worker, records
      `worker_upgrade_failed` and retries with exponential backoff (10 min → 6 h);
    - submits are only refused across protocol versions.
  - **Scheduler:**
    - one task per host, so a slow host never delays the others;
    - host errors never finalize a job or drop its artifacts;
    - a cancel is final only once the worker confirms it.
- **G1 follow-ups (second adversarial round: reproduce + mutation tests):**
  - **Scheduler:** fixed a busy-loop (host passes are now rate-limited to one per
    interval).
  - **Upgrades:**
    - a failed upgrade is retried when its backoff expires, even with the tunnel up;
    - an upgrade only counts once the new worker reports our digest;
    - a failed restart backs off (an explicit check retries now);
    - an upgrade wait is not counted as a failed submit;
    - if the new worker can't start, the previous one is restored (exit 76, transient).
  - **Packages:**
    - matplotlib is installed together with the numpy pin;
    - stale `--target` metadata is removed only after a successful install;
    - only a numpy change waits for running jobs (an optional matplotlib never blocks).
  - **Locking and process identity:**
    - Linux uses `flock`; the macOS mkdir lock takes over atomically;
    - job wrappers are identified by their command line (a reused pid doesn't count);
    - a relative `--python` works.
  - **Hosts and logs:**
    - a host is archived on delete (its history survives), with `?force=true`
      cancel/delete for hosts that are gone;
    - log offsets are saved after every chunk.
- **G2, SSH hardening and one-click connect:**
  - **Transport:**
    - the worker serves on `~/.newton/worker.sock`, reached by OpenSSH stream-local
      forwarding, so other users on a shared host can't connect or squat the port;
    - the user's known_hosts settings are untouched, and Newton's trusted keys are
      added as a global known_hosts file (quoted, so paths with spaces work);
    - `RemoteCommand`, TTY allocation, connection multiplexing, agent forwarding and
      `LocalForward` are neutralised;
    - remote scripts run under `sh -c`, so fish/csh login shells work;
    - an sshd that refuses forwarding gets a specific error, `forwarding_denied`.
  - **Host keys:** captured via `KnownHostsCommand`, so this works through
    ProxyCommand/Tailscale aliases, with ssh-keyscan as a fallback.
  - **API:**
    - `GET /ssh/hosts` parses `~/.ssh/config` with Includes and tags NVIDIA Sync hosts;
    - `POST /hosts/{id}/connect` returns 409 with fingerprints, or does install →
      check → selftest;
    - `user@host` and IPv6 targets are accepted;
    - Brev code removed.
- **M1 completion:** each run records the Mac's power source and power mode. When
  Low Power Mode or battery may have throttled a run, the cost check becomes
  inconclusive (green → yellow) instead of passing or failing. Provenance lists
  numpy, CuPy and MLX versions.
- **G2/G3 review fixes:**
  - **Transport:**
    - The tunnel now runs through a private ControlMaster opened with
      `ClearAllForwardings`, so the user's `LocalForward` lines are never inherited
      (they used to collide with their own sessions).
    - The Mac end is a Unix socket in a fresh 0700 directory, not a TCP port: the
      token can't leak to a local port squatter.
    - A refused forward is detected by asking the host whether the worker runs,
      because OpenSSH reports it exactly like a dead worker. Before, this caused a
      re-bootstrap loop.
  - **Host keys and remote output:**
    - Key capture ignores ssh's `ORDER`/`NONE` call, and key types and blobs are
      validated.
    - Global known_hosts paths containing spaces are rejoined.
    - The remote home is printed between markers, so rc-file output can't corrupt it.
  - **Worker:**
    - It accepts one token per Newton install (`~/.newton/tokens/`), so two Macs or
      two host entries for one account no longer lock each other out.
    - It daemonizes itself: macOS `nohup` fails without a console (found by the
      real-sshd test).
    - Protocol 2.
  - **Hosts:**
    - A deleted host stays deleted everywhere (404).
    - Force cancel and force delete ask the worker first.
    - The scheduler reports reachability, and auto placement re-checks stale
      "online" hosts.
  - **Scheduling and placement:**
    - Nothing runs beside a GPU job.
    - The cached-capability refusal at submit is gone (the worker's 422 decides).
    - The CPU fallback stays on this Mac.
    - Metal is wired in, but "auto" only picks it for runs of at least 65,536 cells,
      where an Apple GPU beats numpy.
- **G3, backends and capability gating:**
  - **Backends:**
    - `cpu | cuda | metal` (`gpu` is accepted as an alias for cuda), through a
      strict benchmark registry: an unavailable backend fails with a reason and
      never falls back to numpy;
    - the CUDA path runs a smoke kernel first;
    - `metal` + float64 is refused (Apple GPUs have no FP64).
  - **Capabilities:**
    - each host has `capabilities` (cpu/cuda/metal: usable, or why not), from a
      probe that now knows Apple GPUs, unified memory (GB10), compute capability and
      `nvidia-smi` errors;
    - the worker refuses a backend it can't run (422) before any code runs.
  - **Placement (`host: auto`, `backend: auto`):** a connected CUDA host first,
    then the local Apple GPU for float32 runs, then the local CPU. It is resolved
    before approval, and the approval shows the device and why.
  - **Scheduler:**
    - GPU jobs run one at a time per device;
    - capability is re-checked at submit;
    - failed jobs carry the benchmark's error and the stderr tail.
  - **Evidence:** a result that ran on the wrong backend is red, and reports show
    the device per variant.

## Verification (2026-10-02)

- **SV4 live (this Mac):** qwen2.5-0.5B-4bit via MLX, pinned `a5339a41`: approved
  download + load 46 s, answered through the router in 0.06 s ("Yes, 2+2=4.").
- **E2 live (GB10, CUDA fp64, 2D, 4 grids):** the IR form of Lax–Wendroff, compiled
  from generated code, gave final states with the same sha256 as the hand-written
  kernel on every grid (19 s). On this Mac the generated Metal kernels of all four
  hand-written schemes are bit-identical to theirs.
- **B4 live:** arXiv:1101.4315 (physics.comp-ph) through llama3.2:3b on the GB10:
  carded in 10 s from 154k characters of HTML, mapped onto the IR, proposed, approved,
  run: yellow, second order confirmed (2.00 vs upwind 0.99, 270x lower error), the
  claimed TVD refuted (new extrema). arXiv:2110.03044: carded, method outside the IR
  vocabulary, kept as a note (no experiment), correctly.
- **B5 live:** a goal's poll found 8 recent papers in 40 s: 4 dismissed with reasons
  (relativistic hydro, Fokker-Planck, Navier-Stokes), 4 carded; none mapped (WENO, DG,
  data-driven, FCT need new IR elements), so nothing proposed.
- **Reviews:** SV4 (2 lenses): 10 code + 10 test findings confirmed, all fixed; E2
  (2 lenses): 9 numerics + 10 test findings confirmed, all fixed (A(c) operators, limiters
  against textbook formulas, RK3/RK4/midpoint stage loops, the claims cap, integrity,
  determinism, stability edges are now pinned by tests). The live MLX run caught a download bug
  (`--include`) that the unit test had encoded wrongly.
- `scripts/test.sh --ignore=…/test_recovery.py`: **497 pass**, 11 skipped, 1m41s.
- **SV3 live** (`NEWTON_TEST_SSH_HOST=ghost@100.85.54.5 … -m live -k
  router_spark_and_mac`, 2m49s), everything through the router key and `/v1`:
  - llama3.2:3b on the GB10 and qwen2.5:0.5b on this Mac (downloaded after approval,
    pinned `a8b0c5157701`) served in parallel: 4 + 4 requests overlapping in time,
    0.3-1.2 s each;
  - a timed CUDA benchmark on the GB10 (P1 kernel vs array): during it the Spark's
    model got `503 device_leased` ("paused: a timed run has host … to itself") while
    the Mac's answered 200; the report says what was paused; the Spark answered
    again right after;
  - nothing of Newton's left running on either machine.
- **SV3 review:** 8 agents (4 lenses, each finding verified by reproduction or
  mutation): 42 confirmed, all fixed; 3 rejected. Mutants that survived the first
  test suite (lease exclusion, release wake-up, leaving the queue) are now killed.
- `scripts/test.sh --ignore=…/test_recovery.py`: **365 pass**, 11 skipped, 1m39s.
- **SV2 live on the GB10** (`NEWTON_TEST_SSH_HOST=ghost@100.85.54.5 … -m live -k
  used_from_this_mac`):
  - created through agentd's API, worker upgraded to protocol 4 on the way;
  - llama3.2:3b ready and reachable on this Mac 15 s after create (model already in
    `~/.newton/models`), through 127.0.0.1:55099 → GB10:43877;
  - answered "Yes." in 0.42 s, then stopped; nothing of Newton's left running on the
    GB10 (the owner's CUDA jobs untouched).
- **SV2 review:** 8 agents (4 lenses, each finding verified by reproduction or
  mutation): 37 confirmed (about 20 distinct issues), all fixed, covered by
  `test_services_rules.py` (15 tests) plus worker tests; 2 rejected.
- `scripts/test.sh --ignore=services/agentd/tests/test_recovery.py`: **325 pass**, 11
  skipped (opt-in), in parallel in 1m47s.

## Verification (2026-10-01)

- **P1 live:**
  - GB10, CUDA float64, 2D van Leer 2048², 50 steps: kernel **48x numpy** (array code
    7.2x), 2.17 ms/step, 61.8 GB/s = 29% of the 214 GB/s measured; **0.0 difference**
    from numpy. Evidence yellow, correctly: the owner's CUDA jobs kept the GPU 78% busy.
  - M3 Max, Metal float32, 4096²: kernel **148x numpy** (array 17x), 98.8 GB/s = 37% of
    the 268 GB/s measured; within 1.8e-7 of numpy. Yellow, correctly: Low Power Mode.
  - The naive kernels reach 29-37% of peak: tiling / vectorized loads are the next
    step if speed matters more (E2 could generate them).
- **P1 review:** 8 agents (4 lenses, each verified by reproduction): 34 confirmed (about
  18 distinct issues) all fixed and covered by tests; 2 rejected.
- **Live on the GB10** (`NEWTON_TEST_SSH_HOST=ghost@100.85.54.5 … -m live`, plain SSH
  over the tailnet, no NVIDIA Sync): **passes in 29 s from an empty `~/.newton`**.
  - Spark: DGX OS / Ubuntu 24.04.3 aarch64, driver 580.95.05, CUDA 13.0, Python
    3.12.3 with python3.12-venv, 121 GB unified memory.
  - Worker: venv created, numpy 2.5.3 through the venv's pip. GPU support then set
    itself up in the background: `cupy-cuda13x` 14.2.0 against the system CUDA 13.0
    (no PyPI toolkit needed), smoke test passed in 1.3 s (sm_121 NVRTC compile,
    exact results). venv 380 MB.
  - Experiment (float64, auto-routed to `cuda: NVIDIA GB10`): upwind vs MUSCL van
    Leer, **green**: order 0.99 vs 1.82, mass drift 5.6e-17, no new extrema.
  - As expected for 1D at 256–2048 cells, the GPU is launch-bound (~0.18 ms per
    step): P1's 2D / larger workloads are what will show a real speedup.
  - Caveat seen: CUDA's free-memory figure (24 GB) understates what's available on
    unified memory (109 GB per `free`); Newton doesn't rely on it.

- `scripts/test.sh --ignore=services/agentd/tests/test_recovery.py`: ruff, format,
  mypy strict (53 files), contracts and bootstrap syntax (sh + dash) are all clean;
  **297 tests pass**, 11 skipped (opt-in), in parallel in about 1m40s.
- **Real OpenSSH** (`NEWTON_TEST_SSHD=1`, a user-level sshd on 127.0.0.1): 4/4 pass.
  They cover the connect flow, refused forwarding, an inherited `LocalForward`, a
  `RemoteCommand` alias, and chatty rc files.
- **Real Metal:** the MLX backend on the M3 Max matches numpy float32 (rel 1e-4),
  conserves mass and stays TVD.
- **Live Metal, end to end** (`NEWTON_TEST_LOCAL_METAL=1 … -m live`): passes in 18 s.
  The report shows device Apple M3 Max, mlx 0.32.3 and the power state.
  - The evidence is yellow, and that is correct: at 4k–16k cells float32
    round-off hides van Leer's second-order gain.
  - P1's precision-aware order fit is what will fix that.
- **G4 on real Linux** (`NEWTON_TEST_DOCKER=1`): the fake GPU through Ubuntu 24.04's
  PEP 668 system pip, incl. the PyPI toolkit fallback; and the real CuPy 14.2 wheel
  from PyPI in a GPU-less container (fake nvidia-smi): its real error
  (`cudaErrorInsufficientDriver`) is recorded, no toolkit download, the worker serves.
- **G4 review:** 10 agents (5 lenses, each verified by reproduction): 33 findings
  confirmed (about 15 distinct issues), all fixed and covered by tests; 4 rejected.
- **Real Linux** (`NEWTON_TEST_DOCKER=1`, arm64 Ubuntu containers): 4/4 pass.
  - Stock 24.04 without python3-venv (DGX-like): real numpy 2.x through the system
    pip, with `flock`, the Unix socket and an offline restart.
  - Bare 24.04: exits 69 with the apt hint.
  - 22.04 with old pip: the `--target` path.
  - 24.04 with venv: the venv's own pip.
- **G1 review:** 34 findings confirmed and fixed. A second round of 4 agents
  reproduced each finding and ran mutation tests; its findings are fixed and
  covered by tests.
- **Not run by the agent:** `test_recovery.py` (the auto-mode check blocked it). Run
  `scripts/test.sh` to include it.
- **Not run:** the live GB10 test (`NEWTON_TEST_SSH_HOST=<alias> scripts/test.sh -m live`).
  It needs S0.

## Architectural decisions

- One worker HTTP protocol for every transport. SSH is only a tunnel plus bootstrap.
- System OpenSSH, so `~/.ssh/config`, the agent, ProxyJump and NVIDIA Sync aliases
  work unchanged.
- Worker job ids are `<job_id>-a<attempt>`. Infrastructure failures retry; a job that
  ran and failed is a result.
- Experiment params travel as `params.json` in the bundle, and argv is fixed per benchmark.

## Known gaps

- GPU support status of a run in progress lives in agentd's memory (`gpu_task`);
  after an agentd restart the next automatic check picks it up again.
- MUSCL-minmod ≈ order 1.65 on smooth data (it clips at extrema), which is expected.
- No SSE yet: the UI will poll `/events?after=` and `/jobs/{id}/logs?offset=`.
- A service's local port changes only if another program took it while the forward
  was down; the new base_url is in `/services/{id}` and the credentials.
- Ollama endpoints have no API key (Ollama has none): any local user of this Mac can
  use one directly while it is up. The router puts its key in front, but the direct
  endpoint stays open, and the device lease only pauses traffic through the router.
- Leases are per host record: the same machine registered twice (two SSH aliases,
  or this Mac as `local` and over SSH) gets two independent leases.
- During an exclusive timed job on this Mac, the router still relays other hosts'
  traffic through this Mac's CPU (small, but not zero).
- Publishing was not run live: it needs the user's GitHub / Notion tokens and consent.
- A 3B model reads papers coarsely (a MUSCL paper became Lax–Wendroff): use a bigger
  default model for ingestion; the claims checks catch what it gets wrong.
- The IR covers three-point flux-form schemes (limiters, one-step or RK): WENO, DG,
  data-driven or flux-corrected methods are carded but not testable yet.
- An aborted request's engine may decode for a moment after its connection closes
  (2 s grace before the lease is granted; an abort is a timing caveat anyway).
- Old OpenSSH (< 8.5) has no `KnownHostsCommand`, so ProxyCommand aliases can't be
  key-scanned there. Direct hosts fall back to ssh-keyscan.
- `venv_ok` compares only Python major.minor: switching between two 3.12 builds
  reuses the venv (accepted, same ABI).
- The Linux `flock` path is only exercised in the Docker tests, not on this Mac.
- The hardening options don't reach ProxyJump hops: ssh applies `-o` only to the
  final connection, so a jump host uses the user's own config for itself.
- The fake CuPy is a numpy subclass, so on non-Apple machines the CUDA test can't
  tell CuPy from numpy calls. The Metal test and the GB10 live test cover the real
  paths.

## Next action

U2 (library, evidence board, research diary in the desktop app) as soon as the user's
design screenshots arrive; B7 (demo hardening, offline modes, signed build). A bigger
reading model (B4/B5 use llama3.2:3b now) will read papers better: qwen2.5:7b or larger
on the GB10.
