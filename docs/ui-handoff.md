# Newton — backend state for the UI (handoff, 2026-10-02)

> **How to use this document.** You are designing and implementing the desktop UI of
> **Newton**. The attached screenshots define the *look*; this document defines the
> *data, behaviour and rules*. Every screen must be built only from the HTTP API
> described here. If a screenshot shows something the API doesn't provide, list it as
> a gap; don't invent data, and don't put product logic in the UI.

---

## 1. What Newton is

A macOS research agent for numerical-methods work (PDE solvers, CFD). It watches
arXiv, turns papers into structured **research cards**, proposes **experiments**
against the user's benchmark (linear advection, 1D/2D), runs them on a remote Linux
GPU box (an NVIDIA GB10 / DGX Spark over SSH) or on the Mac's own GPU, and reports
**evidence**: green / yellow / red / unknown, on whether a method really helps and
whether the paper's claims hold.

It also runs **model services** (LLMs on the GB10 or the Mac) behind one
OpenAI-compatible **router**, used for reading papers.

- **The Mac is the control plane.** `agentd` is a local daemon (Python, FastAPI,
  SQLite) on `127.0.0.1`. The UI only talks to agentd.
- **Linux is invisible compute.** The user never opens a terminal on the GPU box.
- **Nothing outward-facing happens without an approval:** running an experiment,
  downloading a model, publishing a report.
- **Single user, single Mac.** There are no accounts and no roles: one local
  **profile**.

---

## 2. The app: stack, rules, connection

- **Stack.** `apps/desktop` is Tauri v2, React 19, TypeScript, Vite and pnpm.
  - It is a shell today: a placeholder `App.tsx` with a host list.
  - `src/api/client.ts` and `src/api/types.ts` exist; extend them.
  - Run it with `pnpm dev:repo` (agentd via `scripts/dev.sh`), or `pnpm dev`.
- **No business logic in the UI.** It shows what agentd returns. Verdicts,
  placement, estimates, approval details and evidence colours all come from the API.
- **Connection.**
  - The Tauri command `agentd_connection` returns `{base_url, token, data_dir}`, with
    `base_url` defaulting to `http://127.0.0.1:8765`.
  - Every request carries `Authorization: Bearer <token>`; only `GET /health` is public.
  - The client maps failures to `token_missing | config | unreachable | unauthorized | http`.
  - Show a clear "agentd isn't running" state for `token_missing` and `unreachable`.
- **CSP.** `connect-src` allows only Tauri IPC and `http://127.0.0.1:*`. The UI never
  calls the internet directly.
- **Live updates: there is no SSE or WebSocket yet. Poll.**
  - Lists: refresh every 2–5 s while visible.
  - `GET /events?after=<last_id>` is the activity feed and change signal. Each event
    has `{id, ts, entity_type, entity_id, kind, data}`.
  - Logs: `GET /jobs/{id}/logs?stream=stdout|stderr&offset=N` returns
    `{data, offset, next_offset, size}`. Append `data` and poll again from
    `next_offset`; you're caught up when `next_offset == size`.
  - Service logs: `GET /services/{id}/logs?offset=N` returns
    `{data, offset, next_offset, eof}`.
- **Errors.**
  - Most errors are `{"error": "<human sentence>", "code"?: "..."}`, with status 400,
    404, 409 or 502. Show the sentence; it is written for users.
  - Validation errors are `422 {"detail": [{loc, msg, ...}]}` (FastAPI). Map `loc` to
    form fields.
  - Host key unknown is `409 {"error", "fingerprints": [...]}` and drives the
    trust-the-key step (§5.1).
  - `/v1/*` (the router) uses OpenAI's error shape: `{"error": {message, type, code}}`.
- **Times.** Unix seconds as floats (`created_at`, `updated_at`, `finished_at`, …).
- **IDs.** Prefixed strings: `host-…`, `exp-…`, `job-…`, `svc-…`, `paper-…`,
  `goal-…`, `pub-…`, `finding-…`, `req-…`. The local Mac host is always `id: "local"`.

---

## 3. Suggested information architecture

This is a suggestion; follow the screenshots where they differ.

