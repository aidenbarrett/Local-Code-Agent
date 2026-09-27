#include "plugin_backend.hpp"

#include <algorithm>
#include <chrono>
#include <exception>
#include <filesystem>
#include <vector>

#include <nlohmann/json.hpp>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace lca {

namespace {

BackendErrorKind kind_of(lca_status status) {
    switch (status) {
        case LCA_STATUS_INVALID_ARGUMENT: return BackendErrorKind::InvalidArgument;
        case LCA_STATUS_CONTEXT_EXCEEDED: return BackendErrorKind::ContextExceeded;
        case LCA_STATUS_UNAVAILABLE: return BackendErrorKind::Unavailable;
        default: return BackendErrorKind::Internal;
    }
}

[[noreturn]] void raise(lca_status status, const char* what, const std::string& detail) {
    std::string message = std::string("plugin ") + what + " failed (status " +
                          std::to_string(static_cast<int>(status)) + ")";
    if (!detail.empty()) message += ": " + detail;
    throw BackendError(kind_of(status), message);
}

std::optional<std::string> optional_string(const nlohmann::json& obj, const char* key) {
    const auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return std::nullopt;
    if (!it->is_string()) throw BackendError(BackendErrorKind::Internal,
                                             std::string("plugin describe: ") + key + " must be a string");
    return it->get<std::string>();
}

std::optional<double> optional_number(const nlohmann::json& obj, const char* key) {
    const auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return std::nullopt;
    if (!it->is_number()) throw BackendError(BackendErrorKind::Internal,
                                             std::string("plugin describe: ") + key + " must be a number");
    return it->get<double>();
}

struct CallbackContext {
    const TextSink* sink;
    const CancelFlag* cancel;
    std::exception_ptr error;
    std::uint64_t non_empty_pieces = 0;
};

extern "C" lca_sink_action lca_endpoint_plugin_sink(void* ctx, const char* text, size_t len) {
    auto* c = static_cast<CallbackContext*>(ctx);
    try {
        if (text && len > 0) ++c->non_empty_pieces;
        const auto action = (*c->sink)(std::string_view(text ? text : "", text ? len : 0));
        return action == SinkAction::Stop ? LCA_SINK_STOP : LCA_SINK_CONTINUE;
    } catch (...) {
        c->error = std::current_exception();
        return LCA_SINK_STOP;
    }
}

extern "C" int32_t lca_endpoint_plugin_cancel(void* ctx) {
    return static_cast<CallbackContext*>(ctx)->cancel->requested() ? 1 : 0;
}

}  // namespace

// ------------------------------------------------------------ shared library

SharedLibrary::SharedLibrary(const std::string& path_utf8) {
#if defined(_WIN32)
    const auto path = std::filesystem::u8path(path_utf8);
    handle_ = reinterpret_cast<void*>(LoadLibraryW(path.wstring().c_str()));
    if (!handle_) {
        throw BackendError(BackendErrorKind::Unavailable,
                           "cannot load plugin " + path_utf8 + " (Windows error " +
                               std::to_string(GetLastError()) + ")");
    }
#else
    handle_ = dlopen(path_utf8.c_str(), RTLD_NOW | RTLD_LOCAL);  // NOLINT(cppcoreguidelines-prefer-member-initializer): set per platform
    if (!handle_) {
        const char* err = dlerror();
        throw BackendError(BackendErrorKind::Unavailable,
                           "cannot load plugin " + path_utf8 + ": " + (err ? err : "unknown error"));
    }
#endif
}

SharedLibrary::~SharedLibrary() {
    if (!handle_) return;
#if defined(_WIN32)
    FreeLibrary(reinterpret_cast<HMODULE>(handle_));
#else
    dlclose(handle_);
#endif
}

void* SharedLibrary::symbol(const char* name) const {
#if defined(_WIN32)
    return reinterpret_cast<void*>(GetProcAddress(reinterpret_cast<HMODULE>(handle_), name));
#else
    return dlsym(handle_, name);
#endif
}

