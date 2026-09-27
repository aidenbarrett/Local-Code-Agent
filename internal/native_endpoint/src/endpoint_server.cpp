#include "endpoint_server.hpp"

#include <httplib.h>

#include <atomic>
#include <ctime>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>

#include "chat_format.hpp"
#include "lca/backend_plugin.h"
#include "openai_protocol.hpp"
#include "process_memory.hpp"

#ifndef LCA_ENDPOINT_VERSION
#define LCA_ENDPOINT_VERSION "0.1.0"
#endif
#ifndef LCA_BUILD_TYPE
#define LCA_BUILD_TYPE "unknown"
#endif

namespace lca {

const char* const kEndpointVersion = LCA_ENDPOINT_VERSION;

namespace {

constexpr auto kPoll = std::chrono::milliseconds(25);

std::string compiler_identity() {
#if defined(__clang__)
    return "clang " __clang_version__;
#elif defined(__GNUC__)
    return "gcc " __VERSION__;
#elif defined(_MSC_VER)
    return "msvc " + std::to_string(_MSC_FULL_VER);
#else
    return "unknown";
#endif
}

bool is_loopback(const std::string& host) {
    return host == "127.0.0.1" || host == "::1" || host == "localhost";
}

std::string new_request_id() {
    static std::atomic<std::uint64_t> counter{0};
    static const std::uint64_t salt = [] {
        std::random_device rd;
        return (static_cast<std::uint64_t>(rd()) << 32) ^ rd();
    }();
    std::mt19937_64 mix(salt ^ (counter.fetch_add(1) * 0x9E3779B97F4A7C15ull));
    std::ostringstream id;
    id << "req-" << std::hex;
    id.width(16);
    id.fill('0');
    id << mix();
    return id.str();
}

std::mutex& log_mutex() {
    static std::mutex m;
    return m;
}

void log_line(const ordered_json& record) {
    std::lock_guard<std::mutex> lock(log_mutex());
    std::cerr << to_wire(record) << std::endl;
}

void send_json(httplib::Response& res, int status, const ordered_json& body) {
    res.status = status;
    res.set_content(to_wire(body), "application/json");
}

void send_error(httplib::Response& res, const ProtocolError& error) {
    if (error.http_status == 503) res.set_header("Retry-After", "1");
    send_json(res, error.http_status, error_body(error));
}

ProtocolError failure_to_error(const FailureFacts& failure) {
    ProtocolError e;
    e.message = failure.message;
    switch (failure.kind) {
        case BackendErrorKind::ContextExceeded:
            e.http_status = 400;
            e.code = "context_length_exceeded";
            e.param = "messages";
            break;
        case BackendErrorKind::InvalidArgument:
            e.http_status = 400;
            e.code = "invalid_value";
            break;
        case BackendErrorKind::Unavailable:
            e.http_status = 503;
            e.type = "server_error";
            e.code = "backend_unavailable";
            break;
        case BackendErrorKind::Internal:
            e.http_status = 500;
            e.type = "server_error";
            e.code = "backend_error";
            break;
    }
    return e;
}

ordered_json optional_number(const std::optional<double>& v) { return v ? ordered_json(*v) : ordered_json(); }
ordered_json optional_count(const std::optional<std::uint64_t>& v) {
    return v ? ordered_json(*v) : ordered_json();
}
ordered_json optional_text(const std::optional<std::string>& v) { return v ? ordered_json(*v) : ordered_json(); }

// Everything a streamed response needs between provider calls.
struct StreamSession {
    std::string request_id;
    std::shared_ptr<RequestChannel> channel;
    InferenceScheduler* scheduler = nullptr;
    ResponseContext ctx;
    bool include_usage = false;
    bool role_sent = false;
    bool complete = false;
    std::size_t tool_index = 0;
    std::uint64_t admitted_prompt_tokens = 0;
    std::chrono::steady_clock::time_point started;
    bool log = true;
};

}  // namespace

EndpointServer::EndpointServer(EndpointOptions options, BackendFactory factory)
    : options_(std::move(options)), factory_(std::move(factory)) {}

EndpointServer::~EndpointServer() { stop(); }

std::optional<std::string> EndpointServer::load_error() const {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return load_error_;
}

int EndpointServer::start() {
    if (!is_loopback(options_.host) && !options_.allow_remote_bind) {
        throw std::runtime_error("refusing to bind " + options_.host +
                                 ": only loopback addresses are allowed without --allow-remote-bind");
    }
    started_at_ = std::chrono::steady_clock::now();
    http_ = std::make_unique<httplib::Server>();
    const int threads = options_.http_threads > 0 ? options_.http_threads : 8;
    http_->new_task_queue = [threads] { return new httplib::ThreadPool(static_cast<std::size_t>(threads)); };
    http_->set_read_timeout(std::chrono::seconds(30));
    http_->set_write_timeout(std::chrono::seconds(30));
    http_->set_payload_max_length(64u * 1024u * 1024u);
    install_routes();

    int port = options_.port;
    if (port == 0) {
        port = http_->bind_to_any_port(options_.host);
        if (port <= 0) throw std::runtime_error("could not bind any port on " + options_.host);
    } else if (!http_->bind_to_port(options_.host, port)) {
        throw std::runtime_error("could not bind " + options_.host + ":" + std::to_string(port));
    }
    options_.port = port;

    listen_thread_ = std::thread([this] { http_->listen_after_bind(); });
    load_thread_ = std::thread([this] { load_backend(); });
    return port;
}

void EndpointServer::load_backend() {
    try {
        auto backend = factory_();
        if (!backend) throw std::runtime_error("backend factory returned nothing");
        const auto& id = backend->identity();
        if (id.max_context_tokens == 0) throw std::runtime_error("backend reported no context limit");
        system_fingerprint_ = std::string("lca-endpoint-") + kEndpointVersion + "/" + id.runtime_name + "-" +
                              id.runtime_version + "/" + id.device_requested;
        auto scheduler = std::make_unique<InferenceScheduler>(*backend, options_.scheduler);
        std::lock_guard<std::mutex> lock(state_mutex_);
        if (stop_requested_) {
            scheduler.reset();
            return;
        }
        backend_ = std::move(backend);
        scheduler_ = std::move(scheduler);
        ready_after_ms_ =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started_at_).count();
        status_ = Status::Ready;
        if (options_.log_requests) {
            log_line({{"event", "ready"},
                      {"model", backend_->identity().model_id},
                      {"runtime", backend_->identity().runtime_name},
                      {"device", backend_->identity().device_requested},
                      {"port", options_.port},
                      {"ready_after_ms", *ready_after_ms_}});
        }
    } catch (const std::exception& e) {
        std::lock_guard<std::mutex> lock(state_mutex_);
        load_error_ = e.what();
        status_ = Status::Failed;
        if (options_.log_requests) log_line({{"event", "load_failed"}, {"error", e.what()}});
    }
    state_cv_.notify_all();
}