| Area | What the user does | Main endpoints |
|---|---|---|
| **Home / Inbox** | pending approvals, running work, new papers, recent evidence | `/approvals?status=pending`, `/experiments`, `/research/items`, `/events` |
| **Machines** | add a GPU box from `~/.ssh/config`, trust its key, connect, see GPU/CUDA/Metal status | `/ssh/hosts`, `/hosts`, `/hosts/{id}/connect`, `/hosts/{id}/hostkeys…` |
| **Experiments** | design (baseline vs candidates), approve with estimate, live logs, report, evidence | `/benchmarks`, `/schemes`, `/experiments`, `/jobs`, `/approvals` |
| **Research** | goals, the arXiv inbox, paper cards, propose an experiment, findings (memory) | `/goals`, `/research/*`, `/findings` |
| **Models** | model services on the GPU box or the Mac, router status, request log | `/services`, `/router/*`, `/v1/models` |
| **Settings** | profile, Mac models switch, router key, GitHub / Notion connections | `/profile`, `/router/credentials`, `/connectors` |

---

## 4. Enums and states (drive badges and colours)

- **Evidence:** `green` (better, claims hold), `yellow` (better but caveats: a broken
  claim, a timing caveat), `red` (worse, or an integrity problem), `unknown`.
- **Host `status`:** `online`, `unknown`, or `error:<code>`. Codes include
  `hostkey_unknown`, `unreachable` and `deps_missing`. Show `last_error` as the
  explanation.
- **Host `capabilities`:** `{cpu, cuda, metal}`, each `{ok, device?, reason?}`. For
  example `cuda: {ok: true, device: "NVIDIA GB10"}`, or
  `metal: {ok: false, reason: "…"}`.
- **Host `gpu_task`:** present while GPU support is being set up (installing CuPy):
  `{state: "running"|"done"|"failed", ...}`.
- **Experiment:** `awaiting_approval → executing → evaluating → reported | failed`, or
  `rejected`, or `cancelled`.
- **Job:** `pending_approval`, `queued`, `submitting`, `running`, `collecting`, then
  one of `succeeded`, `failed`, `timed_out`, `cancelled`, `rejected`.
  - `error` on a queued job explains a wait, for example "waiting for 1 model
    request(s) on this host to finish: a timed job runs alone".
- **Approval:** `pending | approved | rejected`. `kind` is one of:
  - `execute_experiment`
  - `start_service` (model download or model-supplied code)
  - `publish_report`
- **Service (model server):** `awaiting_approval → approved → starting → ready ⇄
  draining → stopping → stopped`, or `failed`, `lost`, `rejected`, `cancelled`.
- **Research item (paper):** `discovered → (triaged) → extracting → carded →
  experiment_planned → executing → evaluating → reported`, or `dismissed`, or `failed`.
- **Publication:** `awaiting_approval → approved → publishing → published | failed`,
  or `rejected`.

---

## 5. API reference by area

All paths are relative to `base_url`. JSON in and out unless noted.

### 5.1 Machines (hosts)

- **`GET /hosts`** returns the list of hosts, the local Mac included (`id: "local"`,
  `kind: "local"`). Main fields:
  - `id`, `name`, `kind` (`"local"` or `"ssh"`), `status`, `ssh_target`
  - `last_error`, `last_checked_at`, `gpu_support` (`auto|off|cuda`), `max_parallel_jobs`
  - `hardware`: OS, CPU, memory `{total}`, GPU info, worker `{version, ok}`; may be null
  - `capabilities`: `{cpu, cuda, metal}` (§4)
  - `gpu_task` (optional)
- **`GET /ssh/hosts`** lists hosts from `~/.ssh/config` for a "pick a machine" list:
  - `alias`, `source`, `hostname`, `user`, `port`, `proxy`, `config_file`
  - `host_id`: already added, or null
  - `addable`: bool
- **`POST /hosts`** adds a host: `{name, ssh_target, gpu_support?: "auto"}`. Returns
  the host.
- **`POST /hosts/{id}/connect`** is the one-click path: it verifies the host key,
  installs or starts the worker, checks it, and queues a selftest.
  - Returns `{host, selftest_job_id}`.
  - `409 {"error", "code": "hostkey_unknown", "fingerprints": [{"type": "ssh-ed25519", "fingerprint": "SHA256:…"}]}` means the host key isn't
    trusted. Show the fingerprints and ask the user to confirm. Then call
    `POST /hosts/{id}/hostkeys/trust {fingerprints: ["SHA256:…"]}` (the strings) and connect again.
