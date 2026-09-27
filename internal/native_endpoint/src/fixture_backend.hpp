// A deterministic, model-free backend for tests, CI and protocol bring-up.
//
// It is not a model and never claims to be one: its identity says
// runtime "lca-fixture" and no observed device. Replies come from ordered
// substring rules over the formatted prompt; without a matching rule it emits
// filler words until max_new_tokens, which gives cancellation tests a stream
// that only stops when told to.
#pragma once

#include <chrono>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "inference_backend.hpp"

namespace lca {

struct FixtureRule {
    std::string prompt_contains;
    std::string reply;
};

struct FixtureConfig {
    std::string model_id = "lca-fixture";
    std::uint64_t max_context_tokens = 8192;
    std::chrono::milliseconds piece_delay{2};
    double prefill_ms_per_1k_tokens = 0;
    std::chrono::milliseconds load_delay{0};
    std::vector<FixtureRule> rules;
    std::optional<std::string> default_reply;  // nullopt: endless filler

    static FixtureConfig from_json(const nlohmann::json& config);
};

class FixtureBackend final : public InferenceBackend {
public:
    FixtureBackend(FixtureConfig config, std::string endpoint_version);

    const BackendIdentity& identity() const override { return identity_; }
    std::uint64_t count_prompt_tokens(const std::string& prompt) override;
    GenerationOutcome generate(const GenerationRequest& request, const TextSink& sink,
                               const CancelFlag& cancel) override;

    // Split text into the pieces the fixture streams: at most four bytes each,
    // never splitting a UTF-8 character.
    static std::vector<std::string> pieces(const std::string& text);

private:
    FixtureConfig config_;
    BackendIdentity identity_;
};

}  // namespace lca
