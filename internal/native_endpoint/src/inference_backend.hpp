// The in-process view of an inference backend.
//
// Every backend (the deterministic fixture, a C ABI plugin, the OpenVINO GenAI
// pipeline) is adapted to this one interface. The endpoint calls it from a
// single inference thread only, so implementations need no locking.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

namespace lca {

struct BackendIdentity {
    std::string kind;             // "fixture", "plugin", "openvino-genai"
    std::string runtime_name;
    std::string runtime_version;
    std::string device_requested;
    std::optional<std::string> device_observed;  // only what the runtime reported
    std::string model_id;
    std::optional<std::string> model_path;
    std::uint64_t max_context_tokens = 0;
    std::optional<double> load_ms;
    std::optional<double> compile_ms;  // only when the runtime reports it separately
    std::optional<std::string> plugin_path;
};

struct SamplingParams {
    float temperature = 0.0f;  // <= 0 is greedy
    float top_p = 1.0f;
    std::optional<std::int32_t> top_k;
    float min_p = 0.0f;
    std::optional<std::uint64_t> seed;
};

struct GenerationRequest {
    std::string request_id;
    std::string prompt;
    std::uint32_t max_new_tokens = 0;
    SamplingParams sampling;
};

enum class FinishReason { EndOfSequence, Length, StoppedBySink, Cancelled };

const char* to_string(FinishReason reason);

struct GenerationStats {
    std::optional<std::uint64_t> prompt_tokens;
    std::uint64_t completion_tokens = 0;
    std::optional<std::uint64_t> cached_prompt_tokens;
    std::optional<double> prefill_ms;
    std::optional<double> decode_ms;
};

struct GenerationOutcome {
    FinishReason finish = FinishReason::EndOfSequence;
    GenerationStats stats;
};

enum class SinkAction { Continue, Stop };

using TextSink = std::function<SinkAction(std::string_view piece)>;

class CancelFlag {
public:
    void request() noexcept { requested_.store(true, std::memory_order_release); }
    bool requested() const noexcept { return requested_.load(std::memory_order_acquire); }

private:
    std::atomic<bool> requested_{false};
};

enum class BackendErrorKind { InvalidArgument, ContextExceeded, Unavailable, Internal };

class BackendError : public std::runtime_error {
public:
    BackendError(BackendErrorKind kind, const std::string& message)
        : std::runtime_error(message), kind_(kind) {}
    BackendErrorKind kind() const noexcept { return kind_; }

private:
    BackendErrorKind kind_;
};

class InferenceBackend {
public:
    virtual ~InferenceBackend() = default;

    virtual const BackendIdentity& identity() const = 0;

    // Tokens the formatted prompt occupies, without special tokens added.
    virtual std::uint64_t count_prompt_tokens(const std::string& prompt) = 0;

    // Streams decoded text into sink until end of sequence, max_new_tokens,
    // a Stop from the sink, or cancellation. Returns only once the runtime has
    // actually stopped working on the request. Throws BackendError on failure.
    virtual GenerationOutcome generate(const GenerationRequest& request, const TextSink& sink,
                                       const CancelFlag& cancel) = 0;
};

}  // namespace lca