- `GET /hosts/{id}/hostkeys` returns the scanned keys.
- `POST /hosts/{id}/check` re-checks the host.
- `POST /hosts/{id}/bootstrap` (re)installs the worker.
- `PATCH /hosts/{id} {gpu_support?, max_parallel_jobs?}` changes settings.
- **`DELETE /hosts/{id}`** returns 204. Add `?force=true` when it has active model
  services (otherwise 400).
- `POST /hosts/{id}/gpu-support` (202) starts GPU support setup now.
- `POST /hosts/{id}/selftest {sleep?, fail?}` runs a connectivity test job.

### 5.2 Experiments, jobs, approvals

- **`GET /benchmarks`** returns `[{name, description, metrics, params_schema}]`:
  `linear_advection_1d` and `linear_advection_2d`. `params_schema` is the JSON Schema
  of the variant params (use it to build forms).
- **`GET /schemes`** returns the scheme library:
  `[{name, document (SchemeIR), digest, hand_written}]`.
  - Hand-written: `upwind`, `lax_wendroff`, `muscl_minmod`, `muscl_vanleer`.
  - Plus, as documents: `muscl_superbee`, `muscl_mc`, `muscl_koren`,
    `muscl_vanleer_ssprk2`.
- **`POST /schemes/check`** takes a SchemeIR document and returns
  `{document, digest, method_digest, header, header_hash}`, or 400 with the rule it
  broke.
- **`POST /experiments`** takes an ExperimentSpec and returns the experiment in
  `awaiting_approval`.

  ```json
  {
    "title": "MUSCL van Leer vs upwind",
    "benchmark": "linear_advection_1d",
    "objective": "accuracy",
    "host_id": "auto", "backend": "auto",
    "hypothesis": "optional text",
    "variants": [
      {"role": "baseline", "label": "upwind", "params": {"scheme": "upwind"}},
      {"role": "candidate", "label": "vanleer", "params": {"scheme": "muscl_vanleer"}}
    ]
  }
  ```

  - `objective` is `"accuracy"` or `"performance"`.
  - `backend` is `auto`, `cpu`, `cuda` or `metal`.
  - Variant `params`:
    - `scheme`: a library name, or `"ir"` together with `scheme_ir: {SchemeIR}`
    - `resolutions`, `mode` (`convergence|throughput`), `implementation` (`array|kernel`)
    - `cfl`, `velocity`, `velocity_y`, `t_final`, `steps`, `repeats`
    - `initial_condition` (`sine|gaussian|square`), `precision` (`float64|float32`)
  - Fairness rule: variants may differ only in what is compared. Accuracy compares
    the scheme or implementation; performance compares only the implementation.
    Otherwise the result is a 422 with a clear message.
- **`GET /experiments?state=`** lists experiments.
- **`GET /experiments/{id}`** returns:
  - `spec`, `state`, `evidence`, `error`, `report_path`
  - `evaluation` (ValidationReport, below)
  - `jobs` (job views)
  - `approval: {id, status}`
- **`GET /experiments/{id}/report`** returns the report as Markdown text.
  - Sections: results table, verdicts, "Speed and roofline", "Assumption checks",
    "Provenance".
  - Report images: `GET /experiments/{id}/report/files/{path}`.
  - `GET /experiments/{id}/export`: a zip of `report.md`, `report.json` (the
    ValidationReport) and the report's files; 409 until the experiment is reported.
- `POST /experiments/{id}/cancel`.
- **ValidationReport** (`experiment.evaluation`):

  ```json
  {"experiment_id", "title", "benchmark", "evidence": "green|yellow|red|unknown", "summary",
   "variants": [{"label", "role", "job_id", "job_state", "backend", "device",
                 "metrics": {"l2_error", "linf_error", "observed_order", "runtime",
                             "conservation", "tv_increase", "agrees_with_numpy", ...},
                 "assumptions": [{"claim": "order|conservation|tvd|max_cfl|agrees_with_numpy|deterministic",
                                  "claimed", "measured", "holds": true|false|null}]}],
   "verdicts": [{"label", "evidence", "summary",
                 "checks": [{"name", "passed": true|false|null, "detail"}]}],
   "provenance": {"repository_commit", "worker_version", "jobs": {...}}}
  ```

  - Check names include `completed`, `stable`, `conservation`, `accuracy`,
    `convergence_order`, `non_oscillatory`, `cost`, `claims` and `integrity`, plus
    speed checks for performance runs.
  - Show `detail` verbatim.
