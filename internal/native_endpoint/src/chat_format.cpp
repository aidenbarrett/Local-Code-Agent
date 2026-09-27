#include "chat_format.hpp"

#include <array>

namespace lca {

namespace {

void append_python_string(std::string& out, const std::string& value) {
    out.push_back('"');
    for (const char ch : value) {
        const auto c = static_cast<unsigned char>(ch);
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20) {
                    // A control character is written as \u00XX: no printf, no buffer.
                    constexpr std::array<char, 16> kHex{'0', '1', '2', '3', '4', '5', '6', '7',
                                                        '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'};
                    out += "\\u00";
                    out.push_back(kHex.at(c >> 4U));
                    out.push_back(kHex.at(c & 0x0FU));
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

void append_python_json(std::string& out, const ordered_json& value) {
    switch (value.type()) {
        case ordered_json::value_t::object: {
            out.push_back('{');
            bool first = true;
            for (auto it = value.begin(); it != value.end(); ++it) {
                if (!first) out += ", ";
                first = false;
                append_python_string(out, it.key());
                out += ": ";
                append_python_json(out, it.value());
            }
            out.push_back('}');
            break;
        }
        case ordered_json::value_t::array: {
            out.push_back('[');
            bool first = true;
            for (const auto& item : value) {
                if (!first) out += ", ";
                first = false;
                append_python_json(out, item);
            }
            out.push_back(']');
            break;
        }
        case ordered_json::value_t::string:
            append_python_string(out, value.get_ref<const std::string&>());
            break;
        case ordered_json::value_t::null:
            out += "null";
            break;
        default:
            // Booleans and numbers: nlohmann's shortest round-trip form matches
            // Python for every value a tool schema realistically holds.
            out += value.dump();
    }
}

std::string strip_leading(const std::string& s, char c) {
    std::size_t i = 0;
    while (i < s.size() && s[i] == c) ++i;
    return s.substr(i);
}

std::string strip_trailing(const std::string& s, char c) {
    std::size_t n = s.size();
    while (n > 0 && s[n - 1] == c) --n;
    return s.substr(0, n);
}

bool starts_with(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool ends_with(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() &&
           s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

const char* const kToolsPreamble =
    "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n<tools>";

const char* const kToolsPostamble =
    "\n</tools>\n\nFor each function call, return a json object with function name and "
    "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": "
    "<function-name>, \"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n";

}  // namespace

std::string python_style_json(const ordered_json& value) {
    std::string out;
    append_python_json(out, value);
    return out;
}

RenderedPrompt render_chatml_hermes_prompt(const std::vector<ChatMessage>& messages,
                                           const std::vector<ordered_json>& tools,
                                           std::optional<bool> enable_thinking) {
    std::string out;
    const bool first_is_system = !messages.empty() && messages.front().role == "system";

    if (!tools.empty()) {
        out += "<|im_start|>system\n";
        if (first_is_system) out += messages.front().content + "\n\n";
        out += kToolsPreamble;
        for (const auto& tool : tools) {
            out += "\n";
            out += python_style_json(tool);
        }
        out += kToolsPostamble;
    } else if (first_is_system) {
        out += "<|im_start|>system\n" + messages.front().content + "<|im_end|>\n";
    }

    // The last genuine user query. Assistant turns after it (an agent's tool
    // loop) keep their reasoning; earlier ones are rendered without it.
    std::size_t last_query_index = messages.empty() ? 0 : messages.size() - 1;
    for (std::size_t n = messages.size(); n-- > 0;) {
        const auto& m = messages[n];
        if (m.role == "user" &&
            !(starts_with(m.content, "<tool_response>") && ends_with(m.content, "</tool_response>"))) {
            last_query_index = n;
            break;
        }
    }

    for (std::size_t i = 0; i < messages.size(); ++i) {
        const auto& m = messages[i];
        const bool is_last = i + 1 == messages.size();
        std::string content = m.content;

        if (m.role == "user" || (m.role == "system" && i != 0)) {
            out += "<|im_start|>" + m.role + "\n" + content + "<|im_end|>\n";
        } else if (m.role == "assistant") {
            std::string reasoning;
            if (m.reasoning_content) {
                reasoning = *m.reasoning_content;
            } else {
                const auto close = content.find("</think>");
                if (close != std::string::npos) {
                    std::string before = strip_trailing(content.substr(0, close), '\n');
                    const auto open = before.rfind("<think>");
                    if (open != std::string::npos) before = before.substr(open + 7);
                    reasoning = strip_leading(before, '\n');
                    content = strip_leading(content.substr(content.rfind("</think>") + 8), '\n');
                }
            }
            if (i > last_query_index && (is_last || !reasoning.empty())) {
                out += "<|im_start|>assistant\n<think>\n" +
                       strip_trailing(strip_leading(reasoning, '\n'), '\n') + "\n</think>\n\n" +
                       strip_leading(content, '\n');
            } else {
                out += "<|im_start|>assistant\n" + content;
            }
            for (std::size_t k = 0; k < m.tool_calls.size(); ++k) {
                if ((k == 0 && !content.empty()) || k > 0) out += "\n";
                out += "<tool_call>\n{\"name\": \"" + m.tool_calls[k].name + R"(", "arguments": )" +
                       m.tool_calls[k].arguments_text + "}\n</tool_call>";
            }
            out += "<|im_end|>\n";
        } else if (m.role == "tool") {
            if (i == 0 || messages[i - 1].role != "tool") out += "<|im_start|>user";
            out += "\n<tool_response>\n" + content + "\n</tool_response>";
            if (is_last || messages[i + 1].role != "tool") out += "<|im_end|>\n";
        }
        // A leading system message was rendered above; nothing else to do.
    }

    out += "<|im_start|>assistant\n";
    RenderedPrompt rendered;
    if (enable_thinking.has_value() && !*enable_thinking) {
        out += "<think>\n\n</think>\n\n";
    }
    rendered.text = std::move(out);
    rendered.opens_in_reasoning = false;
    rendered.end_of_turn_markers = {"<|im_end|>", "<|endoftext|>"};
    return rendered;
}

}  // namespace lca
