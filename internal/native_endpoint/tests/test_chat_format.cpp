#include <doctest/doctest.h>

#include "chat_format.hpp"

using lca::ChatMessage;
using lca::ChatToolCall;
using lca::ordered_json;
using lca::python_style_json;
using lca::render_chatml_hermes_prompt;

namespace {

ChatMessage msg(const std::string& role, const std::string& content) {
    ChatMessage m;
    m.role = role;
    m.content = content;
    return m;
}

}  // namespace

TEST_CASE("tojson matches Python json.dumps with ensure_ascii=False") {
    const auto value = ordered_json::parse(
        R"({"type":"function","function":{"name":"read","parameters":{"b":1,"a":[true,null,2.5]},"text":"é \"q\"\n\u0001"}})");
    CHECK(python_style_json(value) ==
          "{\"type\": \"function\", \"function\": {\"name\": \"read\", \"parameters\": "
          "{\"b\": 1, \"a\": [true, null, 2.5]}, \"text\": \"é \\\"q\\\"\\n\\u0001\"}}");
}

TEST_CASE("plain conversation renders as ChatML with a generation prompt") {
    const auto p = render_chatml_hermes_prompt({msg("system", "Be brief."), msg("user", "Hi")}, {}, std::nullopt);
    CHECK(p.text ==
          "<|im_start|>system\nBe brief.<|im_end|>\n"
          "<|im_start|>user\nHi<|im_end|>\n"
          "<|im_start|>assistant\n");
    CHECK_FALSE(p.opens_in_reasoning);
}

TEST_CASE("enable_thinking=false closes an empty think block") {
    const auto p = render_chatml_hermes_prompt({msg("user", "Hi")}, {}, false);
    CHECK(p.text == "<|im_start|>user\nHi<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n");
    const auto t = render_chatml_hermes_prompt({msg("user", "Hi")}, {}, true);
    CHECK(t.text == "<|im_start|>user\nHi<|im_end|>\n<|im_start|>assistant\n");
}

TEST_CASE("tools are merged into the system block") {
    const auto tool = ordered_json::parse(
        R"({"type": "function", "function": {"name": "echo_value", "parameters": {"type": "object"}}})");
    const auto p = render_chatml_hermes_prompt({msg("system", "SYS"), msg("user", "go")}, {tool}, std::nullopt);
    CHECK(p.text ==
          "<|im_start|>system\nSYS\n\n# Tools\n\nYou may call one or more functions to assist with the user "
          "query.\n\nYou are provided with function signatures within <tools></tools> XML tags:\n<tools>\n"
          "{\"type\": \"function\", \"function\": {\"name\": \"echo_value\", \"parameters\": {\"type\": "
          "\"object\"}}}\n</tools>\n\nFor each function call, return a json object with function name and "
          "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, "
          "\"arguments\": <args-json-object>}\n</tool_call><|im_end|>\n"
          "<|im_start|>user\ngo<|im_end|>\n<|im_start|>assistant\n");
}

TEST_CASE("tool loop: calls, grouped responses and kept reasoning after the last query") {
    ChatMessage call = msg("assistant", "");
    call.reasoning_content = "need a file";
    call.tool_calls.push_back(ChatToolCall{"c1", "read", "{\"path\": \"a\"}"});
    call.tool_calls.push_back(ChatToolCall{"c2", "read", "{\"path\": \"b\"}"});
    const auto p = render_chatml_hermes_prompt(
        {msg("user", "look"), call, msg("tool", "A"), msg("tool", "B")}, {}, std::nullopt);
    CHECK(p.text ==
          "<|im_start|>user\nlook<|im_end|>\n"
          "<|im_start|>assistant\n<think>\nneed a file\n</think>\n\n"
          "<tool_call>\n{\"name\": \"read\", \"arguments\": {\"path\": \"a\"}}\n</tool_call>\n"
          "<tool_call>\n{\"name\": \"read\", \"arguments\": {\"path\": \"b\"}}\n</tool_call><|im_end|>\n"
          "<|im_start|>user\n<tool_response>\nA\n</tool_response>\n<tool_response>\nB\n</tool_response><|im_end|>\n"
          "<|im_start|>assistant\n");
}

TEST_CASE("assistant turns before the last query drop their reasoning") {
    const auto p = render_chatml_hermes_prompt(
        {msg("user", "one"), msg("assistant", "<think>\nhmm\n</think>\n\nfirst"), msg("user", "two")}, {},
        std::nullopt);
    CHECK(p.text ==
          "<|im_start|>user\none<|im_end|>\n<|im_start|>assistant\nfirst<|im_end|>\n"
          "<|im_start|>user\ntwo<|im_end|>\n<|im_start|>assistant\n");
}

TEST_CASE("assistant content before a tool call is separated by a newline") {
    ChatMessage call = msg("assistant", "Checking.");
    call.tool_calls.push_back(ChatToolCall{"c1", "ls", "{}"});
    const auto p = render_chatml_hermes_prompt({msg("user", "q"), call}, {}, std::nullopt);
    CHECK(p.text.find("Checking.\n<tool_call>\n{\"name\": \"ls\", \"arguments\": {}}\n</tool_call><|im_end|>") !=
          std::string::npos);
}
