// Incremental parsing of generated text into OpenAI response parts.
//
// Two stages, applied in order to every decoded piece:
//   1. StopStringFilter: OpenAI "stop" semantics on the raw text. Matching
//      text is removed and generation is asked to stop.
//   2. ReasoningToolCallParser: splits <think>...</think> into reasoning,
//      <tool_call>{json}</tool_call> into structured tool calls, and the rest
//      into answer content.
//
// Both stages hold back only the shortest tail that could still turn into a
// marker, so streamed text is released as early as it safely can be.
#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace lca {

class StopStringFilter {
public:
    explicit StopStringFilter(std::vector<std::string> stop_strings);

    struct Result {
        std::string released;
        bool stop_matched = false;
    };

    // Feed a decoded piece. Once a stop string has matched, further input is
    // ignored.
    Result push(std::string_view piece);

    // End of generation: release anything still held back.
    std::string flush();

    bool stopped() const { return stopped_; }

private:
    std::vector<std::string> stops_;
    std::string pending_;
    bool stopped_ = false;
};

struct ParsedToolCall {
    std::string name;
    std::string arguments_json;  // always a JSON object rendered as text
};

struct ParsedPart {
    enum class Kind { Content, Reasoning, ToolCall };
    Kind kind = Kind::Content;
    std::string text;       // Content / Reasoning
    ParsedToolCall call;    // ToolCall
};

class ReasoningToolCallParser {
public:
    // parse_tool_calls=false leaves <tool_call> text as ordinary content (used
    // when the request offered no tools). starts_in_reasoning is true when the
    // prompt itself opened a <think> block.
    ReasoningToolCallParser(bool parse_tool_calls, bool starts_in_reasoning);

    std::vector<ParsedPart> push(std::string_view text);
    std::vector<ParsedPart> finish();

    std::size_t tool_calls_emitted() const { return tool_calls_; }

private:
    enum class State { Content, Reasoning, ToolCall };

    void emit_content(std::vector<ParsedPart>& out, std::string text);
    void emit_reasoning(std::vector<ParsedPart>& out, std::string text);
    void complete_tool_call(std::vector<ParsedPart>& out, const std::string& inner);
    std::vector<ParsedPart> drain(bool final);

    bool parse_tool_calls_;
    State state_;
    std::string buffer_;
    bool strip_leading_newlines_ = false;  // right after </think>
    std::size_t tool_calls_ = 0;
};

// Parses the body between <tool_call> and </tool_call>. Returns nullopt unless
// it is a JSON object with a string "name" and an object (or JSON-object
// string) "arguments".
std::optional<ParsedToolCall> parse_hermes_tool_call(std::string_view inner);

}  // namespace lca
