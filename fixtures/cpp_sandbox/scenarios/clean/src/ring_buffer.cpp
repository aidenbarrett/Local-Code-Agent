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