std::string bounded_c_string(const char* data, std::size_t size) {
    if (data == nullptr) return {};
    const char* const end = std::find(data, data + size, '\0');  // NOLINT(cppcoreguidelines-pro-bounds-pointer-arithmetic): the one bounded read of a C buffer
    return {data, end};
}

// ------------------------------------------------------------ plugin backend

PluginBackend::PluginBackend(const std::string& library_path, const std::string& config_json) {
    library_ = std::make_unique<SharedLibrary>(library_path);
    // A symbol address becomes a function pointer only here, at the C ABI boundary.
    auto entry = reinterpret_cast<lca_backend_get_api_fn>(  // NOLINT(cppcoreguidelines-pro-type-reinterpret-cast)
        library_->symbol(LCA_BACKEND_ENTRY_POINT));
    if (!entry) {
        throw BackendError(BackendErrorKind::Unavailable,
                           library_path + " does not export " LCA_BACKEND_ENTRY_POINT);
    }
    api_ = entry();
    if (!api_) throw BackendError(BackendErrorKind::Unavailable, "plugin returned no API table");
    if (api_->abi_version != LCA_BACKEND_ABI_VERSION) {
        throw BackendError(BackendErrorKind::Unavailable,
                           "plugin ABI version " + std::to_string(api_->abi_version) +
                               " does not match endpoint ABI version " +
                               std::to_string(LCA_BACKEND_ABI_VERSION));
    }
    if (api_->struct_size < sizeof(lca_backend_api) || !api_->create || !api_->destroy ||
        !api_->describe || !api_->count_tokens || !api_->generate) {
        throw BackendError(BackendErrorKind::Unavailable, "plugin API table is incomplete");
    }

    const auto started = std::chrono::steady_clock::now();
    PluginErrorBuffer error;
    const lca_status created =
        api_->create(config_json.c_str(), &backend_, error.data(), PluginErrorBuffer::size());
    if (created != LCA_STATUS_OK || !backend_) {
        backend_ = nullptr;
        raise(created == LCA_STATUS_OK ? LCA_STATUS_INTERNAL : created, "create", error.text());
    }
    const double create_ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();

    try {
        std::vector<char> buffer(4096);
        size_t required = 0;
        lca_status described = api_->describe(backend_, buffer.data(), buffer.size(), &required);
        if (described == LCA_STATUS_BUFFER_TOO_SMALL && required > buffer.size() && required < (1u << 20)) {
            buffer.resize(required);
            described = api_->describe(backend_, buffer.data(), buffer.size(), &required);
        }
        if (described != LCA_STATUS_OK) raise(described, "describe", std::string());
        buffer.back() = '\0';
        const auto info = nlohmann::json::parse(buffer.data(), nullptr, false);
        if (info.is_discarded() || !info.is_object()) {
            throw BackendError(BackendErrorKind::Internal, "plugin describe did not return a JSON object");
        }
        const auto runtime = info.value("runtime", nlohmann::json::object());
        const auto device = info.value("device", nlohmann::json::object());
        const auto model = info.value("model", nlohmann::json::object());

        identity_.kind = "plugin";
        identity_.plugin_path = library_path;
        identity_.runtime_name = optional_string(runtime, "name").value_or("unknown");
        identity_.runtime_version = optional_string(runtime, "version").value_or("unknown");
        identity_.device_requested = optional_string(device, "requested").value_or("unknown");
        identity_.device_observed = optional_string(device, "observed");
        identity_.model_id = optional_string(model, "id").value_or("");
        identity_.model_path = optional_string(model, "path");
        if (identity_.model_id.empty()) {
            throw BackendError(BackendErrorKind::Internal, "plugin describe did not name a model id");
        }
        const auto ctx = optional_number(info, "max_context_tokens");
        if (!ctx || *ctx < 1) {
            throw BackendError(BackendErrorKind::Internal,
                               "plugin describe did not report a positive max_context_tokens");
        }
        identity_.max_context_tokens = static_cast<std::uint64_t>(*ctx);
        // What the plugin measured itself wins; otherwise the endpoint's own
        // measurement of create() is the load time.
        identity_.load_ms = optional_number(info, "load_ms").value_or(create_ms);
        identity_.compile_ms = optional_number(info, "compile_ms");
    } catch (...) {
        api_->destroy(backend_);
        backend_ = nullptr;
        throw;
    }
}

