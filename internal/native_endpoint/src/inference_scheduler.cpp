#include "inference_scheduler.hpp"

#include <algorithm>
#include <exception>

namespace lca {

namespace {

double ms_since(std::chrono::steady_clock::time_point start) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
}

}  // namespace

const char* to_string(FinishReason reason) {
    switch (reason) {
        case FinishReason::EndOfSequence: return "end_of_sequence";
        case FinishReason::Length: return "length";
        case FinishReason::StoppedBySink: return "stopped_by_sink";
        case FinishReason::Cancelled: return "cancelled";
    }
    return "unknown";
}

// ------------------------------------------------------------ channel

void RequestChannel::push(ChannelEvent event) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (event.kind == ChannelEvent::Kind::Parts && event.parts.empty()) return;
        events_.push_back(std::move(event));
    }
    cv_.notify_all();
}

std::optional<ChannelEvent> RequestChannel::pop(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(mutex_);
    if (!cv_.wait_for(lock, timeout, [this] { return !events_.empty(); })) return std::nullopt;
    ChannelEvent event = std::move(events_.front());
    events_.pop_front();
    if (event.kind == ChannelEvent::Kind::Parts) {
        while (!events_.empty() && events_.front().kind == ChannelEvent::Kind::Parts) {
            auto& more = events_.front().parts;
            for (auto& part : more) event.parts.push_back(std::move(part));
            events_.pop_front();
        }
    }
    return event;
}

// ------------------------------------------------------------ scheduler

InferenceScheduler::InferenceScheduler(InferenceBackend& backend, SchedulerOptions options)
    : backend_(backend), options_(options) {
    worker_ = std::thread([this] { run(); });
}

InferenceScheduler::~InferenceScheduler() { shutdown(); }

bool InferenceScheduler::submit(InferenceJob job) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_ || queue_.size() >= options_.max_queued_requests) {
            ++counters_.rejected_busy;
            return false;
        }
        queue_.push_back(Queued{std::move(job), std::chrono::steady_clock::now()});
    }
    cv_.notify_all();
    return true;
}

void InferenceScheduler::cancel(const std::string& request_id,
                                const std::shared_ptr<RequestChannel>& channel) {
    channel->cancel().request();
    bool removed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto it = std::find_if(queue_.begin(), queue_.end(), [&](const Queued& q) {
            return q.job.request.request_id == request_id;
        });
        if (it != queue_.end()) {
            queue_.erase(it);
            ++counters_.cancelled;
            removed = true;
        }
    }
    if (removed) {
        ChannelEvent done;
        done.kind = ChannelEvent::Kind::Finished;
        done.finished.outcome.finish = FinishReason::Cancelled;
        channel->push(std::move(done));
    }
}

std::vector<ActiveRequest> InferenceScheduler::active_requests() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<ActiveRequest> out;
    if (running_id_) out.push_back({*running_id_, "running", ms_since(running_since_)});
    for (const auto& q : queue_) out.push_back({q.job.request.request_id, "queued", ms_since(q.enqueued)});
    return out;
}

std::optional<LastRequestTelemetry> InferenceScheduler::last_request() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return last_;
}

SchedulerCounters InferenceScheduler::counters() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return counters_;
}

void InferenceScheduler::shutdown() {
    std::deque<Queued> abandoned;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_ && !worker_.joinable()) return;
        stopping_ = true;
        abandoned.swap(queue_);
        if (running_channel_) running_channel_->cancel().request();
    }
    cv_.notify_all();
    for (auto& q : abandoned) {
        q.job.channel->cancel().request();
        ChannelEvent failed;
        failed.kind = ChannelEvent::Kind::Failed;
        failed.failure = {BackendErrorKind::Unavailable, "endpoint is shutting down"};
        q.job.channel->push(std::move(failed));
    }
    if (worker_.joinable()) worker_.join();
}

void InferenceScheduler::run() {
    for (;;) {
        InferenceJob job;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
            if (stopping_) return;
            job = std::move(queue_.front().job);
            queue_.pop_front();
            running_id_ = job.request.request_id;
            running_since_ = std::chrono::steady_clock::now();
            running_channel_ = job.channel;
        }
        execute(job);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            running_id_.reset();
            running_channel_.reset();
        }
    }
}

