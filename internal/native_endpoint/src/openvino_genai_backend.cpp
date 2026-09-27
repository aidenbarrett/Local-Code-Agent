#include "openvino_genai_backend.hpp"
#include "utf8_path.hpp"

#include <chrono>
#include <exception>
#include <filesystem>
#include <functional>
#include <limits>

#include <openvino/genai/llm_pipeline.hpp>
#include <openvino/genai/text_streamer.hpp>
#include <openvino/genai/version.hpp>
#include <openvino/runtime/core.hpp>

namespace lca {

namespace {

bool is_explicit_device(const std::string& device) {
    // AUTO, HETERO, MULTI and BATCH choose or split devices at run time, so the
    // requested name does not identify where inference ran.
    return device.find(':') == std::string::npos && device.rfind("AUTO", 0) != 0 &&
           device.rfind("HETERO", 0) != 0 && device.rfind("MULTI", 0) != 0 &&
           device.rfind("BATCH", 0) != 0;
}

std::string scalar_text(const nlohmann::json& value) {
    if (value.is_string()) return value.get<std::string>();
    if (value.is_boolean()) return value.get<bool>() ? "YES" : "NO";
    return value.dump();
}

class OpenVinoGenAiBackend final : public InferenceBackend {
public:
    explicit OpenVinoGenAiBackend(const OpenVinoGenAiConfig& config) {
        ov::AnyMap properties;
        for (auto it = config.properties.begin(); it != config.properties.end(); ++it) {
            properties[it.key()] = scalar_text(it.value());
        }

        const auto started = std::chrono::steady_clock::now();
        try {
            pipeline_ = std::make_unique<ov::genai::LLMPipeline>(
                path_from_utf8(config.model_path), config.device, properties);
        } catch (const std::exception& e) {
            throw BackendError(BackendErrorKind::Unavailable,
                               "OpenVINO GenAI could not load " + config.model_path + " on " +
                                   config.device + ": " + e.what());
        }
        identity_.load_ms =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
        tokenizer_ = std::make_unique<ov::genai::Tokenizer>(pipeline_->get_tokenizer());

        identity_.kind = "openvino-genai";
        identity_.runtime_name = "openvino-genai";
        identity_.runtime_version = std::string(ov::genai::get_version().buildNumber) +
                                    " (openvino " + ov::get_openvino_version().buildNumber + ")";
        identity_.device_requested = config.device;
        if (is_explicit_device(config.device)) {
            try {
                ov::Core core;
                identity_.device_observed =
                    config.device + ": " + core.get_property(config.device, ov::device::full_name);
            } catch (const std::exception&) {
                identity_.device_observed.reset();
            }
        }
        identity_.model_id = config.model_id;
        identity_.model_path = config.model_path;
        identity_.max_context_tokens = config.max_context_tokens;
        // LLMPipeline loads and compiles in one call; the split is not
        // observable here, so compile time stays unreported.
        identity_.compile_ms.reset();
    }

    const BackendIdentity& identity() const override { return identity_; }

    std::uint64_t count_prompt_tokens(const std::string& prompt) override {
        try {
            const auto encoded = tokenizer_->encode(prompt, ov::AnyMap{{"add_special_tokens", false}});
            const auto shape = encoded.input_ids.get_shape();
            return shape.size() >= 2 ? static_cast<std::uint64_t>(shape[1]) : 0;
        } catch (const std::exception& e) {
            throw BackendError(BackendErrorKind::Internal, std::string("tokenizer failed: ") + e.what());
        }
    }

