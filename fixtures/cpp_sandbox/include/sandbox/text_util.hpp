#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace sandbox {

std::vector<std::string> split(std::string_view text, char delimiter);
std::string join(const std::vector<std::string>& parts, char delimiter);
std::string trim(std::string_view text);

}  // namespace sandbox
