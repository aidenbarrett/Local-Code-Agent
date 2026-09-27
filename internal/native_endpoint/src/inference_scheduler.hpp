// Single-stream inference scheduling.
//
// One worker thread owns the backend. Requests wait in a bounded FIFO; the
// worker admits them one at a time (token count, context check), streams the
// parsed output back through a per-request channel, and removes the request
// from the active set only after the backend has actually returned. That last
// point is what makes /debug/active-requests usable as cancellation proof:
// a request leaves the set when inference has stopped, not when a socket
// closed.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "inference_backend.hpp"
#include "output_parser.hpp"

namespace lca {

struct AdmittedFacts {
    std::uint64_t prompt_tokens = 0;
    std::uint32_t max_new_tokens = 0;
};

struct FinishedFacts {
    GenerationOutcome outcome;
    std::uint64_t prompt_tokens = 0;  // backend value when reported, else admission count
    bool produced_tool_calls = false;
    bool stop_string_matched = false;
};

struct FailureFacts {
    BackendErrorKind kind = BackendErrorKind::Internal;
    std::string message;
};

struct ChannelEvent {
    enum class Kind { Admitted, Parts, Finished, Failed };
    Kind kind = Kind::Parts;
    AdmittedFacts admitted;
    std::vector<ParsedPart> parts;
    FinishedFacts finished;
    FailureFacts failure;
};

// Worker -> HTTP handler, one per request.
class RequestChannel {
public:
    void push(ChannelEvent event);
    // Waits up to timeout. Consecutive Parts events are merged.
    std::optional<ChannelEvent> pop(std::chrono::milliseconds timeout);

    CancelFlag& cancel() { return cancel_; }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<ChannelEvent> events_;
    CancelFlag cancel_;
};

struct InferenceJob {
    GenerationRequest request;
    std::optional<std::uint32_t> requested_max_tokens;
    std::vector<std::string> stop_strings;
    bool parse_tool_calls = false;
    bool opens_in_reasoning = false;
    std::shared_ptr<RequestChannel> channel;
};

struct ActiveRequest {
    std::string id;
    std::string state;  // "queued" | "running"
    double age_ms = 0;
};

struct LastRequestTelemetry {
    std::string request_id;
    std::string finish_reason;
    std::uint64_t prompt_tokens = 0;
    std::uint64_t completion_tokens = 0;
    std::optional<std::uint64_t> cached_prompt_tokens;
    std::optional<double> backend_prefill_ms;
    std::optional<double> backend_decode_ms;
    double endpoint_first_text_ms = -1;  // admission to first decoded text, endpoint clock
    double endpoint_generate_ms = 0;     // admission to backend return, endpoint clock
};

struct SchedulerCounters {
    std::uint64_t completed = 0;
    std::uint64_t cancelled = 0;
    std::uint64_t failed = 0;
    std::uint64_t rejected_busy = 0;
    std::uint64_t rejected_context = 0;
};

struct SchedulerOptions {
    std::size_t max_queued_requests = 8;
    std::uint32_t default_max_tokens = 2048;
};

class InferenceScheduler {
public:
    InferenceScheduler(InferenceBackend& backend, SchedulerOptions options);
    ~InferenceScheduler();

    InferenceScheduler(const InferenceScheduler&) = delete;
    InferenceScheduler& operator=(const InferenceScheduler&) = delete;

    // False when the queue is full (the caller answers 503).
    bool submit(InferenceJob job);

    // Abandon a request. A queued request is removed immediately; a running
    // one is flagged and leaves the active set when the backend returns.
    void cancel(const std::string& request_id, const std::shared_ptr<RequestChannel>& channel);

    std::vector<ActiveRequest> active_requests() const;
    std::optional<LastRequestTelemetry> last_request() const;
    SchedulerCounters counters() const;

    // Cancels everything and joins the worker.
    void shutdown();

private:
    struct Queued {
        InferenceJob job;
        std::chrono::steady_clock::time_point enqueued;
    };

    void run();
    void execute(InferenceJob& job);

    InferenceBackend& backend_;
    SchedulerOptions options_;

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<Queued> queue_;
    std::optional<std::string> running_id_;
    std::chrono::steady_clock::time_point running_since_;
    std::shared_ptr<RequestChannel> running_channel_;
    bool stopping_ = false;
    std::optional<LastRequestTelemetry> last_;
    SchedulerCounters counters_;
    std::thread worker_;
};

}  // namespace lca
