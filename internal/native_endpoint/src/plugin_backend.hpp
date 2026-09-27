// Adapts a shared library implementing include/lca/backend_plugin.h to the
// endpoint's InferenceBackend interface.
#pragma once

#include <array>
#include <cstddef>
#include <memory>
#include <string>

#include "inference_backend.hpp"
#include "lca/backend_plugin.h"

namespace lca {

// Text a plugin wrote into a caller-owned buffer. The plugin is not trusted to
// NUL-terminate it, so it is read back bounded by the buffer size.
[[nodiscard]] std::string bounded_c_string(const char* data, std::size_t size);

// The error buffer handed to every plugin call that can fail.
class PluginErrorBuffer {
public:
    [[nodiscard]] char* data() noexcept { return text_.data(); }
    [[nodiscard]] static constexpr std::size_t size() noexcept { return kCapacity; }
    [[nodiscard]] std::string text() const { return bounded_c_string(text_.data(), text_.size()); }

private:
    static constexpr std::size_t kCapacity = 1024;
    std::array<char, kCapacity> text_{};
};

class SharedLibrary {
public:
    explicit SharedLibrary(const std::string& path_utf8);
    ~SharedLibrary();
    SharedLibrary(const SharedLibrary&) = delete;
    SharedLibrary& operator=(const SharedLibrary&) = delete;
    SharedLibrary(SharedLibrary&&) = delete;
    SharedLibrary& operator=(SharedLibrary&&) = delete;

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
    PluginBackend(const PluginBackend&) = delete;
    PluginBackend& operator=(const PluginBackend&) = delete;
    PluginBackend(PluginBackend&&) = delete;
    PluginBackend& operator=(PluginBackend&&) = delete;

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
