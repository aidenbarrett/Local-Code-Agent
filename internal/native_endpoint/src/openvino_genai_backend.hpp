// In-process OpenVINO GenAI LLMPipeline backend.
//
// Built only when CMake finds OpenVINOGenAI (LCA_WITH_OPENVINO_GENAI). It is
// the reference for what a direct runtime backend must provide, and the
// baseline any future backend is benchmarked against.
#pragma once

#include <memory>
#include <string>

#include <nlohmann/json.hpp>

#include "inference_backend.hpp"

namespace lca {

struct OpenVinoGenAiConfig {
    std::string model_path;
    std::string device;
    std::string model_id;
    std::uint64_t max_context_tokens = 0;
    // Passed to the LLMPipeline constructor as string-valued properties.
    nlohmann::json properties = nlohmann::json::object();

    static OpenVinoGenAiConfig from_json(const nlohmann::json& config);
};

std::unique_ptr<InferenceBackend> make_openvino_genai_backend(const OpenVinoGenAiConfig& config);

}  // namespace lca
