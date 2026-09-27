#include "process_memory.hpp"

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#elif defined(__linux__)
#include <fstream>
#include <string>
#endif

namespace lca {

ProcessMemory read_process_memory() {
    ProcessMemory out;
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS counters{};
    if (GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof counters)) {
        out.rss_bytes = static_cast<std::uint64_t>(counters.WorkingSetSize);
        out.peak_rss_bytes = static_cast<std::uint64_t>(counters.PeakWorkingSetSize);
    }
#elif defined(__linux__)
    std::ifstream status("/proc/self/status");
    std::string key;
    while (status >> key) {
        if (key == "VmRSS:" || key == "VmHWM:") {
            std::uint64_t kib = 0;
            if (status >> kib) {
                (key == "VmRSS:" ? out.rss_bytes : out.peak_rss_bytes) = kib * 1024;
            }
        }
        status.ignore(1 << 12, '\n');
    }
#endif
    return out;
}

}  // namespace lca
