# Native endpoint (`lca-endpoint`)

`lca-endpoint` is a small C++17 server that puts any inference runtime behind the
OpenAI-compatible HTTP contract the agent already speaks. The controller never
learns which runtime it is talking to: swapping OpenVINO Model Server for a direct
runtime means pointing the model profile at this endpoint, not changing the agent.

The endpoint owns everything that must behave the same whatever the runtime:

- the OpenAI chat-completions protocol, streaming (SSE) and unary, fail-closed
  request validation;
- one chat template (ChatML with Hermes tool calls, the Qwen3 family) and one
  tool-call parser, so every backend sees byte-identical prompts;
- stop strings, reasoning (`<think>`) separation, queueing and single-stream
  scheduling;
- cancellation that is proven, not assumed: a request leaves
  `/debug/active-requests` only after the backend has actually returned;
- identity and telemetry hooks the endpoint harness consumes, with configured,
  observed and backend-reported facts kept apart.

A backend owns three things: describing itself, counting prompt tokens and
generating text.

## Backends

| `--backend` | What it is | Built |
|---|---|---|
| `fixture` | Deterministic, model-free replies from substring rules. For tests, CI and protocol bring-up. Never claims a device. | always |
| `plugin` | Any shared library implementing [`include/lca/backend_plugin.h`](include/lca/backend_plugin.h). | always |
| `openvino-genai` | In-process OpenVINO GenAI `LLMPipeline`. The reference and baseline for other backends. | when CMake finds OpenVINO GenAI |

`plugins/reference_echo_backend.c` is a complete plain-C plugin used by the tests and
meant as the starting template for a real one.

## Writing a direct runtime backend

A runtime that talks to a device directly (for example a thin path over a device's
firmware and user-mode driver that skips a general-purpose inference framework)
plugs in as a plugin. It does not need to know HTTP, JSON schemas, chat templates,
tool calls or cancellation plumbing, and it never needs to live in this repository.

Implement `lca_backend_get_api()` returning an `lca_backend_api` table:

