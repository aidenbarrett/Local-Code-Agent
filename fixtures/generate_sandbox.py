#!/usr/bin/env python3
"""Generate the synthetic C++ torture repository.

Kept as a generator rather than a committed tree so the scenarios and the
baseline can never drift apart: everything is written from one source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "cpp_sandbox"

FILES: dict[str, str] = {}
SCENARIOS: dict[str, dict[str, str]] = {}


def f(path: str, content: str) -> None:
    FILES[path] = content.lstrip("\n")


def scenario(name: str, overrides: dict[str, str]) -> None:
    SCENARIOS[name] = {k: v.lstrip("\n") for k, v in overrides.items()}


# ---------------------------------------------------------------- baseline

f(
    "CMakeLists.txt",
    """
cmake_minimum_required(VERSION 3.20)
project(cpp_sandbox CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(MSVC)
  add_compile_options(/W4)
else()
  add_compile_options(-Wall -Wextra)
endif()

add_library(sandbox
  src/ring_buffer.cpp
  src/text_util.cpp
)
target_include_directories(sandbox PUBLIC include)

enable_testing()

foreach(name ring_buffer text_util fd_owner slow)
  add_executable(test_${name} tests/test_${name}.cpp)
  target_link_libraries(test_${name} PRIVATE sandbox)
  add_test(NAME ${name} COMMAND test_${name})
endforeach()

set_tests_properties(slow PROPERTIES TIMEOUT 5)
""",
)

f(
    "include/sandbox/ring_buffer.hpp",
    """
#pragma once

#include <cstddef>
#include <optional>
#include <vector>

namespace sandbox {

// Fixed-capacity FIFO. Overwrites nothing: push on a full buffer is rejected.
class RingBuffer {
public:
    explicit RingBuffer(std::size_t capacity);

    bool push(int value);
    std::optional<int> pop();

    [[nodiscard]] bool empty() const;
    [[nodiscard]] bool full() const;
    [[nodiscard]] std::size_t size() const;
    [[nodiscard]] std::size_t capacity() const;

private:
    std::vector<int> slots_;
    std::size_t head_ = 0;
    std::size_t tail_ = 0;
    std::size_t count_ = 0;
};

// Declared here, defined in ring_buffer.cpp. The link-error scenario removes
// the definition and leaves this declaration in place.
int checksum(const std::vector<int>& values);

}  // namespace sandbox
""",
)

f(
    "src/ring_buffer.cpp",
    """
#include "sandbox/ring_buffer.hpp"

namespace sandbox {

RingBuffer::RingBuffer(std::size_t capacity) : slots_(capacity) {}

bool RingBuffer::push(int value) {
    if (full()) {
        return false;
    }
    slots_[tail_] = value;
    tail_ = (tail_ + 1) % slots_.size();
    ++count_;
    return true;
}

std::optional<int> RingBuffer::pop() {
    if (empty()) {
        return std::nullopt;
    }
    const int value = slots_[head_];
    head_ = (head_ + 1) % slots_.size();
    --count_;
    return value;
}

bool RingBuffer::empty() const { return count_ == 0; }

bool RingBuffer::full() const { return count_ == slots_.size(); }

std::size_t RingBuffer::size() const { return count_; }

std::size_t RingBuffer::capacity() const { return slots_.size(); }

int checksum(const std::vector<int>& values) {
    int total = 0;
    for (const int v : values) {
        total = (total * 31 + v) % 100003;
    }
    return total;
}

}  // namespace sandbox
""",
)

f(
    "include/sandbox/text_util.hpp",
    """
#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace sandbox {

std::vector<std::string> split(std::string_view text, char delimiter);
std::string join(const std::vector<std::string>& parts, char delimiter);
std::string trim(std::string_view text);

}  // namespace sandbox
""",
)

f(
    "src/text_util.cpp",
    """
#include "sandbox/text_util.hpp"

#include <algorithm>
#include <cctype>

namespace sandbox {

std::vector<std::string> split(std::string_view text, char delimiter) {
    std::vector<std::string> parts;
    std::size_t start = 0;
    while (start <= text.size()) {
        const std::size_t end = text.find(delimiter, start);
        if (end == std::string_view::npos) {
            parts.emplace_back(text.substr(start));
            break;
        }
        parts.emplace_back(text.substr(start, end - start));
        start = end + 1;
    }
    return parts;
}

std::string join(const std::vector<std::string>& parts, char delimiter) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i != 0) {
            out.push_back(delimiter);
        }
        out += parts[i];
    }
    return out;
}

