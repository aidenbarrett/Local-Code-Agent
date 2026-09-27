#include <doctest/doctest.h>

#include "output_parser.hpp"

using lca::ParsedPart;
using lca::ReasoningToolCallParser;
using lca::StopStringFilter;

namespace {

struct Collected {
    std::string content;
    std::string reasoning;
    std::vector<lca::ParsedToolCall> calls;
};

void absorb(Collected& c, const std::vector<ParsedPart>& parts) {
    for (const auto& p : parts) {
        if (p.kind == ParsedPart::Kind::Content) c.content += p.text;
        if (p.kind == ParsedPart::Kind::Reasoning) c.reasoning += p.text;
        if (p.kind == ParsedPart::Kind::ToolCall) c.calls.push_back(p.call);
    }
}

// Feeds text one byte at a time: the worst case for marker splitting.
Collected parse_bytewise(const std::string& text, bool tools = true) {
    ReasoningToolCallParser parser(tools, false);
    Collected c;
    for (char ch : text) absorb(c, parser.push(std::string(1, ch)));
    absorb(c, parser.finish());
    return c;
}

}  // namespace

TEST_CASE("stop strings match across piece boundaries and are not emitted") {
    StopStringFilter f({"STOP", "<|x|>"});
    std::string out;
    bool stopped = false;
    for (const char* piece : {"hello S", "TO", "P world"}) {
        auto r = f.push(piece);
        out += r.released;
        stopped = stopped || r.stop_matched;
    }
    CHECK(stopped);
    CHECK(out == "hello ");
    CHECK(f.flush().empty());
}

TEST_CASE("held-back prefixes are released when they turn out not to be a stop") {
    StopStringFilter f({"STOP"});
    CHECK(f.push("abc ST").released == "abc ");
    CHECK(f.push("ART").released == "START");
    CHECK(f.push("x S").released == "x ");
    CHECK(f.flush() == "S");
}

TEST_CASE("no stop strings passes text straight through") {
    StopStringFilter f({});
    CHECK(f.push("anything").released == "anything");
}

TEST_CASE("reasoning and answer are split, newlines after </think> dropped") {
    const auto c = parse_bytewise("<think>\nplan it\n</think>\n\nThe answer.");
    CHECK(c.reasoning == "\nplan it\n");
    CHECK(c.content == "The answer.");
    CHECK(c.calls.empty());
}

TEST_CASE("hermes tool calls parse even when split byte by byte") {
    const auto c = parse_bytewise(
        "<tool_call>\n{\"name\": \"echo_value\", \"arguments\": {\"value\": \"READY\"}}\n</tool_call>\n"
        "<tool_call>\n{\"name\": \"ls\", \"arguments\": \"{\\\"path\\\": \\\".\\\"}\"}\n</tool_call>");
    REQUIRE(c.calls.size() == 2);
    CHECK(c.calls[0].name == "echo_value");
    CHECK(c.calls[0].arguments_json == "{\"value\": \"READY\"}");
    CHECK(c.calls[1].name == "ls");
    CHECK(c.calls[1].arguments_json == "{\"path\": \".\"}");
    CHECK(c.content.empty());  // whitespace between calls is formatting
}

TEST_CASE("a malformed tool call is surfaced as text, not dropped") {
    const auto c = parse_bytewise("<tool_call>{not json}</tool_call>");
    CHECK(c.calls.empty());
    CHECK(c.content == "<tool_call>{not json}</tool_call>");
}

TEST_CASE("an unterminated tool call is reported raw at the end") {
    const auto c = parse_bytewise("<tool_call>\n{\"name\": \"ls\"");
    CHECK(c.calls.empty());
    CHECK(c.content == "<tool_call>\n{\"name\": \"ls\"");
}

TEST_CASE("tool call markup is plain content when the request offered no tools") {
    const auto c = parse_bytewise("<tool_call>{\"name\": \"ls\"}</tool_call>", false);
    CHECK(c.calls.empty());
    CHECK(c.content == "<tool_call>{\"name\": \"ls\"}</tool_call>");
}

TEST_CASE("text that only looks like the start of a marker is released") {
    ReasoningToolCallParser parser(true, false);
    Collected c;
    absorb(c, parser.push("a <to"));
    CHECK(c.content == "a ");
    absorb(c, parser.push("p> b <"));
    absorb(c, parser.finish());
    CHECK(c.content == "a <top> b <");
}

TEST_CASE("a prompt that opens reasoning starts in reasoning mode") {
    ReasoningToolCallParser parser(false, true);
    Collected c;
    absorb(c, parser.push("thinking</think>answer"));
    absorb(c, parser.finish());
    CHECK(c.reasoning == "thinking");
    CHECK(c.content == "answer");
}

TEST_CASE("tool call validation") {
    CHECK_FALSE(lca::parse_hermes_tool_call("{\"arguments\": {}}"));
    CHECK_FALSE(lca::parse_hermes_tool_call("{\"name\": \"x\", \"arguments\": [1]}"));
    CHECK_FALSE(lca::parse_hermes_tool_call("[\"x\"]"));
    auto ok = lca::parse_hermes_tool_call("  {\"name\": \"x\"}  ");
    REQUIRE(ok);
    CHECK(ok->arguments_json == "{}");
}
