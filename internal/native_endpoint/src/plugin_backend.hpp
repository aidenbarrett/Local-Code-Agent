// Adapts a shared library implementing include/lca/backend_plugin.h to the
// endpoint's InferenceBackend interface.
#pragma once

#include <memory>
#include <string>

#include "inference_backend.hpp"
#include "lca/backend_plugin.h"

namespace lca {

class SharedLibrary {
public:
    explicit SharedLibrary(const std::string& path_utf8);
    ~SharedLibrary();
    SharedLibrary(const SharedLibrary&) = delete;
    SharedLibrary& operator=(const SharedLibrary&) = delete;

    void* symbol(const char* name) const;

private:
    void* handle_ = nullptr;
};

class PluginBackend final : public InferenceBackend {
public:
    // Loads the library, checks the ABI version, creates the backend with
    // config_json and reads its description. Throws BackendError on any
    // failure; nothing is half-loaded.
    PluginBackend(const std::string& library_path, const std::string& config_json);
    ~PluginBackend() override;

    const BackendIdentity& identity() const override { return identity_; }
    std::uint64_t count_prompt_tokens(const std::string& prompt) override;
    GenerationOutcome generate(const GenerationRequest& request, const TextSink& sink,
                               const CancelFlag& cancel) override;

private:
    std::unique_ptr<SharedLibrary> library_;
    const lca_backend_api* api_ = nullptr;
    lca_backend* backend_ = nullptr;
    BackendIdentity identity_;
};

}  // namespace lca
