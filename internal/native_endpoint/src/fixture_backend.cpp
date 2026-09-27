#include "fixture_backend.hpp"

#include <algorithm>
#include <array>
#include <string_view>
#include <thread>

namespace lca {

namespace {

bool sleep_unless_cancelled(std::chrono::milliseconds total, const CancelFlag& cancel) {
    const auto deadline = std::chrono::steady_clock::now() + total;
    while (std::chrono::steady_clock::now() < deadline) {
        if (cancel.requested()) return false;
        const auto left = deadline - std::chrono::steady_clock::now();
        std::this_thread::sleep_for(std::min<std::chrono::steady_clock::duration>(
            left, std::chrono::milliseconds(5)));
    }
    return !cancel.requested();
}

std::size_t utf8_length(unsigned char lead) {
    if (lead < 0x80) return 1;
    if ((lead >> 5) == 0x6) return 2;
    if ((lead >> 4) == 0xE) return 3;
    if ((lead >> 3) == 0x1E) return 4;
    return 1;  // invalid lead byte: pass through alone
}

}  // namespace

FixtureConfig FixtureConfig::from_json(const nlohmann::json& config) {
    FixtureConfig out;
    if (config.is_null()) return out;
    if (!config.is_object()) throw BackendError(BackendErrorKind::InvalidArgument,
                                                "fixture config must be a JSON object");
    out.model_id = config.value("model_id", out.model_id);
    out.max_context_tokens = config.value("max_context_tokens", out.max_context_tokens);
    out.piece_delay = std::chrono::milliseconds(config.value("piece_delay_ms", 2));
    out.prefill_ms_per_1k_tokens = config.value("prefill_ms_per_1k_tokens", 0.0);
    out.load_delay = std::chrono::milliseconds(config.value("load_delay_ms", 0));
    if (config.contains("rules")) {
        for (const auto& rule : config.at("rules")) {
            out.rules.push_back({rule.at("prompt_contains").get<std::string>(),
                                 rule.at("reply").get<std::string>()});
        }
    }
    if (config.contains("default_reply") && config["default_reply"].is_string()) {
        out.default_reply = config["default_reply"].get<std::string>();
    }
    return out;
}

FixtureBackend::FixtureBackend(FixtureConfig config, std::string endpoint_version)
    : config_(std::move(config)) {
    const auto started = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(config_.load_delay);
    identity_.kind = "fixture";
    identity_.runtime_name = "lca-fixture";
    identity_.runtime_version = std::move(endpoint_version);
    identity_.device_requested = "none";
    identity_.model_id = config_.model_id;
    identity_.max_context_tokens = config_.max_context_tokens;
    identity_.load_ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
}

std::uint64_t FixtureBackend::count_prompt_tokens(const std::string& prompt) {
    // Whitespace-separated words: deterministic and close enough to a real
    // tokenizer for context-limit tests. Not a claim about any model.
    std::uint64_t words = 0;
    bool in_word = false;
    for (char c : prompt) {
        const bool space = c == ' ' || c == '\n' || c == '\t' || c == '\r';
        if (!space && !in_word) ++words;
        in_word = !space;
    }
    return words;
}

std::vector<std::string> FixtureBackend::pieces(const std::string& text) {
    std::vector<std::string> out;
    std::size_t i = 0;
    while (i < text.size()) {
        std::size_t len = 0;
        while (i + len < text.size()) {
            const std::size_t ch = utf8_length(static_cast<unsigned char>(text[i + len]));
            if (len > 0 && len + ch > 4) break;
            len += ch;
            if (len >= 4) break;
        }
        len = std::min(len, text.size() - i);
        out.push_back(text.substr(i, len));
        i += len;
    }
    return out;
}

GenerationOutcome FixtureBackend::generate(const GenerationRequest& request, const TextSink& sink,
                                           const CancelFlag& cancel) {
    GenerationOutcome outcome;
    const auto prompt_tokens = count_prompt_tokens(request.prompt);
    outcome.stats.prompt_tokens = prompt_tokens;

    const auto prefill = std::chrono::milliseconds(
        static_cast<long long>(
            config_.prefill_ms_per_1k_tokens * static_cast<double>(prompt_tokens) / 1000.0));
    const auto prefill_started = std::chrono::steady_clock::now();
    if (!sleep_unless_cancelled(prefill, cancel)) {
        outcome.finish = FinishReason::Cancelled;
        return outcome;
    }
    outcome.stats.prefill_ms = std::chrono::duration<double, std::milli>(
                                   std::chrono::steady_clock::now() - prefill_started)
                                   .count();

    const std::string* reply = nullptr;
    for (const auto& rule : config_.rules) {
        if (request.prompt.find(rule.prompt_contains) != std::string::npos) {
            reply = &rule.reply;
            break;
        }
    }
    if (!reply && config_.default_reply) reply = &*config_.default_reply;

    static constexpr std::array<std::string_view, 5> kFiller{"lorem ", "ipsum ", "dolor ", "sit ",
                                                             "amet "};
    const auto decode_started = std::chrono::steady_clock::now();
    const auto emit = [&](const std::string& piece) -> bool {
        if (!sleep_unless_cancelled(config_.piece_delay, cancel)) {
            outcome.finish = FinishReason::Cancelled;
            return false;
        }
        ++outcome.stats.completion_tokens;
        if (sink(piece) == SinkAction::Stop) {
            outcome.finish = cancel.requested() ? FinishReason::Cancelled : FinishReason::StoppedBySink;
            return false;
        }
        return true;
    };

    bool finished_naturally = true;
    if (reply) {
        for (const auto& piece : pieces(*reply)) {
            if (outcome.stats.completion_tokens >= request.max_new_tokens) {
                outcome.finish = FinishReason::Length;
                finished_naturally = false;
                break;
            }
            if (!emit(piece)) {
                finished_naturally = false;
                break;
            }
        }
        if (finished_naturally) outcome.finish = FinishReason::EndOfSequence;
    } else {
        std::size_t k = 0;
        for (;;) {
            if (outcome.stats.completion_tokens >= request.max_new_tokens) {
                outcome.finish = FinishReason::Length;
                break;
            }
            if (!emit(std::string(kFiller.at(k++ % kFiller.size())))) break;
        }
    }
    outcome.stats.decode_ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - decode_started)
                                  .count();
    return outcome;
}

}  // namespace lca
