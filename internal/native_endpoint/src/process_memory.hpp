#pragma once

#include <cstdint>
#include <optional>

namespace lca {

// Resident memory of this process. Fields stay empty on platforms where the
// value is not read, rather than reporting zero.
struct ProcessMemory {
    std::optional<std::uint64_t> rss_bytes;
    std::optional<std::uint64_t> peak_rss_bytes;
};

ProcessMemory read_process_memory();

}  // namespace lca