PluginBackend::~PluginBackend() {
    if (backend_) api_->destroy(backend_);
    backend_ = nullptr;
    // library_ unloads after destroy() has returned.
}

std::uint64_t PluginBackend::count_prompt_tokens(const std::string& prompt) {
    PluginErrorBuffer error;
    uint64_t count = 0;
    const lca_status status = api_->count_tokens(backend_, prompt.data(), prompt.size(), &count,
                                                 error.data(), PluginErrorBuffer::size());
    if (status != LCA_STATUS_OK) raise(status, "count_tokens", error.text());
    return count;
}

GenerationOutcome PluginBackend::generate(const GenerationRequest& request, const TextSink& sink,
                                          const CancelFlag& cancel) {
    lca_generation_params params{};
    params.struct_size = sizeof params;
    params.prompt_utf8 = request.prompt.data();
    params.prompt_len = request.prompt.size();
    params.max_new_tokens = request.max_new_tokens;
    params.temperature = request.sampling.temperature;
    params.top_p = request.sampling.top_p;
    params.top_k = request.sampling.top_k.value_or(0);
    params.min_p = request.sampling.min_p;
    params.has_seed = request.sampling.seed ? 1 : 0;
    params.seed = request.sampling.seed.value_or(0);

    lca_generation_stats stats{};
    stats.struct_size = sizeof stats;
    stats.prompt_tokens = -1;
    stats.completion_tokens = -1;
    stats.cached_prompt_tokens = -1;
    stats.prefill_ms = -1;
    stats.decode_ms = -1;

    CallbackContext ctx{&sink, &cancel, nullptr, 0};
    PluginErrorBuffer error;
    const lca_status status = api_->generate(backend_, &params, &lca_endpoint_plugin_sink,
                                             &lca_endpoint_plugin_cancel, &ctx, &stats, error.data(),
                                             PluginErrorBuffer::size());
    if (ctx.error) std::rethrow_exception(ctx.error);

    GenerationOutcome outcome;
    if (status == LCA_STATUS_CANCELLED) {
        outcome.finish = FinishReason::Cancelled;
    } else if (status != LCA_STATUS_OK) {
        raise(status, "generate", error.text());
    } else {
        switch (stats.finish_reason) {
            case LCA_FINISH_LENGTH: outcome.finish = FinishReason::Length; break;
            case LCA_FINISH_STOPPED_BY_SINK: outcome.finish = FinishReason::StoppedBySink; break;
            default: outcome.finish = FinishReason::EndOfSequence;
        }
    }
    if (stats.prompt_tokens >= 0) outcome.stats.prompt_tokens = static_cast<std::uint64_t>(stats.prompt_tokens);
    // A plugin that does not count its output still delivered pieces; that
    // count is a floor, used only when the plugin reports nothing.
    outcome.stats.completion_tokens = stats.completion_tokens >= 0
                                          ? static_cast<std::uint64_t>(stats.completion_tokens)
                                          : ctx.non_empty_pieces;
    if (stats.cached_prompt_tokens >= 0) {
        outcome.stats.cached_prompt_tokens = static_cast<std::uint64_t>(stats.cached_prompt_tokens);
    }
    if (stats.prefill_ms >= 0) outcome.stats.prefill_ms = stats.prefill_ms;
    if (stats.decode_ms >= 0) outcome.stats.decode_ms = stats.decode_ms;
    return outcome;
}

}  // namespace lca
