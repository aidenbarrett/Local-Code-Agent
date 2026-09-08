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