| Function | Contract |
|---|---|
| `create(config_json, …)` | Load and compile the model. The JSON is operator-supplied and passed through untouched. |
| `describe(…)` | Runtime name/version, requested and **observed** device, model id, context limit, `load_ms`, `compile_ms`. Report `null` for anything the runtime did not actually measure or observe. |
| `count_tokens(text, …)` | Token count without special tokens. Used for context admission. |
| `generate(params, sink, cancel_requested, …)` | Stream UTF-8 pieces into `sink` with special tokens kept (`<think>` and `<tool_call>` are single tokens in the Qwen3 vocabulary; the template's end-of-turn markers are stop strings at the endpoint), poll `cancel_requested` at least once per token (and during prefill where possible), stop when the sink says so, fill `lca_generation_stats` (unknown values negative). |

The header documents the full contract: threading (one thread, one call at a time),
struct versioning via `struct_size`, error buffers and finish reasons. Build it as a
normal shared library against that one header, then:

```text
lca-endpoint --backend plugin --plugin path/to/libmy_backend.so --backend-config my_backend.json --port 18000
```

The same qualification, conformance tests and endpoint harness then apply to it
unchanged, which is the point: a new backend is compared with the existing one on
identical prompts and identical measurement code.

## Build

CMake 3.20+ and a C++17 compiler (GCC, Clang or MSVC).

```text
cmake -S internal/native_endpoint -B build/native-endpoint -DCMAKE_BUILD_TYPE=Release
cmake --build build/native-endpoint --config Release --parallel
ctest --test-dir build/native-endpoint -C Release --output-on-failure
```

Third-party code is not committed. Three single-header MIT libraries (cpp-httplib,
nlohmann/json, doctest for tests) are downloaded once at configure time from
immutable commit URLs and checked against pinned SHA-256 hashes; see
[`cmake/third_party_headers.cmake`](cmake/third_party_headers.cmake). For an offline
build, put the same files in a directory and pass `-DLCA_THIRD_PARTY_DIR=that/dir`;
the hashes are checked there too.

OpenVINO GenAI is picked up automatically when `OpenVINOGenAI_DIR` points at an
installed package (for example after sourcing its `setupvars`). Force it with
`-DLCA_OPENVINO_GENAI=ON` or leave it out with `OFF`.

Useful options: `-DLCA_WARNINGS_AS_ERRORS=ON`, `-DLCA_SANITIZE=address,undefined` or
`thread` (GCC/Clang).

## Run

```text
lca-endpoint --backend openvino-genai --backend-config npu.json --port 18000
```

with, for example:

```json
{
  "model_path": "C:/lca-runtime/models/Qwen3-8B-int4-cw-ov",
  "device": "NPU",
  "model_id": "OpenVINO/Qwen3-8B-int4-cw-ov",
  "max_context_tokens": 8192,
  "properties": {"MAX_PROMPT_LEN": 8192, "CACHE_DIR": "C:/lca-runtime/cache/native"}
}
```

`properties` are handed to the runtime as string-valued properties. The endpoint
binds loopback only unless `--allow-remote-bind` is given; `--api-key-env NAME`
requires a bearer token on every route except `/health`. `--help` lists the rest.
Exit codes: 0 clean stop (SIGINT/SIGTERM), 2 usage, 3 backend failed to load,
4 address could not be bound.

## HTTP surface

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI chat completions. Every response carries `x-request-id`. |
| `GET /v1/models` | The served model. 503 until the backend is loaded, so it doubles as the readiness probe. |
| `/v3/chat/completions`, `/v3/models` | The same routes, for profiles that already address a server under `/v3`. |
| `GET /health` | `loading`, `ready` or `failed` (with the load error). |
| `GET /identity` | Server build, backend kind, runtime, requested and observed device, model, limits, `system_fingerprint`. |
| `GET /telemetry` | `startup.backend_load_ms` / `backend_compile_ms`, `last_request.*` (backend-reported prefill/decode and endpoint-clock timings), `process.rss_bytes`, counters. |
| `GET /debug/active-requests` | `active_request_ids` still being worked on, with `queued`/`running` state. |

Requests the endpoint cannot honour are refused with a 400 rather than silently
ignored: `n > 1`, `tool_choice` `required` or a named function, a non-text
`response_format`, `logprobs`, non-zero penalties, `logit_bias`, non-text content
parts. A prompt that fills the context is a 400 `context_length_exceeded`;
`max_tokens` beyond the remaining room is clamped and the response then ends with
`finish_reason: "length"`.

Streamed responses finish with a finish chunk, an optional usage chunk (with a
llama.cpp-style `timings` block when the backend reported them) and `data: [DONE]`.
A stream that fails or is cancelled server-side ends without `[DONE]`, so the client
sees an incomplete stream rather than a short answer.

## Endpoint harness profile

The generic harness (`internal/perf/endpoint_harness.py`) needs no changes. A local,
uncommitted profile for this endpoint looks like:

```toml
name = "lca-endpoint-npu"

[endpoint]
base_url = "http://127.0.0.1:18000/v1/"
model = "OpenVINO/Qwen3-8B-int4-cw-ov"

[identity]
runtime = "openvino-genai"
device = "NPU"
url = "http://127.0.0.1:18000/identity"
model_path = "model.id"
runtime_path = "runtime.name"
device_path = "device.requested"

[startup]
command = ["path/to/lca-endpoint", "--backend", "openvino-genai", "--backend-config", "npu.json", "--port", "18000"]

[telemetry]
url = "http://127.0.0.1:18000/telemetry"
compile_time_path = "startup.backend_compile_ms"
prefill_time_path = "last_request.backend_prefill_ms"
memory_path = "process.rss_bytes"

[cancellation]
active_requests_url = "http://127.0.0.1:18000/debug/active-requests"
active_request_ids_path = "active_request_ids"
request_id_header = "x-request-id"
```

## What is and is not established

Established by CI on every change: the C++ suite (Linux and Windows, plus ASan/UBSan
and TSan), the product's own OpenAI client, `qualify_server.py` and the endpoint
harness (identity proof, context ladder, proven cancellation) against the fixture
backend, and the OpenVINO GenAI backend building on Linux and Windows and serving a
small real model on CPU through the same qualification and harness.

Not established: any NPU or Panther Lake result, any performance comparison with
OpenVINO Model Server, and product wiring. `serve.py` and the model presets do not
launch this endpoint yet; that wiring should follow a measured comparison on the
target machine, not precede it.

Known limits:

- Only the Hermes `<tool_call>{json}</tool_call>` format is parsed. Profiles using the
  Qwen3-Coder XML tool format need a second parser before they can move here.
- OpenVINO GenAI only checks cancellation between generated tokens, so cancelling
  during a long prefill waits for the prefill to finish. The harness measures this
  rather than assuming it.
- OpenVINO GenAI loads and compiles in one call, so `backend_compile_ms` is `null` for
  that backend and `backend_load_ms` covers both.
- Stopping the endpoint while a backend is still loading waits for the load to end.
