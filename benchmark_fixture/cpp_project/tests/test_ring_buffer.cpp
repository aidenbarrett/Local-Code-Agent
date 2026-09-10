#include "sandbox/ring_buffer.hpp"

#include <cassert>
#include <cstdio>
#include <vector>

#ifdef _MSC_VER
#define LCA_ASSERT(expr)                                                        \
    do {                                                                        \
        if (!(expr)) {                                                          \
            std::fprintf(stderr, "Assertion %s failed.\n", #expr);             \
            return 1;                                                           \
        }                                                                       \
    } while (0)
#else
#define LCA_ASSERT(expr) assert(expr)
#endif

int main() {
    sandbox::RingBuffer buffer(3);
    LCA_ASSERT(buffer.empty());
    LCA_ASSERT(buffer.capacity() == 3);

    LCA_ASSERT(buffer.push(1));
    LCA_ASSERT(buffer.push(2));
    LCA_ASSERT(buffer.push(3));
    LCA_ASSERT(buffer.full());
    LCA_ASSERT(!buffer.push(4) && "push on a full buffer must be rejected");

    LCA_ASSERT(buffer.pop().value() == 1);
    LCA_ASSERT(buffer.pop().value() == 2);
    LCA_ASSERT(buffer.pop().value() == 3);
    LCA_ASSERT(buffer.empty());
    LCA_ASSERT(!buffer.pop().has_value());

    const std::vector<int> values{1, 2, 3, 4};
    LCA_ASSERT(sandbox::checksum(values) == 31810);

    std::puts("ring_buffer ok");
    return 0;
}