- **Jobs:**
  - `GET /jobs?state=&experiment_id=` lists them.
  - `GET /jobs/{id}` includes `manifest`, `metrics`, `results` (full benchmark output,
    with `runs[]`, `environment.device`, `environment.kernels` hashes and
    `assumptions`), `error`, `attempt`, `started_at` and `finished_at`.
  - Logs, as in §2.
  - Artifacts: `GET /jobs/{id}/artifacts` returns file paths;
    `/jobs/{id}/artifacts/{path}` returns the file (plots are PNG).
  - `POST /jobs/{id}/cancel`.
- **Approvals.** The approval card must show `details`. `GET /approvals?status=pending`
  returns `[{id, kind, subject_type, subject_id, title, details, status, created_at}]`.
  - `execute_experiment` details:
    - `host {id, name, kind}`, `backend`, `device`, `placement` (why this machine)
    - `estimated_seconds`, `estimated_peak_gb`, `estimate_basis`
    - `variants[]` (with `scheme_ir_digest` for document schemes)
    - `hypothesis`, `timeout_seconds`, `repository_commit`
  - `start_service` details:
    - `host`, `engine`, `model`, `revision`
    - `download`, `download_gb_estimate`
    - `trust_remote_code`, `memory_gb`, `admission`
  - `publish_report` details:
    - `target`, `destination`, `experiment_id`, `evidence`
    - `characters`, `sha256`, `preview` (the first 1,500 characters of exactly what
      will be sent)
  - `POST /approvals/{id}/approve` and `POST /approvals/{id}/reject` take an optional
    `{note}`.

### 5.3 Research: goals, papers, findings

- **Goals:**
  - `GET /goals` lists them; `POST /goals` creates one:

    ```json
    {"title": "Higher-order advection schemes that beat upwind",
     "description": "...", "keywords": ["flux limiter", "TVD scheme"],
     "categories": ["physics.comp-ph", "math.NA", "physics.flu-dyn"],
     "poll_hours": 24, "auto_propose": true}
    ```

  - `PATCH /goals/{id}` takes the same fields plus `status: active|paused|archived`.
  - A goal also has `last_polled_at`.
- **`POST /goals/{id}/poll`** looks for papers now and returns a summary:
  - `{goal_id, found, new, relevant, dismissed, carded, proposed: [experiment ids],
    skipped: [{item, why}], error?}`
  - `error` is set when no model is configured.
- **`POST /research/ingest {ref, goal_id?, model?}`** (202) ingests one paper. `ref`
  is an arXiv id or URL. It returns the item, which then moves from `discovered` to
  `extracting`, then `carded` or `failed`.
- **`GET /research/items?goal_id=`** and **`GET /research/items/{id}`** return items:
  `{id, goal_id, kind: "paper", title, source: "arxiv", external_id, state, data,
  created_at}`, where `data` holds:
  - `paper: {arxiv_id, title, abstract, authors[], published, categories[], url,
    journal_ref, doi}` (`journal_ref`/`doi` from arXiv, null when the authors gave none)
  - `text: {from: "html"|"pdf", characters, sha256}`
  - `triage: {relevant, why}` (from the research loop)
  - `card`:
    - `relevant`, `summary`
    - `method: {name, limiter, second_order_correction, time_integration, order,
      max_cfl, tvd}`
    - `claims: [{kind, text}]`, `benchmarks: []`
  - `scheme_ir`: the SchemeIR Newton mapped from the card, or null
  - `scheme_note`: "mapped onto Newton's IR", or why not
  - `extraction`: provenance, i.e. which model, service, host and router request,
    `characters_read` and the reader's `context_length` (the paper is cut to fit it)
  - `error`, `proposal_note`
- **`POST /research/items/{id}/propose {host_id?, backend?, baseline?, initial_condition?,
  retest?}`** creates the experiment the card suggests, awaiting approval. Works for a
  `carded` paper and again for a `reported` one (another baseline, machine, …).
  - Scientific memory: the same scheme (`method_digest`) already tested gives
    `409 {error, code: "already_tested", experiment_id}`; already planned elsewhere gives
    `409 {error, code: "already_planned", research_item_id}`. Show the sentence and
    offer "Propose anyway", which sends `retest: true`.
  - A rejected, cancelled or failed experiment puts its paper back to `carded` (or
    `reported` when it was tested before).
