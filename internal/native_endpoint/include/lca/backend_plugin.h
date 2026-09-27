/*
 * Local Code Agent native endpoint: inference backend plugin ABI.
 *
 * This is the one seam through which an out-of-tree inference runtime plugs
 * into lca-endpoint. A backend is a shared library (.so / .dll) that exports
 * a single C function, lca_backend_get_api(), returning a table of function
 * pointers. The endpoint owns HTTP, the OpenAI wire protocol, prompt
 * formatting, tool-call parsing, stop strings, queueing, cancellation
 * bookkeeping, identity and telemetry. The backend owns exactly three things:
 * describing itself, counting prompt tokens and generating text.
 *
 * The ABI is plain C so a backend can be written in C or C++ against any
 * runtime, built with any compiler, and kept entirely outside this
 * repository. Nothing in this header names a vendor, device or runtime.
 *
 * Contract
 * --------
 * - Threading: the endpoint makes every call on one backend object from a
 *   single thread, one call at a time. A backend needs no internal locking.
 *   The sink and cancel callbacks are invoked by the backend, on that same
 *   thread, from inside generate().
 * - Strings are UTF-8 and are passed with explicit lengths. They are not
 *   guaranteed to be NUL-terminated unless stated.
 * - Error buffers are always NUL-terminated by the backend when non-empty;
 *   truncation is allowed.
 * - Structs carry struct_size so either side can grow them later without
 *   breaking older builds. A backend must not read fields beyond the
 *   struct_size the endpoint supplied, and must set struct_size on structs it
 *   fills in.
 * - Cancellation: generate() must poll cancel_requested at least once per
 *   generated token and, where the runtime allows it, during prompt
 *   processing. When it returns non-zero the backend stops as soon as it can
 *   and returns LCA_STATUS_CANCELLED. The endpoint only reports a request as
 *   finished once generate() has actually returned, so the time a backend
 *   takes to honour cancellation is measured, not assumed.
 * - The sink may ask the backend to stop (LCA_SINK_STOP), for example because
 *   a stop string matched. That is a normal finish, not a cancellation:
 *   return LCA_STATUS_OK with finish_reason LCA_FINISH_STOPPED_BY_SINK.
 * - Unknown numeric statistics are reported as negative values, never as 0.
 */
#ifndef LCA_BACKEND_PLUGIN_H
#define LCA_BACKEND_PLUGIN_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LCA_BACKEND_ABI_VERSION 1u
#define LCA_BACKEND_ENTRY_POINT "lca_backend_get_api"

#if defined(_WIN32)
#define LCA_BACKEND_EXPORT __declspec(dllexport)
#else
#define LCA_BACKEND_EXPORT __attribute__((visibility("default")))
#endif

typedef struct lca_backend lca_backend;

typedef enum lca_status {
    LCA_STATUS_OK = 0,
    LCA_STATUS_INVALID_ARGUMENT = 1,
    LCA_STATUS_CONTEXT_EXCEEDED = 2,
    LCA_STATUS_CANCELLED = 3,
    LCA_STATUS_BUFFER_TOO_SMALL = 4,
    LCA_STATUS_INTERNAL = 5,
    LCA_STATUS_UNAVAILABLE = 6
} lca_status;

typedef enum lca_sink_action {
    LCA_SINK_CONTINUE = 0,
    LCA_SINK_STOP = 1
} lca_sink_action;

typedef enum lca_finish_reason {
    /* The model produced its end-of-sequence token. */
    LCA_FINISH_END_OF_SEQUENCE = 0,
    /* max_new_tokens was reached. */
    LCA_FINISH_LENGTH = 1,
    /* The sink returned LCA_SINK_STOP. */
    LCA_FINISH_STOPPED_BY_SINK = 2
} lca_finish_reason;

typedef struct lca_generation_params {
    uint32_t struct_size;
    /* Fully formatted prompt text. The endpoint has already applied the chat
     * template; the backend must not apply another one. */
    const char* prompt_utf8;
    size_t prompt_len;
    uint32_t max_new_tokens;
    /* temperature <= 0 means greedy decoding. */
    float temperature;
    float top_p;
    /* top_k <= 0 means disabled. */
    int32_t top_k;
    /* min_p <= 0 means disabled. */
    float min_p;
    uint64_t seed;
    int32_t has_seed;
} lca_generation_params;

typedef struct lca_generation_stats {
    uint32_t struct_size;
    int64_t prompt_tokens;
    int64_t completion_tokens;
    int64_t cached_prompt_tokens;
    double prefill_ms;
    double decode_ms;
    lca_finish_reason finish_reason;
} lca_generation_stats;

/* Receives decoded text. Pieces may be any length, including empty, but must
 * each be valid UTF-8 on their own (never split a multi-byte character). */
typedef lca_sink_action (*lca_text_sink_fn)(void* callback_ctx, const char* text_utf8, size_t len);

/* Returns non-zero once the endpoint wants this generation abandoned. */
typedef int32_t (*lca_cancel_requested_fn)(void* callback_ctx);

typedef struct lca_backend_api {
    uint32_t abi_version;
    uint32_t struct_size;

    /* config_json is the operator-supplied backend configuration, verbatim
     * (NUL-terminated). The endpoint does not interpret it. */
    lca_status (*create)(const char* config_json, lca_backend** out_backend, char* error_buf,
                         size_t error_buf_len);

    void (*destroy)(lca_backend* backend);

    /* Writes a NUL-terminated JSON object describing the loaded backend:
     *   {
     *     "runtime": {"name": "...", "version": "..."},
     *     "device":  {"requested": "...", "observed": "..." | null},
     *     "model":   {"id": "...", "path": "..." | null},
     *     "max_context_tokens": 8192,
     *     "load_ms": 1234.5 | null,
     *     "compile_ms": 987.6 | null
     *   }
     * "observed" and "compile_ms" must be null unless the runtime actually
     * reported them. If json_buf_len is too small, set *required_len (including
     * the NUL) and return LCA_STATUS_BUFFER_TOO_SMALL. */
    lca_status (*describe)(lca_backend* backend, char* json_buf, size_t json_buf_len,
                           size_t* required_len);

    /* Number of tokens the prompt text occupies, without adding special tokens. */
    lca_status (*count_tokens)(lca_backend* backend, const char* text_utf8, size_t len,
                               uint64_t* out_count, char* error_buf, size_t error_buf_len);

    lca_status (*generate)(lca_backend* backend, const lca_generation_params* params,
                           lca_text_sink_fn sink, lca_cancel_requested_fn cancel_requested,
                           void* callback_ctx, lca_generation_stats* out_stats, char* error_buf,
                           size_t error_buf_len);
} lca_backend_api;

typedef const lca_backend_api* (*lca_backend_get_api_fn)(void);

#ifdef __cplusplus
}
#endif

#endif /* LCA_BACKEND_PLUGIN_H */
