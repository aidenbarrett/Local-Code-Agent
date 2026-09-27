/*
 * Reference implementation of include/lca/backend_plugin.h in plain C.
 *
 * It is a template for a real runtime backend and the fixture the endpoint's
 * tests load through the plugin path. It does no inference: it streams a
 * configured reply one word at a time, or filler words until max_new_tokens
 * when no reply is configured, and honours cancellation between words.
 *
 * Configuration (JSON, optional):
 *   {"model_id": "reference-echo", "reply": "some text", "max_context_tokens": 4096,
 *    "word_delay_ms": 1}
 * The parser below is deliberately tiny: it reads flat string and integer
 * fields and ignores everything else. A real backend should use a JSON library.
 */
/* Strict ISO C11 (no GNU extensions) plus exactly the POSIX surface used: nanosleep. */
#if !defined(_WIN32) && !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#include "lca/backend_plugin.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#include <windows.h>
static void sleep_ms(unsigned ms) { Sleep(ms); }
#else
#include <time.h>
static void sleep_ms(unsigned ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}
#endif

struct lca_backend {
    char model_id[128];
    char* reply; /* NULL: filler until max_new_tokens */
    unsigned long long max_context_tokens;
    unsigned word_delay_ms;
};

static void copy_error(char* buf, size_t len, const char* message) {
    if (!buf || len == 0) return;
    snprintf(buf, len, "%s", message);
}

/* Finds "key": and returns a pointer just past the colon, or NULL. */
static const char* find_field(const char* json, const char* key) {
    char needle[96];
    const char* at;
    snprintf(needle, sizeof needle, "\"%s\"", key);
    at = strstr(json, needle);
    if (!at) return NULL;
    at += strlen(needle);
    while (*at == ' ' || *at == '\t' || *at == '\n' || *at == '\r') ++at;
    if (*at != ':') return NULL;
    ++at;
    while (*at == ' ' || *at == '\t' || *at == '\n' || *at == '\r') ++at;
    return at;
}

/* Reads a JSON string without escapes into a new buffer. */
static char* read_string_field(const char* json, const char* key) {
    const char* at = find_field(json, key);
    const char* end;
    char* out;
    if (!at || *at != '"') return NULL;
    ++at;
    end = strchr(at, '"');
    if (!end) return NULL;
    out = (char*)malloc((size_t)(end - at) + 1);
    if (!out) return NULL;
    memcpy(out, at, (size_t)(end - at));
    out[end - at] = '\0';
    return out;
}

static int read_uint_field(const char* json, const char* key, unsigned long long* out) {
    const char* at = find_field(json, key);
    char* end = NULL;
    unsigned long long v;
    if (!at) return 0;
    v = strtoull(at, &end, 10);
    if (end == at) return 0;
    *out = v;
    return 1;
}

static lca_status echo_create(const char* config_json, lca_backend** out_backend, char* error_buf,
                              size_t error_buf_len) {
    lca_backend* b;
    char* model_id;
    unsigned long long value = 0;
    if (!out_backend) {
        copy_error(error_buf, error_buf_len, "out_backend is NULL");
        return LCA_STATUS_INVALID_ARGUMENT;
    }
    b = (lca_backend*)calloc(1, sizeof *b);
    if (!b) {
        copy_error(error_buf, error_buf_len, "out of memory");
        return LCA_STATUS_INTERNAL;
    }
    snprintf(b->model_id, sizeof b->model_id, "%s", "reference-echo");
    b->max_context_tokens = 4096;
    b->word_delay_ms = 1;
    if (config_json) {
        model_id = read_string_field(config_json, "model_id");
        if (model_id) {
            snprintf(b->model_id, sizeof b->model_id, "%s", model_id);
            free(model_id);
        }
        b->reply = read_string_field(config_json, "reply");
        if (read_uint_field(config_json, "max_context_tokens", &value)) b->max_context_tokens = value;
        if (read_uint_field(config_json, "word_delay_ms", &value)) b->word_delay_ms = (unsigned)value;
    }
    if (b->max_context_tokens == 0) {
        free(b->reply);
        free(b);
        copy_error(error_buf, error_buf_len, "max_context_tokens must be positive");
        return LCA_STATUS_INVALID_ARGUMENT;
    }
    *out_backend = b;
    return LCA_STATUS_OK;
}

static void echo_destroy(lca_backend* backend) {
    if (!backend) return;
    free(backend->reply);
    free(backend);
}