- **`GET /findings?goal_id=`** is the scientific memory:
  - `[{experiment_id, research_item_id, scheme_name, evidence,
    claims: [{claim, claimed, holds}], summary, created_at}]`

### 5.4 Models: services and router

- **`GET /services?host_id=`** lists services. Each has:
  - `id`, `host_id`, `name`, `spec`, `state`, `remote_state`, `healthy`
  - `remote_port`, `local_port`, `error`, `ready_at`
  - `endpoint: {base_url, auth: "bearer"|"none", reachable}`, while serving
  - `auth_note` (Ollama and MLX have no keys; prefer the router)
- **`POST /services {host_id, name, settings}`** creates one, as `approved`, or
  `awaiting_approval` when it needs a download. `settings`:
  - `engine`: `ollama | vllm | mlx | fake`
  - `model`, `revision` (pinned: an Ollama digest, or a 40-hex commit for vllm and mlx)
  - `memory_gb`, `context_length`, `parallel`, `kv_cache_type`, `trust_remote_code`
    (vllm only)
  - 409 when the host can't take it now (not enough memory or disk, Mac on battery,
    and so on). The message says why.
- Other service actions:
  - `POST /services/{id}/stop`
  - `POST /services/{id}/drain?seconds=`
  - `GET /services/{id}/logs?offset=`
  - `GET /services/{id}/credentials` returns the direct endpoint and key (avoid in the
    UI; the router is the way)
- **`GET /router/status`** returns `{leases: [...], in_flight, waiting, models: [...]}`.
  - `models` is the same list as `/v1/models`, with `newton.services[]`: per service,
    `routable`, `paused` (a timed benchmark has the host), `in_flight` and `parallel`.
  - `leases`: `{host_id, job_id, granted, in_flight, waiting_on, caveats}`, shown as
    "paused for a timed run".
- `GET /router/requests?service_id=&limit=` is the request log. It holds no content:
  timings, status, attempts and token counts, with
  `model_requested → service/host/engine/model/revision`.
- `GET /router/credentials` returns `{base_url: ".../v1", api_key}`, the
  inference-only key for tools. `POST /router/credentials/rotate` rotates it.
- **`/v1/*`** is the OpenAI-compatible endpoint, for tools rather than the UI. The UI
  may use `GET /v1/models`.

### 5.5 Profile and settings

- `GET /profile` returns `{display_name, default_model, mac_models, updated_at}`.
- `PATCH /profile {display_name?, default_model?, mac_models?}`.
  - `default_model` is the model used for reading papers, and what `"default"` means
    in the router.
  - `mac_models` is off by default. Turning it on allows small MLX models on this Mac;
    they run only on AC power. Turning it off stops them.

### 5.6 Publishing (GitHub, Notion)

- `GET /connectors` returns `{github: bool, notion: bool}`.
- `PUT /connectors/{github|notion} {token}` stores the token in the Keychain.
  - `POST /connectors/github/import-gh` uses the GitHub CLI's login instead.
  - `DELETE /connectors/{target}` disconnects.
- **`POST /experiments/{id}/publish {target, destination}`** returns a publication
  awaiting approval. `destination` is one of:
  - `github` gist: `{}`
  - `github` issue: `{kind: "issue", repo: "owner/name"}`
  - `notion`: `{parent_page_id: "<32 hex>"}`
- `GET /connectors/notion/pages?query=` lists the pages the Notion integration can
  write under: `[{id (32 hex), title, url, icon (emoji or null)}]`, last edited first;
  `409 {code: "not_connected"}` without a token, `502` with Notion's own message.
- `GET /publications?experiment_id=` returns
  `[{id, target, destination, state, url, error, content_sha256, created_at}]`.

### 5.7 System

- `GET /health` (public) returns `{status, version, db: {ok, schema_version},
  scheduler: {running, ticks}, uptime_seconds}`.
- `GET /events?after=&limit=&entity_type=&entity_id=` is the activity feed (§2).
  Useful kinds include:
  - `state` (`data: {from, to}`), `created`, `connected`, `device_lease`, `forwarded`
  - `reachable`/`unreachable`, `poll`, `carded`, `proposed`, `published`, `finding`

