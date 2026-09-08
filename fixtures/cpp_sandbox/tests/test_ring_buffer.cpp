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
