// Prompt rendering for the ChatML / Hermes tool-call family (Qwen3 and
// compatible models).
//
// The endpoint applies the chat template itself instead of asking each
// runtime to do it, so every backend sees byte-identical prompts for the same
// conversation and tool-call parsing has exactly one owner.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace lca {

using ordered_json = nlohmann::ordered_json;

struct ChatToolCall {
    std::string id;
    std::string name;
    // Exactly as supplied by the client: OpenAI sends arguments as a JSON
    // string, which the template embeds verbatim.
    std::string arguments_text;
};

struct ChatMessage {
    std::string role;  // system | user | assistant | tool
    std::string content;
    std::optional<std::string> reasoning_content;
    std::vector<ChatToolCall> tool_calls;
};

struct RenderedPrompt {
    std::string text;
    // True when the prompt ends inside an open <think> block, so the first
    // generated text is reasoning rather than answer.
    bool opens_in_reasoning = false;
    // Text that ends the assistant turn in this template. The endpoint treats
    // these as stop strings, so a backend may stream special tokens verbatim.
    std::vector<std::string> end_of_turn_markers;
};

// Python json.dumps(value, ensure_ascii=False) with default separators and
// key order preserved: what the Hugging Face "tojson" template filter emits.
std::string python_style_json(const ordered_json& value);

// Renders the Qwen3 chat template with add_generation_prompt=true.
// tools holds the OpenAI "tools" array entries, verbatim.
// enable_thinking mirrors chat_template_kwargs.enable_thinking; nullopt means
// the template default (thinking allowed).
RenderedPrompt render_chatml_hermes_prompt(const std::vector<ChatMessage>& messages,
                                           const std::vector<ordered_json>& tools,
                                           std::optional<bool> enable_thinking);

}  // namespace lca