---

## 6. Key user flows (what the UI should make easy)

1. **Connect the GPU box**
   1. The user picks an alias from `GET /ssh/hosts`, then `POST /hosts`, then
      `POST /hosts/{id}/connect`.
   2. On a 409, the UI shows the fingerprints with Trust / Cancel. On trust it calls
      `POST /hosts/{id}/hostkeys/trust`, then connect again.
   3. Then the host card shows status and capabilities. `gpu_task` shows "setting up
      GPU support…" while CuPy installs (a minute or so the first time).
2. **Run an experiment**
   1. The user picks a baseline and candidates (library schemes or documents) and
      submits; this returns `awaiting_approval`.
   2. The approval card shows the machine, device, why it was chosen, the time and
      memory estimate, the variants, and the hypothesis.
   3. After Approve, the jobs run: a live state timeline per job, and live logs.
   4. When reported: an evidence badge per candidate, check details, metrics, a
      convergence plot (artifact), and the report.
3. **Read a paper**
   1. The user pastes an arXiv link into `POST /research/ingest`.
   2. The paper shows as extracting, then carded: summary, method, claims, and
      whether it maps onto a testable scheme (with the reason if not).
   3. Propose creates an experiment, then the approval flow (2) follows.
4. **Let it watch arXiv**
   1. The user creates a goal (keywords, categories, how often), then waits for the
      schedule or presses "Poll now".
   2. The inbox shows new papers, relevant or dismissed with the reason, and their
      cards. Experiments proposed automatically show up as pending approvals.
   3. Findings list what each tested method showed, claim by claim.
5. **Models**
   1. To serve a model on the GPU box: `POST /services`, approval if it downloads,
      then ready. The router lists it.
   2. During a timed benchmark on that machine, its models show "paused".
   3. Settings: the default model for reading papers, the router key for other tools,
      and the Mac models switch.
6. **Publish**
   1. Connect GitHub or Notion once.
   2. On a reported experiment, press Publish, choose where, then approve: the card
      shows the preview of exactly what is sent.
   3. The result is a link (the publication `url`).

---

## 7. Rules the UI must keep

- **Approvals are the only way work starts.** Never auto-approve, and never hide
  `details`. Show the estimate and the placement reason.
- **Show agentd's sentences as they are** (`error`, `detail`, `summary`,
  `scheme_note`, `triage.why`). They are written for users.
- **Never show or store secrets.**
  - The router key is shown only on an explicit "Reveal / Copy" in Settings.
  - Tokens are write-only: the UI sends them and never reads them back; there is no
    endpoint that returns them.
  - Don't log the agentd token.
- **Evidence colours come from `evidence`. Don't recompute them.** A yellow can mean
  "better, but a claim failed". Show the `claims` check.
- **Polling must stop when a view isn't visible**, and must back off when agentd is
  unreachable.

## 8. Known gaps (what the backend doesn't do yet)

- No push updates: poll as in §2.
- No user accounts or roles, by design.
- No "edit an experiment": create a new one.
- Benchmarks are linear advection only, in 1D and 2D.
- The scheme IR covers three-point flux-form schemes: limiters, one-step or
  Runge-Kutta. Papers using WENO, DG and similar get a card but no experiment.
- Typed response models for the UI are hand-written in `apps/desktop/src/api/types.ts`.
  `packages/contracts/*.schema.json` has the request schemas (ExperimentSpec,
  SchemeIR, ServiceCreate, GoalCreate, ProfileUpdate, …).

## 9. Current real data (for realistic mock-ups)

- **Hosts.**
  - `local`, "This Mac": Apple M3 Max, 36 GB, Metal ok.
  - `spark`: NVIDIA GB10 (DGX Spark) over SSH (`ghost@100.85.54.5`), 121 GB unified
    memory, CUDA 13.0, CuPy 14.2 ok.
- **Typical results.**
  - Kernel 48× numpy on the GB10 (29% of peak bandwidth); 148× on the M3 Max.
  - A paper's claimed TVD refuted: yellow.
  - Lax–Wendroff vs upwind: order 2.00 vs 0.99, L2 error 270× lower.
- **Models.** `llama3.2:3b` (Ollama) on the GB10 reads papers in about 10 s each;
  `mlx-community/Qwen2.5-0.5B-Instruct-4bit` runs on the Mac.