void InferenceScheduler::execute(InferenceJob& job) {
    auto& channel = *job.channel;
    const auto started = std::chrono::steady_clock::now();

    auto fail = [&](BackendErrorKind kind, std::string message) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (kind == BackendErrorKind::ContextExceeded) {
                ++counters_.rejected_context;
            } else {
                ++counters_.failed;
            }
        }
        ChannelEvent failed;
        failed.kind = ChannelEvent::Kind::Failed;
        failed.failure = {kind, std::move(message)};
        channel.push(std::move(failed));
    };

    if (channel.cancel().requested()) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ++counters_.cancelled;
        }
        ChannelEvent done;
        done.kind = ChannelEvent::Kind::Finished;
        done.finished.outcome.finish = FinishReason::Cancelled;
        channel.push(std::move(done));
        return;
    }

    std::uint64_t prompt_tokens = 0;
    try {
        prompt_tokens = backend_.count_prompt_tokens(job.request.prompt);
    } catch (const BackendError& e) {
        fail(e.kind(), e.what());
        return;
    } catch (const std::exception& e) {
        fail(BackendErrorKind::Internal, std::string("token counting failed: ") + e.what());
        return;
    }

    const std::uint64_t context = backend_.identity().max_context_tokens;
    if (context > 0 && prompt_tokens >= context) {
        fail(BackendErrorKind::ContextExceeded,
             "prompt is " + std::to_string(prompt_tokens) + " tokens; the model context limit is " +
                 std::to_string(context) + " tokens");
        return;
    }
    std::uint64_t room = context > 0 ? context - prompt_tokens : UINT32_MAX;
    std::uint64_t wanted = job.requested_max_tokens.value_or(options_.default_max_tokens);
    job.request.max_new_tokens = static_cast<std::uint32_t>(std::min<std::uint64_t>(wanted, room));

    ChannelEvent admitted;
    admitted.kind = ChannelEvent::Kind::Admitted;
    admitted.admitted = {prompt_tokens, job.request.max_new_tokens};
    channel.push(std::move(admitted));

    StopStringFilter stops(job.stop_strings);
    ReasoningToolCallParser parser(job.parse_tool_calls, job.opens_in_reasoning);
    double first_text_ms = -1;

    const TextSink sink = [&](std::string_view piece) -> SinkAction {
        if (first_text_ms < 0 && !piece.empty()) first_text_ms = ms_since(started);
        auto filtered = stops.push(piece);
        ChannelEvent parts;
        parts.kind = ChannelEvent::Kind::Parts;
        parts.parts = parser.push(filtered.released);
        channel.push(std::move(parts));
        if (filtered.stop_matched) return SinkAction::Stop;
        return channel.cancel().requested() ? SinkAction::Stop : SinkAction::Continue;
    };

    GenerationOutcome outcome;
    try {
        outcome = backend_.generate(job.request, sink, channel.cancel());
    } catch (const BackendError& e) {
        fail(e.kind(), e.what());
        return;
    } catch (const std::exception& e) {
        fail(BackendErrorKind::Internal, std::string("generation failed: ") + e.what());
        return;
    }

    // The backend has returned: the request is no longer running.
    ChannelEvent tail;
    tail.kind = ChannelEvent::Kind::Parts;
    if (!stops.stopped()) {
        tail.parts = parser.push(stops.flush());
    }
    for (auto& part : parser.finish()) tail.parts.push_back(std::move(part));
    channel.push(std::move(tail));

    if (channel.cancel().requested()) {
        outcome.finish = FinishReason::Cancelled;
    } else if (stops.stopped()) {
        outcome.finish = FinishReason::StoppedBySink;
    }

    FinishedFacts finished;
    finished.outcome = outcome;
    finished.prompt_tokens = outcome.stats.prompt_tokens.value_or(prompt_tokens);
    finished.produced_tool_calls = parser.tool_calls_emitted() > 0;
    finished.stop_string_matched = stops.stopped();

    {
        std::lock_guard<std::mutex> lock(mutex_);
        LastRequestTelemetry t;
        t.request_id = job.request.request_id;
        t.finish_reason = to_string(outcome.finish);
        t.prompt_tokens = finished.prompt_tokens;
        t.completion_tokens = outcome.stats.completion_tokens;
        t.cached_prompt_tokens = outcome.stats.cached_prompt_tokens;
        t.backend_prefill_ms = outcome.stats.prefill_ms;
        t.backend_decode_ms = outcome.stats.decode_ms;
        t.endpoint_first_text_ms = first_text_ms;
        t.endpoint_generate_ms = ms_since(started);
        last_ = std::move(t);
        if (outcome.finish == FinishReason::Cancelled) {
            ++counters_.cancelled;
        } else {
            ++counters_.completed;
        }
    }

    ChannelEvent done;
    done.kind = ChannelEvent::Kind::Finished;
    done.finished = std::move(finished);
    channel.push(std::move(done));
}

}  // namespace lca
