#include "sandbox/text_util.hpp"

#include <cassert>
#include <cstdio>

int main() {
    const auto parts = sandbox::split("a,b,c", ',');
    assert(parts.size() == 3);
    assert(parts[0] == "a");
    assert(parts[2] == "c");

    assert(sandbox::split("", ',').size() == 1);
    assert(sandbox::join({"a", "b"}, '-') == "a-b");
    assert(sandbox::trim("  padded  ") == "padded");
    assert(sandbox::trim("   ").empty());

    std::puts("text_util ok");
    return 0;
}