static lca_status echo_describe(lca_backend* backend, char* json_buf, size_t json_buf_len,
                                size_t* required_len) {
    char text[512];
    int n = snprintf(text, sizeof text,
                     "{\"runtime\": {\"name\": \"reference-echo\", \"version\": \"1\"}, "
                     "\"device\": {\"requested\": \"none\", \"observed\": null}, "
                     "\"model\": {\"id\": \"%s\", \"path\": null}, "
                     "\"max_context_tokens\": %llu, \"load_ms\": null, \"compile_ms\": null}",
                     backend->model_id, backend->max_context_tokens);
    if (n < 0) return LCA_STATUS_INTERNAL;
    if (required_len) *required_len = (size_t)n + 1;
    if (!json_buf || json_buf_len < (size_t)n + 1) return LCA_STATUS_BUFFER_TOO_SMALL;
    memcpy(json_buf, text, (size_t)n + 1);
    return LCA_STATUS_OK;
}

static uint64_t count_words(const char* text, size_t len) {
    uint64_t words = 0;
    int in_word = 0;
    size_t i;
    for (i = 0; i < len; ++i) {
        int space = text[i] == ' ' || text[i] == '\n' || text[i] == '\t' || text[i] == '\r';
        if (!space && !in_word) ++words;
        in_word = !space;
    }
    return words;
}

static lca_status echo_count_tokens(lca_backend* backend, const char* text_utf8, size_t len,
                                    uint64_t* out_count, char* error_buf, size_t error_buf_len) {
    (void)backend;
    if (!out_count || (!text_utf8 && len)) {
        copy_error(error_buf, error_buf_len, "invalid arguments");
        return LCA_STATUS_INVALID_ARGUMENT;
    }
    *out_count = count_words(text_utf8, len);
    return LCA_STATUS_OK;
}

static lca_status echo_generate(lca_backend* backend, const lca_generation_params* params,
                                lca_text_sink_fn sink, lca_cancel_requested_fn cancel_requested,
                                void* callback_ctx, lca_generation_stats* out_stats, char* error_buf,
                                size_t error_buf_len) {
    static const char* const filler[] = {"alpha ", "beta ", "gamma ", "delta "};
    int64_t produced = 0;
    const char* cursor;
    if (!params || params->struct_size < sizeof *params || !sink || !cancel_requested || !out_stats) {
        copy_error(error_buf, error_buf_len, "invalid arguments");
        return LCA_STATUS_INVALID_ARGUMENT;
    }
    out_stats->struct_size = sizeof *out_stats;
    out_stats->prompt_tokens = (int64_t)count_words(params->prompt_utf8, params->prompt_len);
    out_stats->cached_prompt_tokens = -1;
    out_stats->prefill_ms = -1;
    out_stats->decode_ms = -1;
    out_stats->finish_reason = LCA_FINISH_END_OF_SEQUENCE;

    cursor = backend->reply;
    for (;;) {
        const char* piece;
        size_t piece_len;
        if (cancel_requested(callback_ctx)) {
            out_stats->completion_tokens = produced;
            return LCA_STATUS_CANCELLED;
        }
        if ((uint64_t)produced >= params->max_new_tokens) {
            out_stats->finish_reason = LCA_FINISH_LENGTH;
            break;
        }
        if (backend->reply) {
            const char* space;
            if (!*cursor) break; /* end of sequence */
            space = strchr(cursor, ' ');
            piece = cursor;
            piece_len = space ? (size_t)(space - cursor) + 1 : strlen(cursor);
            cursor += piece_len;
        } else {
            piece = filler[produced % 4];
            piece_len = strlen(piece);
        }
        if (backend->word_delay_ms) sleep_ms(backend->word_delay_ms);
        ++produced;
        if (sink(callback_ctx, piece, piece_len) == LCA_SINK_STOP) {
            out_stats->finish_reason = LCA_FINISH_STOPPED_BY_SINK;
            break;
        }
    }
    out_stats->completion_tokens = produced;
    return LCA_STATUS_OK;
}

static const lca_backend_api kApi = {
    LCA_BACKEND_ABI_VERSION, sizeof(lca_backend_api), echo_create,  echo_destroy,
    echo_describe,           echo_count_tokens,       echo_generate,
};

LCA_BACKEND_EXPORT const lca_backend_api* lca_backend_get_api(void) { return &kApi; }
