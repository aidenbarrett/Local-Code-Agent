// The one UTF-8 -> filesystem path conversion, shared by plugin and model loading.
#pragma once

#include <filesystem>
#include <string>

namespace lca {

// Paths reach the endpoint as UTF-8 (command line, JSON config). On Windows the
// native path encoding is UTF-16, so a plain std::filesystem::path(std::string)
// would decode the bytes in the active code page and corrupt non-ASCII paths.
// Building the path from char8_t text makes the UTF-8 contract explicit on every
// platform. It replaces std::filesystem::u8path, which C++20 deprecates.
[[nodiscard]] inline std::filesystem::path path_from_utf8(const std::string& utf8) {
    return {std::u8string(utf8.begin(), utf8.end())};
}

}  // namespace lca