    GenerationOutcome generate(const GenerationRequest& request, const TextSink& sink,
                               const CancelFlag& cancel) override {
        ov::genai::GenerationConfig config = pipeline_->get_generation_config();
        config.max_new_tokens = request.max_new_tokens;
        config.apply_chat_template = false;  // the endpoint already formatted the prompt
        config.num_return_sequences = 1;
        if (request.sampling.temperature <= 0.0f) {
            config.do_sample = false;
        } else {
            config.do_sample = true;
            config.temperature = request.sampling.temperature;
            config.top_p = request.sampling.top_p;
            config.top_k = request.sampling.top_k ? static_cast<std::size_t>(*request.sampling.top_k)
                                                  : std::numeric_limits<std::size_t>::max();
            config.min_p = request.sampling.min_p;
            if (request.sampling.seed) config.rng_seed = static_cast<std::size_t>(*request.sampling.seed);
        }

        bool sink_stopped = false;
        std::exception_ptr sink_error;
        std::function<ov::genai::CallbackTypeVariant(std::string)> on_text =
            [&](std::string piece) -> ov::genai::CallbackTypeVariant {
            if (cancel.requested()) return ov::genai::StreamingStatus::CANCEL;
            try {
                if (sink(piece) == SinkAction::Stop) {
                    sink_stopped = true;
                    return cancel.requested() ? ov::genai::StreamingStatus::CANCEL
                                              : ov::genai::StreamingStatus::STOP;
                }
            } catch (...) {
                sink_error = std::current_exception();
                return ov::genai::StreamingStatus::CANCEL;
            }
            return ov::genai::StreamingStatus::RUNNING;
        };

        // Keep special tokens in the text: <tool_call> and <think> are tokens in
        // the Qwen3 vocabulary and the endpoint's parser needs to see them. The
        // chat template's end-of-turn markers are stop strings at the endpoint.
        auto streamer = std::make_shared<ov::genai::TextStreamer>(
            *tokenizer_, on_text, ov::AnyMap{{"skip_special_tokens", false}});

        ov::genai::DecodedResults results;
        try {
            results = pipeline_->generate(request.prompt, config,
                                          ov::genai::StreamerVariant{
                                              std::static_pointer_cast<ov::genai::StreamerBase>(streamer)});
        } catch (const std::exception& e) {
            if (sink_error) std::rethrow_exception(sink_error);
            throw BackendError(BackendErrorKind::Internal, std::string("generation failed: ") + e.what());
        }
        if (sink_error) std::rethrow_exception(sink_error);

        GenerationOutcome outcome;
        auto& metrics = results.perf_metrics;
        const auto generated = static_cast<std::uint64_t>(metrics.get_num_generated_tokens());
        outcome.stats.prompt_tokens = static_cast<std::uint64_t>(metrics.get_num_input_tokens());
        outcome.stats.completion_tokens = generated;
        // GenAI reports time to first token; that is prefill plus one decode
        // step, and is labelled as backend-reported by the endpoint.
        outcome.stats.prefill_ms = static_cast<double>(metrics.get_ttft().mean);
        if (generated > 1) {
            outcome.stats.decode_ms =
                static_cast<double>(metrics.get_tpot().mean) * static_cast<double>(generated - 1);
        }
        if (cancel.requested()) {
            outcome.finish = FinishReason::Cancelled;
        } else if (sink_stopped) {
            outcome.finish = FinishReason::StoppedBySink;
        } else if (generated >= request.max_new_tokens) {
            outcome.finish = FinishReason::Length;
        } else {
            outcome.finish = FinishReason::EndOfSequence;
        }
        return outcome;
    }

private:
    std::unique_ptr<ov::genai::LLMPipeline> pipeline_;
    std::unique_ptr<ov::genai::Tokenizer> tokenizer_;
    BackendIdentity identity_;
};

}  // namespace

OpenVinoGenAiConfig OpenVinoGenAiConfig::from_json(const nlohmann::json& config) {
    if (!config.is_object()) {
        throw BackendError(BackendErrorKind::InvalidArgument, "openvino-genai config must be a JSON object");
    }
    OpenVinoGenAiConfig out;
    try {
        out.model_path = config.at("model_path").get<std::string>();
        out.device = config.at("device").get<std::string>();
        out.model_id = config.at("model_id").get<std::string>();
        out.max_context_tokens = config.at("max_context_tokens").get<std::uint64_t>();
    } catch (const std::exception& e) {
        throw BackendError(BackendErrorKind::InvalidArgument,
                           std::string("openvino-genai config needs model_path, device, model_id and "
                                       "max_context_tokens: ") +
                               e.what());
    }
    if (out.max_context_tokens == 0) {
        throw BackendError(BackendErrorKind::InvalidArgument, "max_context_tokens must be positive");
    }
    if (config.contains("properties")) {
        if (!config["properties"].is_object()) {
            throw BackendError(BackendErrorKind::InvalidArgument, "properties must be an object");
        }
        out.properties = config["properties"];
    }
    return out;
}

std::unique_ptr<InferenceBackend> make_openvino_genai_backend(const OpenVinoGenAiConfig& config) {
    return std::make_unique<OpenVinoGenAiBackend>(config);
}

}  // namespace lca
