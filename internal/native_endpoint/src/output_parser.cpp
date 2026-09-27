#include "output_parser.hpp"

#include <algorithm>

#include <nlohmann/json.hpp>

#include "chat_format.hpp"

namespace lca {

namespace {

constexpr std::string_view kThinkOpen = "<think>";
constexpr std::string_view kThinkClose = "</think>";
constexpr std::string_view kToolOpen = "<tool_call>";
constexpr std::string_view kToolClose = "</tool_call>";

// Length of the longest suffix of text that is a proper prefix of marker.
std::size_t partial_suffix(std::string_view text, std::string_view marker) {
    const std::size_t limit = std::min(text.size(), marker.size() - 1);
    for (std::size_t len = limit; len > 0; --len) {
        if (text.substr(text.size() - len) == marker.substr(0, len)) return len;
    }
    return 0;
}

bool is_blank(const std::string& s) {
    return std::all_of(s.begin(), s.end(),
                       [](char c) { return c == ' ' || c == '\n' || c == '\r' || c == '\t'; });
}

std::string_view trim(std::string_view s) {
    const auto ws = " \n\r\t";
    const auto b = s.find_first_not_of(ws);
    if (b == std::string_view::npos) return {};
    const auto e = s.find_last_not_of(ws);
    return s.substr(b, e - b + 1);
}

}  // namespace

// ------------------------------------------------------------ stop strings

StopStringFilter::StopStringFilter(std::vector<std::string> stop_strings) {
    for (auto& s : stop_strings) {
        if (!s.empty()) stops_.push_back(std::move(s));
    }
}

StopStringFilter::Result StopStringFilter::push(std::string_view piece) {
    Result result;
    if (stopped_) {
        result.stop_matched = true;
        return result;
    }
    pending_.append(piece.data(), piece.size());
    if (stops_.empty()) {
        result.released.swap(pending_);
        return result;
    }

    std::size_t earliest = std::string::npos;
    for (const auto& stop : stops_) {
        const auto at = pending_.find(stop);
        if (at < earliest) earliest = at;
    }
    if (earliest != std::string::npos) {
        result.released = pending_.substr(0, earliest);
        result.stop_matched = true;
        pending_.clear();
        stopped_ = true;
        return result;
    }

    std::size_t hold = 0;
    for (const auto& stop : stops_) hold = std::max(hold, partial_suffix(pending_, stop));
    result.released = pending_.substr(0, pending_.size() - hold);
    pending_.erase(0, pending_.size() - hold);
    return result;
}

std::string StopStringFilter::flush() {
    std::string out;
    if (!stopped_) out.swap(pending_);
    pending_.clear();
    return out;
}

// ------------------------------------------------------------ tool calls

std::optional<ParsedToolCall> parse_hermes_tool_call(std::string_view inner) {
    const auto body = trim(inner);
    auto parsed = ordered_json::parse(body.begin(), body.end(), nullptr, /*allow_exceptions=*/false);
    if (parsed.is_discarded() || !parsed.is_object()) return std::nullopt;
    const auto name = parsed.find("name");
    if (name == parsed.end() || !name->is_string() || name->get_ref<const std::string&>().empty()) {
        return std::nullopt;
    }
    ordered_json arguments = ordered_json::object();
    const auto args = parsed.find("arguments");
    if (args != parsed.end()) {
        if (args->is_object()) {
            arguments = *args;
        } else if (args->is_string()) {
            auto nested = ordered_json::parse(args->get_ref<const std::string&>(), nullptr, false);
            if (nested.is_discarded() || !nested.is_object()) return std::nullopt;
            arguments = std::move(nested);
        } else if (!args->is_null()) {
            return std::nullopt;
        }
    }
    ParsedToolCall call;
    call.name = name->get<std::string>();
    call.arguments_json = python_style_json(arguments);
    return call;
}

// ------------------------------------------------------------ reasoning / tools

ReasoningToolCallParser::ReasoningToolCallParser(bool parse_tool_calls, bool starts_in_reasoning)
    : parse_tool_calls_(parse_tool_calls),
      state_(starts_in_reasoning ? State::Reasoning : State::Content) {}

void ReasoningToolCallParser::emit_content(std::vector<ParsedPart>& out, std::string text) {
    if (strip_leading_newlines_) {
        const auto keep = text.find_first_not_of('\n');
        if (keep == std::string::npos) return;  // still only newlines; keep stripping
        text.erase(0, keep);
        strip_leading_newlines_ = false;
    }
    if (text.empty()) return;
    // Whitespace between or after tool calls is formatting, not an answer.
    if (tool_calls_ > 0 && is_blank(text)) return;
    if (!out.empty() && out.back().kind == ParsedPart::Kind::Content) {
        out.back().text += text;
        return;
    }
    ParsedPart part;
    part.kind = ParsedPart::Kind::Content;
    part.text = std::move(text);
    out.push_back(std::move(part));
}

void ReasoningToolCallParser::emit_reasoning(std::vector<ParsedPart>& out, std::string text) {
    if (text.empty()) return;
    if (!out.empty() && out.back().kind == ParsedPart::Kind::Reasoning) {
        out.back().text += text;
        return;
    }
    ParsedPart part;
    part.kind = ParsedPart::Kind::Reasoning;
    part.text = std::move(text);
    out.push_back(std::move(part));
}

void ReasoningToolCallParser::complete_tool_call(std::vector<ParsedPart>& out,
                                                 const std::string& inner) {
    auto call = parse_hermes_tool_call(inner);
    if (!call) {
        // A malformed call is surfaced as text so the caller sees exactly what
        // the model produced instead of a silently dropped action.
        emit_content(out, std::string(kToolOpen) + inner + std::string(kToolClose));
        return;
    }
    ParsedPart part;
    part.kind = ParsedPart::Kind::ToolCall;
    part.call = std::move(*call);
    out.push_back(std::move(part));
    ++tool_calls_;
}

std::vector<ParsedPart> ReasoningToolCallParser::drain(bool final) {
    std::vector<ParsedPart> out;
    for (;;) {
        if (state_ == State::Content) {
            const auto think = buffer_.find(kThinkOpen);
            const auto tool = parse_tool_calls_ ? buffer_.find(kToolOpen) : std::string::npos;
            const auto next = std::min(think, tool);
            if (next != std::string::npos) {
                emit_content(out, buffer_.substr(0, next));
                if (next == think) {
                    buffer_.erase(0, next + kThinkOpen.size());
                    state_ = State::Reasoning;
                } else {
                    buffer_.erase(0, next + kToolOpen.size());
                    state_ = State::ToolCall;
                }
                continue;
            }
            std::size_t hold = 0;
            if (!final) {
                hold = partial_suffix(buffer_, kThinkOpen);
                if (parse_tool_calls_) hold = std::max(hold, partial_suffix(buffer_, kToolOpen));
            }
            emit_content(out, buffer_.substr(0, buffer_.size() - hold));
            buffer_.erase(0, buffer_.size() - hold);
            return out;
        }
        if (state_ == State::Reasoning) {
            const auto close = buffer_.find(kThinkClose);
            if (close != std::string::npos) {
                emit_reasoning(out, buffer_.substr(0, close));
                buffer_.erase(0, close + kThinkClose.size());
                state_ = State::Content;
                strip_leading_newlines_ = true;
                continue;
            }
            const std::size_t hold = final ? 0 : partial_suffix(buffer_, kThinkClose);
            emit_reasoning(out, buffer_.substr(0, buffer_.size() - hold));
            buffer_.erase(0, buffer_.size() - hold);
            return out;
        }
        // Tool call body: nothing is released until it is complete.
        const auto close = buffer_.find(kToolClose);
        if (close != std::string::npos) {
            complete_tool_call(out, buffer_.substr(0, close));
            buffer_.erase(0, close + kToolClose.size());
            state_ = State::Content;
            continue;
        }
        if (final) {
            // Unterminated call (for example cut off by max_tokens): report
            // the raw text rather than inventing a call.
            emit_content(out, std::string(kToolOpen) + buffer_);
            buffer_.clear();
            state_ = State::Content;
        }
        return out;
    }
}

std::vector<ParsedPart> ReasoningToolCallParser::push(std::string_view text) {
    buffer_.append(text.data(), text.size());
    return drain(false);
}

std::vector<ParsedPart> ReasoningToolCallParser::finish() { return drain(true); }

}  // namespace lca