bool EndpointServer::wait_until_loaded(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(state_mutex_);
    state_cv_.wait_for(lock, timeout, [this] { return status_ != Status::Loading; });
    return status_ == Status::Ready;
}

bool EndpointServer::wait() {
    std::unique_lock<std::mutex> lock(state_mutex_);
    state_cv_.wait(lock, [this] { return stop_requested_ || status_ == Status::Failed; });
    return status_ != Status::Failed;
}

void EndpointServer::stop() {
    {
        std::lock_guard<std::mutex> lock(state_mutex_);
        stop_requested_ = true;
    }
    state_cv_.notify_all();
    if (load_thread_.joinable()) load_thread_.join();
    if (scheduler_) scheduler_->shutdown();
    if (http_) http_->stop();
    if (listen_thread_.joinable()) listen_thread_.join();
    scheduler_.reset();
    backend_.reset();
}

void EndpointServer::install_routes() {
    auto& http = *http_;

    // Authentication for everything except /health.
    http.set_pre_routing_handler([this](const httplib::Request& req, httplib::Response& res) {
        if (!options_.api_key || req.path == "/health") return httplib::Server::HandlerResponse::Unhandled;
        if (req.get_header_value("Authorization") == "Bearer " + *options_.api_key) {
            return httplib::Server::HandlerResponse::Unhandled;
        }
        ProtocolError e;
        e.http_status = 401;
        e.code = "invalid_api_key";
        e.message = "missing or invalid bearer token";
        send_error(res, e);
        return httplib::Server::HandlerResponse::Handled;
    });

    auto not_ready = [this](httplib::Response& res) {
        ProtocolError e;
        e.http_status = 503;
        e.type = "server_error";
        const auto status = status_.load();
        e.code = status == Status::Failed ? "backend_failed" : "backend_loading";
        e.message = status == Status::Failed ? "backend failed to load: " + load_error().value_or("")
                                             : "backend is still loading";
        send_error(res, e);
    };

    http.Get("/health", [this](const httplib::Request&, httplib::Response& res) {
        const auto status = status_.load();
        ordered_json body;
        body["status"] = status == Status::Ready ? "ready" : status == Status::Failed ? "failed" : "loading";
        if (status == Status::Failed) body["error"] = load_error().value_or("");
        send_json(res, status == Status::Ready ? 200 : 503, body);
    });

    // /v3 mirrors /v1 so the endpoint can stand in for a server the product
    // profiles already address under /v3 without changing their base_url.
    const auto models = [this, not_ready](const httplib::Request&, httplib::Response& res) {
        if (status_ != Status::Ready) return not_ready(res);
        ordered_json model;
        model["id"] = backend_->identity().model_id;
        model["object"] = "model";
        model["created"] = 0;
        model["owned_by"] = "lca-endpoint";
        send_json(res, 200, {{"object", "list"}, {"data", ordered_json::array({model})}});
    };
    http.Get("/v1/models", models);
    http.Get("/v3/models", models);

    http.Get("/identity", [this](const httplib::Request&, httplib::Response& res) {
        ordered_json body;
        ordered_json build;
        build["compiler"] = compiler_identity();
        build["build_type"] = LCA_BUILD_TYPE;
        body["server"] = {{"name", "lca-endpoint"},
                          {"version", kEndpointVersion},
                          {"backend_plugin_abi_version", LCA_BACKEND_ABI_VERSION},
                          {"build", build}};
        const auto status = status_.load();
        body["status"] = status == Status::Ready ? "ready" : status == Status::Failed ? "failed" : "loading";
        body["chat_format"] = "chatml-hermes";
        if (status == Status::Ready) {
            const auto& id = backend_->identity();
            body["backend"] = {{"kind", id.kind}, {"plugin_path", optional_text(id.plugin_path)}};
            body["runtime"] = {{"name", id.runtime_name}, {"version", id.runtime_version}};
            body["device"] = {{"requested", id.device_requested}, {"observed", optional_text(id.device_observed)}};
            body["model"] = {{"id", id.model_id}, {"path", optional_text(id.model_path)}};
            body["limits"] = {{"max_context_tokens", id.max_context_tokens},
                              {"max_queued_requests", options_.scheduler.max_queued_requests},
                              {"default_max_tokens", options_.scheduler.default_max_tokens}};
            body["system_fingerprint"] = system_fingerprint_;
        }
        send_json(res, 200, body);
    });

    http.Get("/telemetry", [this](const httplib::Request&, httplib::Response& res) {
        ordered_json body;
        const auto memory = read_process_memory();
        body["process"] = {{"rss_bytes", optional_count(memory.rss_bytes)},
                           {"peak_rss_bytes", optional_count(memory.peak_rss_bytes)}};
        body["uptime_s"] =
            std::chrono::duration<double>(std::chrono::steady_clock::now() - started_at_).count();
        if (status_ != Status::Ready) {
            body["startup"] = nullptr;
            body["last_request"] = nullptr;
            send_json(res, 200, body);
            return;
        }
        const auto& id = backend_->identity();
        body["startup"] = {{"backend_load_ms", optional_number(id.load_ms)},
                           {"backend_compile_ms", optional_number(id.compile_ms)},
                           {"endpoint_ready_after_ms", optional_number(ready_after_ms_)}};
        const auto last = scheduler_->last_request();
        if (last) {
            body["last_request"] = {
                {"request_id", last->request_id},
                {"finish_reason", last->finish_reason},
                {"prompt_tokens", last->prompt_tokens},
                {"completion_tokens", last->completion_tokens},
                {"cached_prompt_tokens", optional_count(last->cached_prompt_tokens)},
                {"backend_prefill_ms", optional_number(last->backend_prefill_ms)},
                {"backend_decode_ms", optional_number(last->backend_decode_ms)},
                {"endpoint_first_text_ms",
                 last->endpoint_first_text_ms >= 0 ? ordered_json(last->endpoint_first_text_ms) : ordered_json()},
                {"endpoint_generate_ms", last->endpoint_generate_ms}};
        } else {
            body["last_request"] = nullptr;
        }
        const auto c = scheduler_->counters();
        body["counters"] = {{"completed", c.completed},
                            {"cancelled", c.cancelled},
                            {"failed", c.failed},
                            {"rejected_busy", c.rejected_busy},
                            {"rejected_context", c.rejected_context}};
        send_json(res, 200, body);
    });

    http.Get("/debug/active-requests", [this](const httplib::Request&, httplib::Response& res) {
        ordered_json ids = ordered_json::array();
        ordered_json detail = ordered_json::array();
        if (status_ == Status::Ready) {
            for (const auto& a : scheduler_->active_requests()) {
                ids.push_back(a.id);
                detail.push_back({{"id", a.id}, {"state", a.state}, {"age_ms", a.age_ms}});
            }
        }
        send_json(res, 200, {{"active_request_ids", ids}, {"requests", detail}});
    });

    const auto chat_completions = [this, not_ready](const httplib::Request& req, httplib::Response& res) {
        if (status_ != Status::Ready) return not_ready(res);
        const auto started = std::chrono::steady_clock::now();

        ChatCompletionRequest parsed;
        try {
            parsed = parse_chat_completion_request(req.body);
        } catch (const ProtocolException& e) {
            return send_error(res, e.error());
        }
        const auto& identity = backend_->identity();
        if (parsed.model != identity.model_id) {
            ProtocolError e;
            e.http_status = 404;
            e.code = "model_not_found";
            e.param = "model";
            e.message = "model '" + parsed.model + "' is not served here; this endpoint serves '" +
                        identity.model_id + "'";
            return send_error(res, e);
        }

        const auto prompt = render_chatml_hermes_prompt(parsed.messages, parsed.tools, parsed.enable_thinking);
        const std::string request_id = new_request_id();
        auto channel = std::make_shared<RequestChannel>();

        InferenceJob job;
        job.request.request_id = request_id;
        job.request.prompt = prompt.text;
        job.request.sampling = parsed.sampling;
        job.requested_max_tokens = parsed.max_tokens;
        job.stop_strings = parsed.stop;
        job.parse_tool_calls = parsed.tools_enabled;
        job.opens_in_reasoning = prompt.opens_in_reasoning;
        job.channel = channel;
        if (!scheduler_->submit(std::move(job))) {
            ProtocolError e;
            e.http_status = 503;
            e.type = "server_error";
            e.code = "endpoint_busy";
            e.message = "the request queue is full";
            return send_error(res, e);
        }
        res.set_header("x-request-id", request_id);

        // Wait for admission: tokenization and the context check happen on
        // the inference thread, so a too-long prompt is still a clean 400.
        std::uint64_t admitted_prompt_tokens = 0;
        for (;;) {
            auto event = channel->pop(kPoll);
            if (!event) {
                if (req.is_connection_closed()) {
                    scheduler_->cancel(request_id, channel);
                    return;
                }
                continue;
            }
            if (event->kind == ChannelEvent::Kind::Admitted) {
                admitted_prompt_tokens = event->admitted.prompt_tokens;
                break;
            }
            if (event->kind == ChannelEvent::Kind::Failed) {
                return send_error(res, failure_to_error(event->failure));
            }
            if (event->kind == ChannelEvent::Kind::Finished) {
                ProtocolError e;
                e.http_status = 503;
                e.type = "server_error";
                e.code = "request_cancelled";
                e.message = "the request was cancelled before it started";
                return send_error(res, e);
            }
        }

        ResponseContext ctx;
        ctx.completion_id = "chatcmpl-" + request_id;
        ctx.model = identity.model_id;
        ctx.system_fingerprint = system_fingerprint_;
        ctx.created = static_cast<std::int64_t>(std::time(nullptr));
        const bool log = options_.log_requests;

        if (parsed.stream) {
            auto session = std::make_shared<StreamSession>();
            session->request_id = request_id;
            session->channel = channel;
            session->scheduler = scheduler_.get();
            session->ctx = ctx;
            session->include_usage = parsed.include_usage;
            session->admitted_prompt_tokens = admitted_prompt_tokens;
            session->started = started;
            session->log = log;
            res.set_header("Cache-Control", "no-cache");
            res.set_header("X-Accel-Buffering", "no");
            res.set_chunked_content_provider(
                "text/event-stream",
                [session](std::size_t, httplib::DataSink& sink) -> bool {
                    auto& s = *session;
                    auto write = [&](const std::string& text) -> bool {
                        if (sink.write(text.data(), text.size())) return true;
                        s.scheduler->cancel(s.request_id, s.channel);
                        return false;
                    };
                    if (!s.role_sent) {
                        s.role_sent = true;
                        if (!write(sse_event(chunk_role(s.ctx)))) return false;
                    }
                    auto event = s.channel->pop(kPoll);
                    if (!event) {
                        // No output yet (for example a long prefill): notice a
                        // client that has gone away without waiting for a write.
                        if (!sink.is_writable()) {
                            s.scheduler->cancel(s.request_id, s.channel);
                            return false;
                        }
                        return true;
                    }
                    if (event->kind == ChannelEvent::Kind::Parts) {
                        for (const auto& part : event->parts) {
                            std::string text;
                            if (part.kind == ParsedPart::Kind::Content) {
                                text = sse_event(chunk_content(s.ctx, part.text));
                            } else if (part.kind == ParsedPart::Kind::Reasoning) {
                                text = sse_event(chunk_reasoning(s.ctx, part.text));
                            } else {
                                ResponseToolCall call{"call_" + s.request_id.substr(4) + "_" +
                                                          std::to_string(s.tool_index),
                                                      part.call};
                                text = sse_event(chunk_tool_call(s.ctx, s.tool_index++, call));
                            }
                            if (!write(text)) return false;
                        }
                        return true;
                    }
                    if (event->kind == ChannelEvent::Kind::Failed ||
                        (event->kind == ChannelEvent::Kind::Finished &&
                         event->finished.outcome.finish == FinishReason::Cancelled)) {
                        // Abort without [DONE]: the client must see an
                        // incomplete stream, not a short answer.
                        ProtocolError e = event->kind == ChannelEvent::Kind::Failed
                                              ? failure_to_error(event->failure)
                                              : ProtocolError{503, "server_error", "request_cancelled",
                                                              "the request was cancelled", std::nullopt};
                        const std::string error_event = sse_event(error_body(e));
                        sink.write(error_event.data(), error_event.size());
                        if (s.log) {
                            log_line({{"event", "request"}, {"id", s.request_id}, {"stream", true},
                                      {"outcome", event->kind == ChannelEvent::Kind::Failed ? "failed" : "cancelled"},
                                      {"error", e.message}});
                        }
                        s.complete = true;
                        return false;
                    }
                    // Finished normally.
                    const auto& f = event->finished;
                    const std::string finish = openai_finish_reason(f.outcome.finish, f.produced_tool_calls);
                    if (!write(sse_event(chunk_finish(s.ctx, finish)))) return false;
                    UsageFacts usage{f.prompt_tokens, f.outcome.stats.completion_tokens,
                                     f.outcome.stats.cached_prompt_tokens};
                    TimingFacts timings{f.outcome.stats.prefill_ms, f.outcome.stats.decode_ms,
                                        f.outcome.stats.cached_prompt_tokens};
                    if (s.include_usage && !write(sse_event(chunk_usage(s.ctx, usage, timings)))) return false;
                    if (!write("data: [DONE]\n\n")) return false;
                    s.complete = true;
                    sink.done();
                    if (s.log) {
                        log_line({{"event", "request"}, {"id", s.request_id}, {"stream", true},
                                  {"outcome", "completed"}, {"finish_reason", finish},
                                  {"prompt_tokens", usage.prompt_tokens},
                                  {"completion_tokens", usage.completion_tokens},
                                  {"endpoint_ms", std::chrono::duration<double, std::milli>(
                                                      std::chrono::steady_clock::now() - s.started)
                                                      .count()}});
                    }
                    return true;
                },
                [session](bool) {
                    // However the response ended, a request that did not
                    // complete must not keep running.
                    if (!session->complete) session->scheduler->cancel(session->request_id, session->channel);
                });
            return;
        }

        // Unary response.
        std::string content;
        std::string reasoning;
        std::vector<ResponseToolCall> calls;
        for (;;) {
            auto event = channel->pop(kPoll);
            if (!event) {
                if (req.is_connection_closed()) {
                    scheduler_->cancel(request_id, channel);
                    return;
                }
                continue;
            }
            if (event->kind == ChannelEvent::Kind::Parts) {
                for (auto& part : event->parts) {
                    if (part.kind == ParsedPart::Kind::Content) {
                        content += part.text;
                    } else if (part.kind == ParsedPart::Kind::Reasoning) {
                        reasoning += part.text;
                    } else {
                        calls.push_back({"call_" + request_id.substr(4) + "_" + std::to_string(calls.size()),
                                         std::move(part.call)});
                    }
                }
                continue;
            }
            if (event->kind == ChannelEvent::Kind::Failed) {
                return send_error(res, failure_to_error(event->failure));
            }
            if (event->kind == ChannelEvent::Kind::Finished) {
                const auto& f = event->finished;
                if (f.outcome.finish == FinishReason::Cancelled) {
                    ProtocolError e{503, "server_error", "request_cancelled", "the request was cancelled",
                                    std::nullopt};
                    return send_error(res, e);
                }
                const std::string finish = openai_finish_reason(f.outcome.finish, f.produced_tool_calls);
                UsageFacts usage{f.prompt_tokens, f.outcome.stats.completion_tokens,
                                 f.outcome.stats.cached_prompt_tokens};
                TimingFacts timings{f.outcome.stats.prefill_ms, f.outcome.stats.decode_ms,
                                    f.outcome.stats.cached_prompt_tokens};
                send_json(res, 200, completion_response(ctx, content, reasoning, calls, finish, usage, timings));
                if (log) {
                    log_line({{"event", "request"}, {"id", request_id}, {"stream", false},
                              {"outcome", "completed"}, {"finish_reason", finish},
                              {"prompt_tokens", usage.prompt_tokens},
                              {"completion_tokens", usage.completion_tokens},
                              {"endpoint_ms",
                               std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started)
                                   .count()}});
                }
                return;
            }
        }
    };
    http.Post("/v1/chat/completions", chat_completions);
    http.Post("/v3/chat/completions", chat_completions);
}

}  // namespace lca