std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
    std::size_t begin = 0;
    while (begin < text.size() && is_space(static_cast<unsigned char>(text[begin]))) {
        ++begin;
    }
    std::size_t end = text.size();
    while (end > begin && is_space(static_cast<unsigned char>(text[end - 1]))) {
        --end;
    }
    return std::string(text.substr(begin, end - begin));
}

}  // namespace sandbox
""",
)

f(
    "include/sandbox/fd_owner.hpp",
    """
#pragma once

#include <utility>

namespace sandbox {

// Move-only owner of a POSIX-style file descriptor. Header only so the
// scenarios can break the library without disturbing it.
class FdOwner {
public:
    FdOwner() = default;
    explicit FdOwner(int fd) noexcept : fd_(fd) {}

    FdOwner(const FdOwner&) = delete;
    FdOwner& operator=(const FdOwner&) = delete;

    FdOwner(FdOwner&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}

    FdOwner& operator=(FdOwner&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }

    ~FdOwner() { reset(); }

    [[nodiscard]] int get() const noexcept { return fd_; }
    [[nodiscard]] bool valid() const noexcept { return fd_ >= 0; }

    int release() noexcept { return std::exchange(fd_, -1); }

    void reset(int fd = -1) noexcept {
        // The sandbox does not actually close anything; it only tracks state.
        fd_ = fd;
    }

private:
    int fd_ = -1;
};

}  // namespace sandbox
""",
)

f(
    "tests/test_ring_buffer.cpp",
    """
#include "sandbox/ring_buffer.hpp"

#include <cassert>
#include <cstdio>
#include <vector>

int main() {
    sandbox::RingBuffer buffer(3);
    assert(buffer.empty());
    assert(buffer.capacity() == 3);

    assert(buffer.push(1));
    assert(buffer.push(2));
    assert(buffer.push(3));
    assert(buffer.full());
    assert(!buffer.push(4) && "push on a full buffer must be rejected");

    assert(buffer.pop().value() == 1);
    assert(buffer.pop().value() == 2);
    assert(buffer.pop().value() == 3);
    assert(buffer.empty());
    assert(!buffer.pop().has_value());

    const std::vector<int> values{1, 2, 3, 4};
    assert(sandbox::checksum(values) == 31810);

    std::puts("ring_buffer ok");
    return 0;
}
""",
)

f(
    "tests/test_text_util.cpp",
    """
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
""",
)

f(
    "tests/test_fd_owner.cpp",
    """
#include "sandbox/fd_owner.hpp"

#include <cassert>
#include <cstdio>
#include <utility>

int main() {
    sandbox::FdOwner a(7);
    assert(a.valid());
    assert(a.get() == 7);

    sandbox::FdOwner b(std::move(a));
    assert(b.get() == 7);
    assert(!a.valid() && "moved-from owner must relinquish the descriptor");

    sandbox::FdOwner c;
    c = std::move(b);
    assert(c.get() == 7);
    assert(!b.valid());

    assert(c.release() == 7);
    assert(!c.valid());

    std::puts("fd_owner ok");
    return 0;
}
""",
)

f(
    "tests/test_slow.cpp",
    """
#include <cstdio>

int main() {
    // Bounded work. The timeout scenario replaces the bound with a condition
    // that never becomes false.
    unsigned long long acc = 0;
    for (unsigned long long i = 0; i < 2000000ULL; ++i) {
        acc += i % 7;
    }
    std::printf("slow ok %llu\\n", acc);
    return 0;
}
""",
)

f(
    ".local-agent.toml",
    """
[repo]
name = "cpp-sandbox"
build_dir = "build"
run_dir = ".local-agent/runs"
default_profile = "debug"
skills_dir = ".github/skills"

[profiles.debug]
configure = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]
build = ["cmake", "--build", "build", "--parallel", "4"]
test = ["ctest", "--test-dir", "build", "--output-on-failure"]

[profiles.release]
configure = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=RelWithDebInfo"]
build = ["cmake", "--build", "build", "--parallel", "4"]
test = ["ctest", "--test-dir", "build", "--output-on-failure"]

[policy]
allow_build = true
allow_test = true
allow_patch = true
allow_commit = false
command_timeout_seconds = 300
max_tool_calls = 30
max_repeat_calls = 3
""",
)

f(
    ".gitignore",
    """
build/
.local-agent/
""",
)

f(
    "README.md",
    """
# cpp-sandbox

A deliberately breakable C++ repository for exercising the agent. Nothing here
is real work and nothing here is anybody's intellectual property.

    python scripts/apply_scenario.py --list
    python scripts/apply_scenario.py clean
    python scripts/apply_scenario.py test_failure

