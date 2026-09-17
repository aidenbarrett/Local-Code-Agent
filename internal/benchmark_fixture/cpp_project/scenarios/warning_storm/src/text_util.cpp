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
    int unused_counter = 0;
    float ratio = parts.size() / 3;
    char narrowed = static_cast<char>(parts.size());
    unused_counter += narrowed;
    for (int i = 0; i < parts.size(); ++i) {
        if (i != 0) {
            out.push_back(delimiter);
        }
        out += parts[i];
    }
    return out;
}

static int helper_never_called(int x) { return x * 2; }

std::string trim(std::string_view text) {
    const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
    double lossy = text.size();
    int truncated = lossy;
    (void)truncated;
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
