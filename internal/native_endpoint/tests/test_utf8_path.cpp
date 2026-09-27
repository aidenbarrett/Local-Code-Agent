#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <system_error>

#include "plugin_backend.hpp"
#include "utf8_path.hpp"

using namespace lca;

namespace {

// "modèle-日本-😀": Latin-1 range, CJK and a character outside the BMP (a UTF-16
// surrogate pair on Windows), spelled as UTF-8 bytes so the source encoding of
// this file does not matter.
const std::string kUtf8Name =
    "mod\xC3\xA8le-\xE6\x97\xA5\xE6\x9C\xAC-\xF0\x9F\x98\x80";

}  // namespace

TEST_CASE("a UTF-8 path round-trips exactly through the native path encoding") {
    const auto path = path_from_utf8("models/" + kUtf8Name + "/weights.bin");
    const auto back = path.generic_u8string();
    CHECK(std::string(back.begin(), back.end()) == "models/" + kUtf8Name + "/weights.bin");
    CHECK(path.filename().u8string() == u8"weights.bin");
#if defined(_WIN32)
    // The native form is UTF-16: 'è' is one unit and the emoji a surrogate pair.
    const std::wstring native = path.parent_path().filename().native();
    CHECK(native == L"modèle-日本-\U0001F600");
#endif
}

TEST_CASE("a plugin loads from a directory whose name is not ASCII") {
    const auto source = path_from_utf8(LCA_REFERENCE_PLUGIN_PATH);
    const auto dir = std::filesystem::temp_directory_path() / path_from_utf8("lca-" + kUtf8Name);
    std::error_code ignored;
    std::filesystem::remove_all(dir, ignored);
    std::filesystem::create_directories(dir);
    const auto copy = dir / source.filename();
    std::filesystem::copy_file(source, copy, std::filesystem::copy_options::overwrite_existing);

    const auto utf8 = copy.u8string();
    {
        PluginBackend backend(std::string(utf8.begin(), utf8.end()), R"({"model_id": "unicode"})");
        CHECK(backend.identity().model_id == "unicode");
    }
    std::filesystem::remove_all(dir, ignored);
}
