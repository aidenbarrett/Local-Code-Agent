// The HTTP surface of lca-endpoint.
//
//   POST /v1/chat/completions   OpenAI chat completions, streaming or unary
//   GET  /v1/models             served model (503 until the backend is loaded)
//        (/v3/... serves the same two routes for profiles addressed under /v3)
//   GET  /health                loading | ready | failed
//   GET  /identity              server build, backend, runtime, device, model, limits
//   GET  /telemetry             backend-reported startup/request timings, RSS, counters
//   GET  /debug/active-requests request ids the backend is still working on
//
// Every chat response carries an x-request-id header naming the id that
// /debug/active-requests reports, which is what lets the endpoint harness
// prove cancellation instead of assuming it.
#pragma once

#include <cstdint>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include "inference_backend.hpp"
#include "inference_scheduler.hpp"

namespace httplib {
class Server;
}

namespace lca {

extern const char* const kEndpointVersion;

struct EndpointOptions {
    std::string host = "127.0.0.1";
    int port = 0;  // 0 picks a free port
    bool allow_remote_bind = false;
    std::optional<std::string> api_key;
    SchedulerOptions scheduler;
    int http_threads = 8;
    bool log_requests = true;
};

using BackendFactory = std::function<std::unique_ptr<InferenceBackend>()>;

class EndpointServer {
public:
    EndpointServer(EndpointOptions options, BackendFactory factory);
    ~EndpointServer();

    EndpointServer(const EndpointServer&) = delete;
    EndpointServer& operator=(const EndpointServer&) = delete;
    EndpointServer(EndpointServer&&) = delete;
    EndpointServer& operator=(EndpointServer&&) = delete;

    // Binds, then starts serving and loading the backend on background
    // threads. Returns the bound port. Throws std::runtime_error when the
    // address is refused or cannot be bound.
    int start();

    // Blocks until the backend is loaded or failed. True when ready.
    bool wait_until_loaded(std::chrono::milliseconds timeout);

    // Blocks until stop() is called or the backend fails to load.
    // Returns false when the backend failed to load.
    bool wait();

    void stop();

    std::optional<std::string> load_error() const;

private:
    enum class Status : std::uint8_t { Loading, Ready, Failed };

    void install_routes();
    void load_backend();

    EndpointOptions options_;
    BackendFactory factory_;
    std::unique_ptr<httplib::Server> http_;
    std::unique_ptr<InferenceBackend> backend_;
    std::unique_ptr<InferenceScheduler> scheduler_;
    std::thread listen_thread_;
    std::thread load_thread_;
    std::atomic<Status> status_{Status::Loading};
    mutable std::mutex state_mutex_;
    std::condition_variable state_cv_;
    std::optional<std::string> load_error_;
    bool stop_requested_ = false;
    std::chrono::steady_clock::time_point started_at_;
    std::optional<double> ready_after_ms_;
    std::string system_fingerprint_;
};

}  // namespace lca
