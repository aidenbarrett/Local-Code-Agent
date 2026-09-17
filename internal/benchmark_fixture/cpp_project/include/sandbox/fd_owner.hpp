#pragma once

#include <utility>

namespace sandbox {

// Move-only owner of a POSIX-style file descriptor. Header only so the
// scenarios can break the library without disturbing it.
class FdOwner {
public:
    FdOwner() = default;
    explicit FdOwner(int fd) noexcept : fd_(fd) {}

    FdOwner(const FdOwner&) = delete;
    FdOwner& operator=(const FdOwner&) = delete;

    FdOwner(FdOwner&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}

    FdOwner& operator=(FdOwner&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }

    ~FdOwner() { reset(); }

    [[nodiscard]] int get() const noexcept { return fd_; }
    [[nodiscard]] bool valid() const noexcept { return fd_ >= 0; }

    int release() noexcept { return std::exchange(fd_, -1); }

    void reset(int fd = -1) noexcept {
        // The sandbox does not actually close anything; it only tracks state.
        fd_ = fd;
    }

private:
    int fd_ = -1;
};

}  // namespace sandbox