Each scenario overwrites a small number of files with a broken variant. `clean`
restores the baseline.
""",
)

# --------------------------------------------------------------- scenarios

scenario("clean", {})

scenario(
    "compile_error",
    {
        # `count` instead of `count_`, plus a missing semicolon further down.
        "src/ring_buffer.cpp": FILES["src/ring_buffer.cpp"]
        .replace("    ++count_;\n    return true;", "    ++count;\n    return true;")
        .replace(
            "bool RingBuffer::empty() const { return count_ == 0; }",
            "bool RingBuffer::empty() const { return count_ == 0 }",
        )
    },
)

scenario(
    "link_error",
    {
        # Declaration stays in the header; definition disappears.
        "src/ring_buffer.cpp": FILES["src/ring_buffer.cpp"].split(
            "int checksum(const std::vector<int>& values) {"
        )[0]
        + "}  // namespace sandbox\n"
    },
)

scenario(
    "test_failure",
    {
        # Off by one: the buffer reports full one slot early, so the third push
        # is rejected and the pop order assertion fails.
        "src/ring_buffer.cpp": FILES["src/ring_buffer.cpp"].replace(
            "bool RingBuffer::full() const { return count_ == slots_.size(); }",
            "bool RingBuffer::full() const { return count_ + 1 == slots_.size(); }",
        )
    },
)

scenario(
    "crash",
    {
        # Reads one past the end of the view, then dereferences a null pointer
        # on the empty-input path.
        "src/text_util.cpp": FILES["src/text_util.cpp"].replace(
            """std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };""",
            """std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
    if (text.find_first_not_of(" \\t") == std::string_view::npos) {
        const std::string* broken = nullptr;
        return *broken;  // deliberate null dereference on all-whitespace input
    }""",
        )
    },
)

scenario(
    "timeout",
    {
        "tests/test_slow.cpp": """
#include <cstdio>

int main() {
    // The bound is never reached: i wraps and the loop never terminates.
    unsigned char i = 0;
    unsigned long long acc = 0;
    while (i < 300) {
        acc += i;
        ++i;
    }
    std::printf("slow ok %llu\\n", acc);
    return 0;
}
"""
    },
)

scenario(
    "warning_storm",
    {
        "src/text_util.cpp": FILES["src/text_util.cpp"].replace(
            """std::string join(const std::vector<std::string>& parts, char delimiter) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {""",
            """std::string join(const std::vector<std::string>& parts, char delimiter) {
    std::string out;
    int unused_counter = 0;
    float ratio = parts.size() / 3;
    char narrowed = static_cast<char>(parts.size());
    unused_counter += narrowed;
    for (int i = 0; i < parts.size(); ++i) {""",
        ).replace(
            """std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };""",
            """static int helper_never_called(int x) { return x * 2; }

std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
    double lossy = text.size();
    int truncated = lossy;
    (void)truncated;""",
        )
    },
)


APPLY_SCRIPT = '''#!/usr/bin/env python3
"""Apply one failure scenario to the sandbox, or restore the baseline."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scenarios" / "manifest.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def restore_baseline(manifest: dict) -> None:
    baseline = ROOT / "scenarios" / "clean"
    for rel in manifest["managed_files"]:
        src = baseline / rel
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def apply(name: str) -> int:
    manifest = load()
    if name not in manifest["scenarios"]:
        print(f"unknown scenario {name!r}; known: {', '.join(manifest['scenarios'])}")
        return 2
    restore_baseline(manifest)
    for rel in manifest["scenarios"][name]:
        src = ROOT / "scenarios" / name / rel
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    print(f"scenario applied: {name}")
    if manifest["scenarios"][name]:
        print("  " + "\\n  ".join(manifest["scenarios"][name]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?", default="clean")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, files in load()["scenarios"].items():
            print(f"{name:16s} {len(files)} file(s) replaced")
        return 0
    return apply(args.scenario)


if __name__ == "__main__":
    sys.exit(main())
'''


def main() -> None:
    if ROOT.exists():
        import shutil

        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)

    for rel, content in FILES.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    managed = sorted(
        {rel for overrides in SCENARIOS.values() for rel in overrides}
    )

    clean_dir = ROOT / "scenarios" / "clean"
    for rel in managed:
        dst = clean_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(FILES[rel], encoding="utf-8")

    for name, overrides in SCENARIOS.items():
        if name == "clean":
            continue
        for rel, content in overrides.items():
            dst = ROOT / "scenarios" / name / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")

    manifest = {
        "managed_files": managed,
        "scenarios": {name: sorted(ov) for name, ov in SCENARIOS.items()},
    }
    (ROOT / "scenarios" / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    scripts = ROOT / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "apply_scenario.py").write_text(APPLY_SCRIPT, encoding="utf-8")
    (scripts / "apply_scenario.py").chmod(0o755)

    print(f"wrote {len(FILES)} baseline files and {len(SCENARIOS)} scenarios to {ROOT}")


if __name__ == "__main__":
    main()
